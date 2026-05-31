"""Convert a RuleSet into a decision tree JSON and optionally a Mermaid flowchart.

Stage 2 Step 6 from build.md:
  - Generate decision tree JSON from RuleSet
  - Use LLM to generate Mermaid flowchart if tree has <= MERMAID_LIMIT leaf conditions
  - If tree is too large, prompt user to scope the problem

MERMAID_LIMIT = 30 leaf conditions.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import app.config as _cfg

from app.models.schemas import ConditionNode, RuleSet
from app.pipeline.prompts import STAGE2_FLOWCHART_SYSTEM, STAGE2_FLOWCHART_USER_TEMPLATE
from app.utils.llm_client import complete

MERMAID_LIMIT = 30


def _node_to_tree(node: ConditionNode) -> dict[str, Any]:
    """Recursively convert a ConditionNode into a plain dict for JSON serialisation."""
    base: dict[str, Any] = {
        "id": node.rule_id,
        "label": node.description,
        "escalated": node.escalated,
    }

    if node.is_leaf:
        base["type"] = "LEAF"
        base["condition_type"] = node.condition_type.value if node.condition_type else None
        base["field"] = node.field_required
        base["operator"] = node.operator.value if node.operator else None
        base["threshold"] = node.threshold
        if node.source_clause:
            base["source_clause"] = node.source_clause
    else:
        base["type"] = node.logic  # "AND" or "OR"
        base["children"] = [_node_to_tree(child) for child in node.conditions]

    return base


def to_tree_json(ruleset: RuleSet) -> dict[str, Any]:
    """Build the decision tree dict from a RuleSet."""
    tree = {
        "policy_name": ruleset.policy_name,
        "constraint_scenario": ruleset.constraint_scenario,
        "tree": _node_to_tree(ruleset.root),
        "escalated_rules": [
            {
                "id": r.rule_id,
                "label": r.description,
                "type": "ESCALATED",
                "reason": r.escalation_note or "discretionary",
                "source_clause": r.source_clause,
            }
            for r in ruleset.escalated_rules
        ],
    }
    return tree


def _count_leaves(node: dict) -> int:
    if node.get("type") in ("LEAF", "ESCALATED"):
        return 1
    return sum(_count_leaves(child) for child in node.get("children", []))


def generate_mermaid(
    tree_dict: dict[str, Any],
    model: Optional[str] = None,
) -> tuple[str, bool]:
    """Generate a Mermaid flowchart from the tree dict via LLM.

    Returns (mermaid_str, was_generated).
    was_generated=False if the tree is too large to generate a useful diagram.
    """
    leaf_count = _count_leaves(tree_dict.get("tree", {}))
    if leaf_count > MERMAID_LIMIT:
        msg = (
            f"# Flowchart not generated\n"
            f"# The tree has {leaf_count} leaf conditions, exceeding the {MERMAID_LIMIT}-condition limit.\n"
            f"# To generate a flowchart, scope the extraction to a sub-section of the policy document.\n"
            f"# Re-run Stage 1 with a more specific constraint_scenario or on a shorter document.\n"
        )
        return msg, False

    model_name = model or _cfg.get("MODEL_STAGE2", "openai/gpt-4.1")
    tree_json = json.dumps(tree_dict, indent=2)
    user_message = STAGE2_FLOWCHART_USER_TEMPLATE.format(tree_json=tree_json)

    completion = complete(
        system=STAGE2_FLOWCHART_SYSTEM,
        user=user_message,
        model=model_name,
        max_tokens=2048,
        temperature=0.0,
    )

    mermaid = completion.text
    # Ensure it starts with flowchart directive
    if not mermaid.strip().startswith("flowchart"):
        mermaid = "flowchart TD\n" + mermaid

    return mermaid, True
