"""Evaluate Stage 2 generated code.

Automated checks (guardrails):
  - Syntax valid (ast.parse)
  - No forbidden imports or calls (os, subprocess, eval, etc.)
  - Every executable RuleSet leaf has a check_ function
  - Every escalated rule has an escalate_ stub
  - evaluate() exists

Human review (generated checklist):
  - Operator direction correct for each condition
  - Threshold value matches document
  - Null/missing field handled (returns escalate, not crash)
  - Escalated stubs return "escalate"
  - OR tree logic in evaluate() (any child passes → allow)
  - spot-check two or three conditions end-to-end

The checklist is written to eval/results/<label>_review.md for the reviewer to fill in.
Automated checks run immediately; semantic correctness is the reviewer's call.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from datetime import datetime
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Automated guardrail checks
# ---------------------------------------------------------------------------

_FORBIDDEN_MODULES = frozenset({
    "os", "sys", "subprocess", "socket", "requests", "urllib",
    "http", "httpx", "pathlib", "shutil", "tempfile",
})
_FORBIDDEN_CALLS = frozenset({"open", "exec", "eval", "compile", "__import__"})


def check_syntax_and_safety(code: str) -> dict[str, Any]:
    report: dict[str, Any] = {}

    try:
        tree = ast.parse(code)
        report["syntax_ok"] = True
    except SyntaxError as exc:
        report["syntax_ok"] = False
        report["syntax_error"] = str(exc)
        return report

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _FORBIDDEN_MODULES:
                    violations.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in _FORBIDDEN_MODULES:
                violations.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
                violations.append(f"call to {node.func.id}()")
            elif isinstance(node.func, ast.Attribute):
                if (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id in _FORBIDDEN_MODULES
                ):
                    violations.append(
                        f"call to {node.func.value.id}.{node.func.attr}()"
                    )

    report["safe"] = len(violations) == 0
    report["dangerous_violations"] = violations

    fn_names = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    ]
    report["function_count"] = len(fn_names)
    report["evaluate_exists"] = "evaluate" in fn_names
    report["check_functions"] = [f for f in fn_names if f.startswith("check_")]
    report["escalate_stubs"] = [f for f in fn_names if f.startswith("escalate_")]

    return report


def check_coverage(code: str, ruleset_data: dict) -> dict[str, Any]:
    """Verify every executable leaf has a check_ function and every escalated rule
    has an escalate_ stub."""
    executable_missing: list[str] = []
    escalated_missing: list[str] = []

    def slug(rule_id: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]", "_", rule_id).lower().strip("_")

    def walk(node: dict) -> None:
        if not (node.get("conditions") or []):  # leaf
            if not node.get("escalated", False):
                fn = f"check_{slug(node.get('rule_id', ''))}"
                if fn not in code and node.get("rule_id", "") not in code:
                    executable_missing.append(node.get("rule_id", ""))
        for child in node.get("conditions", []):
            walk(child)

    walk(ruleset_data.get("root", {}))

    for rule in ruleset_data.get("escalated_rules", []):
        fn = f"escalate_{slug(rule.get('rule_id', ''))}"
        if fn not in code and rule.get("rule_id", "") not in code:
            escalated_missing.append(rule.get("rule_id", ""))

    return {
        "rule_coverage_ok": len(executable_missing) == 0,
        "escalation_coverage_ok": len(escalated_missing) == 0,
        "missing_check_functions": executable_missing,
        "missing_escalate_stubs": escalated_missing,
    }


# ---------------------------------------------------------------------------
# Human review checklist generator
# ---------------------------------------------------------------------------

def _extract_fn_body(code: str, fn_name: str) -> str:
    """Extract the source of a named function from the generated code string."""
    pattern = re.compile(
        rf"(def {re.escape(fn_name)}\b.*?)(?=\ndef |\Z)",
        re.DOTALL,
    )
    match = pattern.search(code)
    return match.group(1).strip() if match else f"(function {fn_name!r} not found in code)"


def _collect_all_leaves(node: dict) -> list[dict]:
    if not (node.get("conditions") or []):
        return [node]
    leaves = []
    for child in node.get("conditions", []):
        leaves.extend(_collect_all_leaves(child))
    return leaves


def generate_review_checklist(
    code: str,
    ruleset_data: dict,
    guardrail_summary: dict,
    label: str,
    variant: str,
) -> str:
    """Generate a markdown review checklist for human inspection."""

    executable_leaves = [
        n for n in _collect_all_leaves(ruleset_data.get("root", {}))
        if not n.get("escalated", False)
    ]
    escalated_rules = ruleset_data.get("escalated_rules", [])

    def slug(rule_id: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]", "_", rule_id).lower().strip("_")

    guard_pass = "✓ PASS" if guardrail_summary.get("passed") else "✗ FAIL"
    lines = [
        f"# Stage 2 Review — {label}",
        f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        f"**Variant:** {variant}  ",
        f"**Guardrails:** {guard_pass}",
        "",
        "---",
        "",
        "## Automated guardrail results",
        "",
        "| Check | Result |",
        "|-------|--------|",
    ]
    checks = [
        ("Syntax valid", guardrail_summary.get("syntax_ok", False)),
        ("No dangerous calls", guardrail_summary.get("no_dangerous_calls", False)),
        ("Rule coverage", guardrail_summary.get("rule_coverage_ok", False)),
        ("Escalation coverage", guardrail_summary.get("escalation_coverage_ok", False)),
        ("No hallucinations", guardrail_summary.get("no_hallucinations", False)),
        ("evaluate() exists", guardrail_summary.get("evaluate_exists", False)),
    ]
    for name, passed in checks:
        icon = "✓" if passed else "✗"
        lines.append(f"| {name} | {icon} |")

    lines += [
        "",
        "---",
        "",
        "## Human review",
        "",
        "For each condition below, verify the three things automation cannot check.",
        "Mark **pass** or **fail** in the Result column and add notes where needed.",
        "",
        "**Checks per condition:**",
        "- **Operator direction** — does `>=` / `<=` / `==` match the document language",
        '  (e.g. "must not exceed" → `lte`, "at least" → `gte`)?',
        "- **Threshold value** — does the number or set exactly match the document?",
        "- **Null handling** — if the profile field is missing, does the function return",
        '  `verdict="escalate"` without raising an exception?',
        "",
    ]

    if executable_leaves:
        lines += [
            "### Executable conditions",
            "",
            "| Rule ID | Source clause | Operator | Threshold | Result | Notes |",
            "|---------|--------------|----------|-----------|--------|-------|",
        ]
        for node in executable_leaves:
            rule_id = node.get("rule_id", "")
            source = (node.get("source_clause") or "")[:80].replace("|", "\\|")
            op = node.get("operator") or "—"
            threshold = node.get("threshold") or node.get("value") or "—"
            lines.append(f"| `{rule_id}` | {source} | `{op}` | `{threshold}` |  |  |")

        lines += [
            "",
            "#### Function spot-checks",
            "",
            "Pick the 3–5 most likely error-prone conditions (income computations, temporal",
            "direction, composite conditions) and paste the function body below to inspect.",
            "",
        ]
        # Auto-include computation and temporal conditions as priority spot-checks
        priority_types = {"computation", "temporal", "composite", "sequential"}
        priority = [
            n for n in executable_leaves
            if (n.get("condition_type") or "") in priority_types
        ][:5]

        for node in priority:
            rule_id = node.get("rule_id", "")
            fn_name = f"check_{slug(rule_id)}"
            fn_body = _extract_fn_body(code, fn_name)
            ctype = node.get("condition_type", "")
            lines += [
                f"<details>",
                f"<summary><code>{fn_name}</code> ({ctype})</summary>",
                "",
                "**Source clause:**",
                f"> {node.get('source_clause', '')[:300]}",
                "",
                "```python",
                fn_body[:1200],
                "```",
                "",
                "**Review:**  ",
                "- Operator direction: [ ] pass  [ ] fail — _notes:_",
                "- Threshold value:    [ ] pass  [ ] fail — _notes:_",
                "- Null handling:      [ ] pass  [ ] fail — _notes:_",
                "",
                "</details>",
                "",
            ]

    if escalated_rules:
        lines += [
            "---",
            "",
            "### Escalated conditions",
            "",
            "Verify each escalation stub returns `verdict='escalate'` and does not attempt",
            "to evaluate the condition.",
            "",
            "| Rule ID | Escalation reason | Result | Notes |",
            "|---------|------------------|--------|-------|",
        ]
        for rule in escalated_rules:
            rule_id = rule.get("rule_id", "")
            reason = rule.get("escalation_note") or "discretionary"
            lines.append(f"| `{rule_id}` | {reason} |  |  |")

    lines += [
        "",
        "---",
        "",
        "### OR tree logic",
        "",
        "Run `evaluate()` with a profile that satisfies only one OR alternative and",
        "verify the overall verdict is `allow` (not `deny`).",
        "",
        "- [ ] OR tree returns `allow` when one alternative passes",
        "- [ ] AND tree returns `deny` when one condition fails",
        "- [ ] Missing required field returns `escalate` (not exception)",
        "",
        "**Notes:**",
        "",
        "---",
        "",
        "### Overall verdict",
        "",
        "- [ ] Approve — code is correct, ready to use",
        "- [ ] Approve with minor corrections — list below",
        "- [ ] Reject — re-run generation with a different variant",
        "",
        "**Corrections needed:**",
        "",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main eval
# ---------------------------------------------------------------------------

def eval_stage2(
    code_path: str,
    ruleset_path: str | None,
    guardrail_path: str | None,
    label: str,
) -> dict:
    code = Path(code_path).read_text(encoding="utf-8")
    results: dict[str, Any] = {
        "label": label,
        "code_path": code_path,
        "ruleset_path": ruleset_path,
    }

    # Automated checks
    results["code_check"] = check_syntax_and_safety(code)

    ruleset_data: dict = {}
    if ruleset_path and Path(ruleset_path).exists():
        ruleset_data = json.loads(Path(ruleset_path).read_text(encoding="utf-8"))
        results["coverage"] = check_coverage(code, ruleset_data)

    if guardrail_path and Path(guardrail_path).exists():
        results["guardrails"] = json.loads(Path(guardrail_path).read_text(encoding="utf-8"))

    # Print summary
    cc = results["code_check"]
    print(f"\n=== Stage 2 Eval: {label} ===")
    print(f"Syntax:           {'OK' if cc.get('syntax_ok') else 'FAIL'}")
    print(f"Safe (no bad imports/calls): {'yes' if cc.get('safe') else 'no — ' + str(cc.get('dangerous_violations'))}")
    print(f"Function count:   {cc.get('function_count', '?')} ({len(cc.get('check_functions', []))} check_, {len(cc.get('escalate_stubs', []))} escalate_)")
    print(f"evaluate() exists: {cc.get('evaluate_exists', False)}")

    if "coverage" in results:
        cov = results["coverage"]
        print(f"Rule coverage:    {'OK' if cov['rule_coverage_ok'] else 'FAIL — missing: ' + str(cov['missing_check_functions'])}")
        print(f"Escalation stubs: {'OK' if cov['escalation_coverage_ok'] else 'FAIL — missing: ' + str(cov['missing_escalate_stubs'])}")

    if "guardrails" in results:
        gr = results["guardrails"]
        print(f"Guardrails overall: {'PASS' if gr.get('passed') else 'FAIL'}")

    # Generate human review checklist
    out_dir = Path("eval/results")
    out_dir.mkdir(parents=True, exist_ok=True)

    guardrail_summary = results.get("guardrails", results.get("coverage", {}))
    # merge code_check fields into guardrail_summary for the checklist
    combined = {**cc, **guardrail_summary}

    variant = ruleset_data.get("extraction_variant", "unknown") if ruleset_data else "unknown"
    checklist = generate_review_checklist(code, ruleset_data, combined, label, variant)

    checklist_path = out_dir / f"{label}_review.md"
    checklist_path.write_text(checklist, encoding="utf-8")
    print(f"\nHuman review checklist: {checklist_path}")
    print("Open it, work through each condition, mark pass/fail, note corrections.")

    results_path = out_dir / f"{label}_stage2.json"
    results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Automated results:      {results_path}")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Evaluate Stage 2 generated code (automated checks + human review checklist)"
    )
    parser.add_argument("--code", required=True, help="Path to generated Python file")
    parser.add_argument(
        "--ruleset", default=None, help="Path to the Stage 1 RuleSet JSON this code was generated from"
    )
    parser.add_argument(
        "--guardrails", default=None, help="Path to guardrail JSON report from run_stage2_experiment.py"
    )
    parser.add_argument("--label", required=True)
    args = parser.parse_args()

    eval_stage2(
        code_path=args.code,
        ruleset_path=args.ruleset,
        guardrail_path=args.guardrails,
        label=args.label,
    )
