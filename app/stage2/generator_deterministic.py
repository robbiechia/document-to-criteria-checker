"""Stage 2 deterministic code generator.

Walks the RuleSet tree and emits one Python check function per executable leaf,
one escalation stub per escalated rule, a DISPATCH table, a _TREE constant, and
an evaluate(profile) function that preserves the AND/OR tree logic.
"""

from __future__ import annotations

import re
import textwrap
from datetime import datetime
from typing import Any

from app.models.schemas import ConditionNode, ConditionType, LogicOperator, RuleSet

_OP_MAP = {
    LogicOperator.GTE: ">=",
    LogicOperator.GT: ">",
    LogicOperator.LTE: "<=",
    LogicOperator.LT: "<",
    LogicOperator.EQ: "==",
    LogicOperator.IN: "in",
    LogicOperator.NOT_IN: "not in",
}

_HEADER = '''\
"""
PolicyCode — generated constraint functions
Source:    {policy_name}
Scenario:  {constraint_scenario}
Generated: {timestamp}
Generator: deterministic
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RuleResult:
    verdict: str  # "allow" | "deny" | "escalate"
    rule_id: str
    clause: str
    reason: str
'''


def _safe_fn(rule_id: str, prefix: str = "check") -> str:
    """Convert a rule_id to a valid Python function name."""
    slug = re.sub(r"[^a-zA-Z0-9]", "_", rule_id).lower().strip("_")
    return f"{prefix}_{slug}"


def _threshold_body(node: ConditionNode) -> str:
    field = node.field_required or "UNKNOWN_FIELD"
    op_sym = _OP_MAP.get(node.operator, "==") if node.operator else "=="  # type: ignore[arg-type]
    threshold = node.threshold
    # Represent threshold as Python literal
    if isinstance(threshold, str):
        threshold_repr = repr(threshold)
    elif isinstance(threshold, list):
        threshold_repr = "{" + ", ".join(repr(v) for v in threshold) + "}"
    else:
        threshold_repr = repr(threshold)

    op_enum = node.operator
    is_set_op = op_enum in (LogicOperator.IN, LogicOperator.NOT_IN)

    if is_set_op:
        allowed_var = "allowed" if op_enum == LogicOperator.IN else "excluded"
        set_repr = "{" + ", ".join(repr(v) for v in (threshold if isinstance(threshold, list) else [threshold])) + "}"
        return textwrap.dedent(f"""\
            val = profile.get({repr(field)})
            if val is None:
                return RuleResult(
                    verdict="escalate", rule_id=RULE_ID, clause=CLAUSE,
                    reason="Required field {repr(field)} not present in profile",
                )
            {allowed_var} = {set_repr}
            if val {op_sym} {allowed_var}:
                return RuleResult(
                    verdict="allow", rule_id=RULE_ID, clause=CLAUSE,
                    reason=f"{field}={{val!r}} satisfies {op_sym} {allowed_var}",
                )
            return RuleResult(
                verdict="deny", rule_id=RULE_ID, clause=CLAUSE,
                reason=f"{field}={{val!r}} fails {op_sym} {allowed_var}",
            )
        """)
    else:
        return textwrap.dedent(f"""\
            val = profile.get({repr(field)})
            if val is None:
                return RuleResult(
                    verdict="escalate", rule_id=RULE_ID, clause=CLAUSE,
                    reason="Required field {repr(field)} not present in profile",
                )
            if val {op_sym} {threshold_repr}:
                return RuleResult(
                    verdict="allow", rule_id=RULE_ID, clause=CLAUSE,
                    reason=f"{field}={{val!r}} satisfies {op_sym} {threshold_repr}",
                )
            return RuleResult(
                verdict="deny", rule_id=RULE_ID, clause=CLAUSE,
                reason=f"{field}={{val!r}} fails {op_sym} {threshold_repr}",
            )
        """)


