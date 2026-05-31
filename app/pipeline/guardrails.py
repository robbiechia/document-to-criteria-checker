"""Hallucination and escalation guardrails for extracted RuleSets.

Source-clause verification uses a two-tier approach for PDFs:

  Tier 1 — partial_ratio ≥ 85%
            Finds the best fuzzy match of the clause against any window of the document.
            Catches verbatim and lightly paraphrased clauses.

  Tier 2 — token_set_ratio ≥ 70%
            Order-insensitive token overlap. Catches cases where the model reordered
            words or slightly expanded abbreviations (e.g. "PR" → "Permanent Resident",
            "AV" → "Annual Value") — the actual failure modes on policy documents.
            No model download needed, runs instantly.

A clause that passes either tier is considered verified.
Only clauses that fail both tiers are flagged as hallucination_risk.

Images skip verification entirely — OCR and model-read text differ systematically.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from rapidfuzz import fuzz

from app.models.schemas import ConditionNode, RuleSet

FUZZY_THRESHOLD      = 75   # partial_ratio — consistent with eval_stage1 match threshold
TOKEN_SET_THRESHOLD  = 70   # token_set_ratio — order-insensitive / abbreviation expansion


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def verify_source_clause(
    source_clause: str,
    document_text: str,
    threshold: int = FUZZY_THRESHOLD,
) -> tuple[bool, float]:
    """Tier 1: sliding-window partial_ratio."""
    clause   = _normalise(source_clause)
    document = _normalise(document_text)
    if not clause or not document:
        return False, 0.0
    if clause in document:
        return True, 100.0

    window_size = min(max(len(clause) + 80, 120), len(document))
    step        = max(20, len(clause) // 3)
    best_score  = 0.0
    for start in range(0, max(len(document) - window_size + 1, 1), step):
        window = document[start : start + window_size]
        score  = fuzz.partial_ratio(clause, window)
        best_score = max(best_score, score)
        if score >= threshold:
            return True, float(score)
    return False, float(best_score)


def _token_set_verify(
    source_clause: str,
    document_text: str,
    threshold: int = TOKEN_SET_THRESHOLD,
) -> tuple[bool, float]:
    """Tier 2: token_set_ratio over sliding windows.

    token_set_ratio is order-insensitive and handles abbreviation expansions well:
      "PR" → "Permanent Resident", "AV" → "Annual Value"
      reordered words: "income per person monthly" vs "monthly income per person"
    """
    clause   = _normalise(source_clause)
    document = _normalise(document_text)
    if not clause or not document:
        return False, 0.0

    # Larger window for token_set since order doesn't matter
    window_size = min(max(len(clause) + 120, 200), len(document))
    step        = max(30, len(clause) // 2)
    best_score  = 0.0
    for start in range(0, max(len(document) - window_size + 1, 1), step):
        window = document[start : start + window_size]
        score  = fuzz.token_set_ratio(clause, window)
        best_score = max(best_score, score)
        if score >= threshold:
            return True, float(score)
    return False, float(best_score)


def _walk_nodes(node: ConditionNode) -> Iterable[ConditionNode]:
    yield node
    for child in node.conditions:
        yield from _walk_nodes(child)


def apply_guardrail(
    ruleset: RuleSet,
    document_text: str,
    is_image: bool = False,
) -> RuleSet:
    """Two-tier source-clause verification for PDFs; skipped for images."""
    if is_image:
        ruleset.hallucination_risk_count = 0
        return ruleset

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

        # Tier 1: partial_ratio
        verified, t1_score = verify_source_clause(
            node.source_clause, document_text, FUZZY_THRESHOLD
        )

        if not verified:
            # Tier 2: token_set_ratio (catches abbreviation expansions + reordering)
            verified, t2_score = _token_set_verify(
                node.source_clause, document_text, TOKEN_SET_THRESHOLD
            )
            if verified:
                node.hallucination_risk = False
                node.hallucination_note = None
            else:
                node.hallucination_risk = True
                node.hallucination_note = (
                    f"partial_ratio={t1_score:.0f}% token_set={t2_score:.0f}% — "
                    "source clause not verified. Check against source document."
                )
                risk_count += 1
        else:
            node.hallucination_risk = False
            node.hallucination_note = None

    ruleset.hallucination_risk_count = risk_count
    return ruleset


def apply_safety_gate(ruleset: RuleSet) -> RuleSet:
    """Remove discretionary conditions from the executable tree."""
    newly_escalated: list[ConditionNode] = []
    escalated_ids = {rule.rule_id for rule in ruleset.escalated_rules}

    def mark_escalated(node: ConditionNode) -> None:
        if not node.escalated:
            node.escalated = True
        if node.rule_id not in escalated_ids:
            newly_escalated.append(node)
            escalated_ids.add(node.rule_id)

    def filter_node(node: ConditionNode) -> Optional[ConditionNode]:
        if node.is_leaf:
            if node.should_escalate:
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
            description="All extracted conditions require escalation (discretionary).",
            source_clause="No executable conditions remain after safety gate.",
            escalated=True,
            escalation_note="all conditions are discretionary",
        )

    ruleset.root = filtered_root

    tree_ids: set[str] = {n.rule_id for n in _walk_nodes(ruleset.root)}
    all_escalated = [*ruleset.escalated_rules, *newly_escalated]
    ruleset.escalated_rules = [
        r for r in all_escalated
        if r.should_escalate and r.rule_id not in tree_ids
    ]
    return ruleset
