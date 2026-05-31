"""Stage 2 SQL generator — deterministic path.

Converts a RuleSet into a clean, properly indexed SQL eligibility query.

Output shape:
  - WITH block (CTEs) for existence and computation stubs
  - SELECT with CASE WHEN preserving AND/OR tree logic
  - Escalated (discretionary) conditions become TRUE placeholders with a
    summary comment at the end — they do not clutter the WHERE logic
  - No inline chatter comments on every condition line
"""

from __future__ import annotations

import re
import textwrap
from datetime import datetime
from typing import Optional

from app.models.schemas import ConditionNode, ConditionType, LogicOperator, RuleSet

_OP_MAP = {
    LogicOperator.GTE:    ">=",
    LogicOperator.GT:     ">",
    LogicOperator.LTE:    "<=",
    LogicOperator.LT:     "<",
    LogicOperator.EQ:     "=",
    LogicOperator.IN:     "IN",
    LogicOperator.NOT_IN: "NOT IN",
}

_INDENT = "    "   # 4 spaces per tree level


def _sql_value(threshold) -> str:
    if isinstance(threshold, bool):
        return "TRUE" if threshold else "FALSE"
    if isinstance(threshold, str):
        return f"'{threshold}'"
    if isinstance(threshold, list):
        parts = ", ".join(f"'{v}'" if isinstance(v, str) else str(v) for v in threshold)
        return f"({parts})"
    return str(threshold) if threshold is not None else "NULL"


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", (name or "field").lower()).strip("_")


# ---------------------------------------------------------------------------
# Leaf → SQL fragment
# ---------------------------------------------------------------------------

def _leaf_to_sql(
    node: ConditionNode,
    params: list[str],
    ctes: list[str],
    escalated_notes: list[str],
) -> str:
    ctype = node.condition_type
    field = node.field_required or "unknown_field"

    # Discretionary — always TRUE in the WHERE so the query still runs,
    # noted in the footer summary instead of cluttering the logic.
    if ctype == ConditionType.DISCRETIONARY:
        escalated_notes.append(
            f"  -- [discretionary] {node.rule_id}: {node.description[:80]}"
        )
        return "TRUE  -- requires officer assessment (see footer)"

    # Existence — NOT EXISTS CTE stub
    if ctype == ConditionType.EXISTENCE:
        cte_name = f"{_safe_name(field)}_check"
        table    = f"{_safe_name(field)}_records"
        note     = node.escalation_note or "external record lookup"
        if cte_name not in [c.split("AS")[0].strip().split()[-1] for c in ctes]:
            ctes.append(
                f"{cte_name} AS (\n"
                f"    -- TODO: replace with real table — {note}\n"
                f"    SELECT 1 FROM {table}\n"
                f"    WHERE record_key = :applicant_id\n"
                f"    LIMIT 1\n"
                f")"
            )
        if field not in params:
            params.append(field)
        return f"NOT EXISTS (SELECT 1 FROM {cte_name})"

    # Computation — CTE stub with formula placeholder
    if ctype == ConditionType.COMPUTATION:
        cte_name  = _safe_name(field) + "_derived"
        op        = _OP_MAP.get(node.operator, "<=") if node.operator else "<="
        threshold = _sql_value(node.threshold) if node.threshold is not None else "NULL"
        if cte_name not in [c.split("AS")[0].strip().split()[-1] for c in ctes]:
            ctes.append(
                f"{cte_name} AS (\n"
                f"    -- TODO: implement formula for {field}\n"
                f"    -- Source: {node.source_clause[:120]}\n"
                f"    SELECT :applicant_id AS applicant_id,\n"
                f"           NULL::numeric AS {field}\n"
                f")"
            )
        if field not in params:
            params.append(field)
        return f"(SELECT {field} FROM {cte_name}) {op} {threshold}"

    # Sequential — two-field transition check
    if ctype == ConditionType.SEQUENTIAL:
        cur_field  = field
        next_field = field.replace("current", "next") if "current" in field else f"next_{field}"
        for f in (cur_field, next_field):
            if f not in params:
                params.append(f)
        threshold = node.threshold
        if isinstance(threshold, str) and "|" in threshold:
            pairs = [p.split("->") for p in threshold.split("|") if "->" in p]
            if pairs:
                cases = "\n        OR ".join(
                    f"({cur_field} = '{p[0].strip()}' AND {next_field} = '{p[1].strip()}')"
                    for p in pairs
                )
                return f"(\n        {cases}\n    )"
        # No explicit transitions — check both fields are present and non-null
        return f"({cur_field} IS NOT NULL AND {next_field} IS NOT NULL)"

    # threshold / membership / temporal — direct comparison
    if field not in params:
        params.append(field)

    op        = _OP_MAP.get(node.operator, "=") if node.operator else "="
    threshold = node.threshold

    if op in ("IN", "NOT IN") and isinstance(threshold, list):
        return f"{field} {op} {_sql_value(threshold)}"

    return f"{field} {op} {_sql_value(threshold)}"


# ---------------------------------------------------------------------------
# Tree → SQL boolean expression
# ---------------------------------------------------------------------------

def _tree_to_sql(
    node: ConditionNode,
    params: list[str],
    ctes: list[str],
    escalated_notes: list[str],
    depth: int = 0,
) -> str:
    pad = _INDENT * (depth + 1)

    if node.is_leaf:
        return _leaf_to_sql(node, params, ctes, escalated_notes)

    logic    = node.logic or "AND"
    children = [
        _tree_to_sql(c, params, ctes, escalated_notes, depth + 1)
        for c in node.conditions
    ]

    if len(children) == 1:
        return children[0]

    joiner  = f"\n{pad}{logic} "
    combined = joiner.join(children)

    if depth == 0:
        # Root — no extra wrapping parens
        return combined
    return f"(\n{pad}{combined}\n{_INDENT * depth})"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(ruleset: RuleSet, table_name: str = "applicant_profiles") -> str:
    """Generate a clean, parameterised SQL eligibility query."""
    params:          list[str] = []
    ctes:            list[str] = []
    escalated_notes: list[str] = []

    where_expr = _tree_to_sql(ruleset.root, params, ctes, escalated_notes, depth=0)

    # CTE block
    cte_block = ""
    if ctes:
        cte_block = "WITH\n" + ",\n\n".join(ctes) + "\n\n"

    # Escalated (discretionary) footer — kept out of WHERE logic
    footer = ""
    if escalated_notes or ruleset.escalated_rules:
        lines = ["", "-- Escalated conditions (require manual review, not auto-evaluated):"]
        for note in escalated_notes:
            lines.append(note)
        for rule in ruleset.escalated_rules:
            ctype = rule.condition_type.value if rule.condition_type else "unknown"
            lines.append(f"  -- [{ctype}] {rule.rule_id}: {rule.description[:80]}")
        footer = "\n".join(lines)

    # Indented WHERE expression (each line gets extra indent under WHEN)
    where_indented = textwrap.indent(where_expr, _INDENT * 5)

    sql = (
        f"-- {ruleset.policy_name} — eligibility check\n"
        f"-- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"\n"
        f"{cte_block}"
        f"SELECT\n"
        f"    :applicant_id       AS applicant_id,\n"
        f"    CASE\n"
        f"        WHEN\n"
        f"{where_indented}\n"
        f"        THEN 'eligible'\n"
        f"        ELSE 'ineligible'\n"
        f"    END                 AS verdict,\n"
        f"    CURRENT_TIMESTAMP   AS evaluated_at\n"
        f"FROM {table_name}\n"
        f"WHERE applicant_id = :applicant_id;"
        f"{footer}\n"
    )

    return sql