def _sequential_body(node: ConditionNode) -> str:
    """Sequential: both current_state and next_state must match an allowed transition."""
    field = node.field_required or "current_flat_type"
    # Infer the next-state field name
    next_field = field.replace("current", "next") if "current" in field else f"next_{field}"
    threshold = node.threshold
    if isinstance(threshold, str) and "|" in threshold:
        pairs = [pair.split("->") for pair in threshold.split("|")]
    else:
        pairs = []

    if pairs:
        pair_repr = repr([[p[0].strip(), p[1].strip()] for p in pairs if len(p) == 2])
    else:
        pair_repr = "[]  # TODO: populate allowed transitions from source clause"

    return textwrap.dedent(f"""\
        current = profile.get({repr(field)})
        next_val = profile.get({repr(next_field)})
        if current is None or next_val is None:
            return RuleResult(
                verdict="escalate", rule_id=RULE_ID, clause=CLAUSE,
                reason="Sequential condition requires both {field!r} and {next_field!r} in profile",
            )
        allowed_transitions = {pair_repr}
        if not allowed_transitions or [current, next_val] in allowed_transitions:
            return RuleResult(
                verdict="allow", rule_id=RULE_ID, clause=CLAUSE,
                reason=f"Transition {{current!r}} -> {{next_val!r}} is permitted",
            )
        return RuleResult(
            verdict="deny", rule_id=RULE_ID, clause=CLAUSE,
            reason=f"Transition {{current!r}} -> {{next_val!r}} is not a permitted upgrade path",
        )
    """)


def _computation_stub(node: ConditionNode) -> str:
    field = node.field_required or "computed_value"
    op_sym = _OP_MAP.get(node.operator, "<=") if node.operator else "<="
    threshold = node.threshold
    threshold_repr = repr(threshold) if threshold is not None else "None"
    threshold_comment = "" if threshold is not None else "  # TODO: set threshold from source clause"
    source_snippet = node.source_clause[:200].replace("\n", " ")
    return textwrap.dedent(f"""\
        # FORMULA REQUIRED before comparison
        # Source: {source_snippet}
        #
        # Steps to implement:
        #   1. Derive {field!r} from the profile using the formula in the source clause
        #   2. Replace the escalate return below with the formula result
        #   3. Then compare: derived_value {op_sym} {threshold_repr}
        #
        # Example structure once implemented:
        #   derived = compute_{field}(profile)
        #   if derived is None:
        #       return RuleResult(verdict="escalate", ...)
        #   if derived {op_sym} {threshold_repr}:
        #       return RuleResult(verdict="allow", ...)
        #   return RuleResult(verdict="deny", ...)
        derived = profile.get({repr(field)})
        if derived is None:
            return RuleResult(
                verdict="escalate", rule_id=RULE_ID, clause=CLAUSE,
                reason=(
                    f"Field {repr(field)} requires formula derivation. "
                    "Implement the formula from the source clause before evaluating."
                ),
            )
        THRESHOLD = {threshold_repr}{threshold_comment}
        if THRESHOLD is None:
            return RuleResult(
                verdict="escalate", rule_id=RULE_ID, clause=CLAUSE,
                reason="Threshold not set — implement formula and set THRESHOLD",
            )
        if derived {op_sym} THRESHOLD:
            return RuleResult(
                verdict="allow", rule_id=RULE_ID, clause=CLAUSE,
                reason=f"{field}={{derived!r}} satisfies {op_sym} {{THRESHOLD!r}}",
            )
        return RuleResult(
            verdict="deny", rule_id=RULE_ID, clause=CLAUSE,
            reason=f"{field}={{derived!r}} fails {op_sym} {{THRESHOLD!r}}",
        )
    """)


