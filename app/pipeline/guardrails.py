"""Hallucination and escalation guardrails for extracted RuleSets."""

from __future__ import annotations

import re
from typing import Iterable

from rapidfuzz import fuzz

from app.models.schemas import ConditionNode, RuleSet

SIMILARITY_THRESHOLD = 85


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def verify_source_clause(
    source_clause: str,
    document_text: str,
    threshold: int = SIMILARITY_THRESHOLD,
) -> tuple[bool, float]:
    """Return whether a source clause can be verified in the document text."""
    clause = _normalise(source_clause)
    document = _normalise(document_text)
    if not clause or not document:
        return False, 0.0
    if clause in document:
        return True, 100.0

    window_size = min(max(len(clause) + 80, 120), len(document))
    step = max(20, len(clause) // 3)
    best_score = 0.0
    for start in range(0, max(len(document) - window_size + 1, 1), step):
        window = document[start : start + window_size]
        score = fuzz.partial_ratio(clause, window)
        best_score = max(best_score, score)
        if score >= threshold:
            return True, float(score)
    return False, float(best_score)


def _walk_nodes(node: ConditionNode) -> Iterable[ConditionNode]:
    yield node
    for child in node.conditions:
        yield from _walk_nodes(child)


def apply_guardrail(ruleset: RuleSet, document_text: str) -> RuleSet:
    """Flag nodes whose source_clause cannot be verified in the document."""
    risk_count = 0
    seen: set[int] = set()
    nodes = list(_walk_nodes(ruleset.root))
    for rule in ruleset.escalated_rules:
        nodes.extend(_walk_nodes(rule))

    for node in nodes:
        node_id = id(node)
        if node_id in seen:
            continue
        seen.add(node_id)
        verified, score = verify_source_clause(node.source_clause, document_text)
        if not verified:
            node.hallucination_risk = True
            node.hallucination_note = (
                "source_clause could not be verified in document "
                f"(best fuzzy match: {score:.0f}%). Do not use this condition "
                "for code generation without manual verification."
            )
            risk_count += 1

    ruleset.hallucination_risk_count = risk_count
    return ruleset


def apply_safety_gate(ruleset: RuleSet) -> RuleSet:
    """
    Remove escalated and hallucination-risk leaf nodes from the executable tree.

    Escalated nodes remain visible in ruleset.escalated_rules for review.
    """
    newly_escalated: list[ConditionNode] = []
    escalated_ids = {rule.rule_id for rule in ruleset.escalated_rules}

    def mark_escalated(node: ConditionNode) -> None:
        if not node.escalated:
            node.escalated = True
        if node.hallucination_risk and not node.escalation_note:
            node.escalation_note = "unhandled — removed from executable tree"
        if node.rule_id not in escalated_ids:
            newly_escalated.append(node)
            escalated_ids.add(node.rule_id)

    def filter_node(node: ConditionNode) -> ConditionNode | None:
        if node.is_leaf:
            if node.should_escalate or node.hallucination_risk:
                mark_escalated(node)
                return None
            return node

        kept_children = []
        for child in node.conditions:
            kept_child = filter_node(child)
            if kept_child is not None:
                kept_children.append(kept_child)
        node.conditions = kept_children

        if not kept_children:
            mark_escalated(node)
            return None
        return node

    filtered_root = filter_node(ruleset.root)
    if filtered_root is None:
        filtered_root = ConditionNode(
            rule_id="ROOT-EMPTY",
            description="All extracted conditions require escalation or verification.",
            source_clause="No executable conditions remain after safety gate.",
            escalated=True,
            escalation_note="all conditions require escalation or verification",
        )

    ruleset.root = filtered_root
    ruleset.escalated_rules = [*ruleset.escalated_rules, *newly_escalated]
    return ruleset