def _existence_body(node: ConditionNode) -> str:
    """Existence: check whether a record is present in an external data source.

    The profile field holds the pre-fetched records (list) or a boolean indicating
    whether the record exists. If the field is absent, escalate — the external
    data has not yet been fetched into the profile.
    """
    field = node.field_required or "existence_records"
    source_snippet = node.source_clause[:200].replace("\n", " ")
    note = node.escalation_note or "requires external record lookup"
    return textwrap.dedent(f"""\
        # EXISTENCE CHECK: {source_snippet}
        # External source: {note}
        #
        # The profile field {field!r} should be pre-populated from the external
        # system before calling evaluate(). It can be:
        #   - A list of matching records (empty list = no record = first-timer / allow)
        #   - A boolean (False = no record = allow, True = record exists = deny)
        #   - None = data not yet fetched → escalate
        records = profile.get({repr(field)})
        if records is None:
            return RuleResult(
                verdict="escalate", rule_id=RULE_ID, clause=CLAUSE,
                reason=(
                    f"Field {repr(field)} not in profile. "
                    "Pre-populate from external system before evaluating: {note!r}"
                ),
            )
        # Boolean form: True means record exists
        if isinstance(records, bool):
            exists = records
        else:
            exists = len(records) > 0
        if not exists:
            return RuleResult(
                verdict="allow", rule_id=RULE_ID, clause=CLAUSE,
                reason=f"No matching record found in external system ({note})",
            )
        return RuleResult(
            verdict="deny", rule_id=RULE_ID, clause=CLAUSE,
            reason=f"Matching record exists in external system — condition not met ({note})",
        )
    """)




def _make_check_function(node: ConditionNode) -> str:
    fn_name = _safe_fn(node.rule_id, "check")
    clause = node.source_clause.replace('"', '\\"')
    docstring = f'    """\n    {node.description}\n    Source: {node.source_clause[:300]}\n    """'
    rule_id_const = f'    RULE_ID = {repr(node.rule_id)}'
    clause_const = f'    CLAUSE = {repr(node.source_clause[:500])}'

    ctype = node.condition_type
    if ctype in (ConditionType.THRESHOLD, ConditionType.MEMBERSHIP, ConditionType.TEMPORAL):
        body = _threshold_body(node)
    elif ctype == ConditionType.SEQUENTIAL:
        body = _sequential_body(node)
    elif ctype == ConditionType.COMPUTATION:
        body = _computation_stub(node)
    elif ctype == ConditionType.EXISTENCE:
        body = _existence_body(node)
    elif ctype == ConditionType.COMPOSITE:
        # Legacy type — treat same as threshold/membership depending on fields present
        body = _threshold_body(node) if (node.field_required and node.operator) else _computation_stub(node)
    else:
        body = textwrap.dedent(f"""\
            return RuleResult(
                verdict="escalate", rule_id=RULE_ID, clause=CLAUSE,
                reason="Unhandled condition type: {ctype}",
            )
        """)

    indented_body = textwrap.indent(body, "    ")
    return (
        f"def {fn_name}(profile: dict[str, Any]) -> RuleResult:\n"
        f"{docstring}\n"
        f"{rule_id_const}\n"
        f"{clause_const}\n"
        f"{indented_body}\n"
    )


def _make_escalation_stub(node: ConditionNode) -> str:
    fn_name = _safe_fn(node.rule_id, "escalate")
    reason = node.escalation_note or "requires_manual_review"
    return (
        f"def {fn_name}(profile: dict[str, Any]) -> RuleResult:\n"
        f'    """\n'
        f"    ESCALATION REQUIRED: {node.description}\n"
        f"    Source: {node.source_clause[:300]}\n"
        f"    Reason: {reason}\n"
        f'    """\n'
        f"    return RuleResult(\n"
        f"        verdict=\"escalate\",\n"
        f"        rule_id={repr(node.rule_id)},\n"
        f"        clause={repr(node.source_clause[:500])},\n"
        f"        reason={repr(f'{reason}: requires manual review')},\n"
        f"    )\n\n"
    )


def _encode_tree(node: ConditionNode) -> str:
    """Encode the ConditionNode tree as a nested tuple literal."""
    if node.is_leaf:
        return f"({repr(node.rule_id)}, {repr(node.logic)}, [])"
    children = ", ".join(_encode_tree(child) for child in node.conditions)
    return f"({repr(node.rule_id)}, {repr(node.logic)}, [{children}])"


def _collect_leaves(node: ConditionNode) -> list[ConditionNode]:
    if node.is_leaf:
        return [node]
    leaves = []
    for child in node.conditions:
        leaves.extend(_collect_leaves(child))
    return leaves


_EVAL_NODE_FN = '''\

def _eval_node(
    node_id: str,
    logic: str,
    children: list,
    profile: dict[str, Any],
    results: list,
    escalated: list,
) -> str:
    if not children:
        fn = _DISPATCH.get(node_id)
        if fn is None:
            escalated.append(node_id)
            return "escalate"
        r = fn(profile)
        results.append(r)
        if r.verdict == "escalate":
            escalated.append(node_id)
        return r.verdict

    verdicts = [
        _eval_node(cid, clogic, cchildren, profile, results, escalated)
        for cid, clogic, cchildren in children
    ]

    if logic == "AND":
        if all(v == "allow" for v in verdicts):
            return "allow"
        if any(v == "deny" for v in verdicts):
            return "deny"
        return "escalate"
    # OR
    if any(v == "allow" for v in verdicts):
        return "allow"
    if all(v == "deny" for v in verdicts):
        return "deny"
    return "escalate"


def evaluate(profile: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a profile against all extracted eligibility conditions.

    Returns a dict with keys:
      overall_verdict  — "allow" | "deny" | "escalate"
      results          — list of per-condition dicts
      escalated_rule_ids — list of rule_ids that triggered escalation
    """
    results: list[RuleResult] = []
    escalated_ids: list[str] = []
    overall = _eval_node(*_TREE, profile, results, escalated_ids)
    return {
        "overall_verdict": overall,
        "results": [
            {
                "verdict": r.verdict,
                "rule_id": r.rule_id,
                "clause": r.clause,
                "reason": r.reason,
            }
            for r in results
        ],
        "escalated_rule_ids": escalated_ids,
    }
'''


def generate(ruleset: RuleSet) -> str:
    """Generate executable Python constraint code from a validated RuleSet.

    Returns the complete Python source as a string.
    """
    parts: list[str] = []

    # Header
    parts.append(
        _HEADER.format(
            policy_name=ruleset.policy_name,
            constraint_scenario=ruleset.constraint_scenario,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
    )

    # Executable leaf functions — every type except discretionary generates code.
    leaves = _collect_leaves(ruleset.root)
    executable_leaves = [
        n for n in leaves
        if n.condition_type is not None
        and n.condition_type != ConditionType.DISCRETIONARY
    ]

    if executable_leaves:
        parts.append("\n# " + "=" * 68)
        parts.append("# Executable check functions")
        parts.append("# " + "=" * 68 + "\n")
        for node in executable_leaves:
            parts.append(_make_check_function(node))

    # Escalation stubs
    all_escalated: list[ConditionNode] = list(ruleset.escalated_rules)
    escalated_leaves = [n for n in leaves if n.should_escalate]
    seen_ids = {n.rule_id for n in all_escalated}
    for n in escalated_leaves:
        if n.rule_id not in seen_ids:
            all_escalated.append(n)
            seen_ids.add(n.rule_id)

    if all_escalated:
        parts.append("\n# " + "=" * 68)
        parts.append("# Escalation stubs (discretionary — officer judgment required)")
        parts.append("# " + "=" * 68 + "\n")
        for node in all_escalated:
            parts.append(_make_escalation_stub(node))

    # Dispatch table
    dispatch_entries: list[str] = []
    for node in executable_leaves:
        fn = _safe_fn(node.rule_id, "check")
        dispatch_entries.append(f"    {repr(node.rule_id)}: {fn},")
    for node in all_escalated:
        fn = _safe_fn(node.rule_id, "escalate")
        dispatch_entries.append(f"    {repr(node.rule_id)}: {fn},")

    dispatch_body = "\n".join(dispatch_entries) if dispatch_entries else "    # (empty)"
    parts.append(
        "\n# " + "=" * 68 + "\n"
        "# Dispatch table\n"
        "# " + "=" * 68 + "\n\n"
        f"_DISPATCH: dict[str, Any] = {{\n{dispatch_body}\n}}\n"
    )

    # Tree constant
    tree_repr = _encode_tree(ruleset.root)
    parts.append(
        "\n# " + "=" * 68 + "\n"
        "# AND/OR tree (preserves original policy structure)\n"
        "# " + "=" * 68 + "\n\n"
        f"_TREE = {tree_repr}\n"
    )

    # Tree evaluator + evaluate()
    parts.append(_EVAL_NODE_FN)

    return "\n".join(parts)
