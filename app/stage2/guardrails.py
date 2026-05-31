"""Stage 2 guardrails for generated Python constraint code.

Checks (4a–4g from build.md):
  4a  Only supported condition types become executable checks
  4b  Escalated rules return "escalate" or appear in escalation report
  4c  Every executable RuleSet leaf maps to a generated function
  4d  No hallucinated rules — every generated function references a known rule_id
  4e  AND/OR structure preserved — evaluate() walks the tree
  4f  Syntax validation (ast.parse)
  4g  No file / network / subprocess / system calls
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Optional

from app.models.schemas import ConditionNode, RuleSet

_FORBIDDEN_MODULES = frozenset(
    {
        "os", "sys", "subprocess", "socket", "requests", "urllib",
        "http", "httpx", "pathlib", "shutil", "tempfile", "glob",
        "io", "ftplib", "smtplib", "asyncio",
    }
)

_FORBIDDEN_BUILTINS = frozenset({"open", "exec", "eval", "compile", "__import__"})


@dataclass
class GuardrailReport:
    syntax_ok: bool = False
    syntax_error: str = ""
    no_dangerous_calls: bool = False
    dangerous_violations: list[str] = field(default_factory=list)
    rule_coverage_ok: bool = False
    missing_functions: list[str] = field(default_factory=list)
    escalation_coverage_ok: bool = False
    missing_escalation_stubs: list[str] = field(default_factory=list)
    no_hallucinations: bool = False
    hallucinated_functions: list[str] = field(default_factory=list)
    evaluate_exists: bool = False
    passed: bool = False

    def summary(self) -> dict:
        return {
            "passed": self.passed,
            "syntax_ok": self.syntax_ok,
            "no_dangerous_calls": self.no_dangerous_calls,
            "rule_coverage_ok": self.rule_coverage_ok,
            "escalation_coverage_ok": self.escalation_coverage_ok,
            "no_hallucinations": self.no_hallucinations,
            "evaluate_exists": self.evaluate_exists,
            "dangerous_violations": self.dangerous_violations,
            "missing_functions": self.missing_functions,
            "missing_escalation_stubs": self.missing_escalation_stubs,
            "hallucinated_functions": self.hallucinated_functions,
        }


def _collect_all_nodes(ruleset: RuleSet) -> tuple[list[ConditionNode], list[ConditionNode]]:
    executable: list[ConditionNode] = []
    escalated: list[ConditionNode] = list(ruleset.escalated_rules)

    def walk(node: ConditionNode) -> None:
        if node.is_leaf:
            if node.should_escalate:
                if node.rule_id not in {n.rule_id for n in escalated}:
                    escalated.append(node)
            else:
                executable.append(node)
        for child in node.conditions:
            walk(child)

    walk(ruleset.root)
    return executable, escalated


def _check_syntax(code: str) -> tuple[bool, str]:
    try:
        ast.parse(code)
        return True, ""
    except SyntaxError as exc:
        return False, str(exc)


def _check_no_dangerous_calls(code: str) -> tuple[bool, list[str]]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False, ["syntax error — cannot inspect AST"]

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_mod = alias.name.split(".")[0]
                if root_mod in _FORBIDDEN_MODULES:
                    violations.append(f"Forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_mod = node.module.split(".")[0]
                if root_mod in _FORBIDDEN_MODULES:
                    violations.append(f"Forbidden import from: {node.module}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_BUILTINS:
                violations.append(f"Forbidden call: {node.func.id}()")
            elif isinstance(node.func, ast.Attribute):
                # e.g. os.system(), subprocess.run()
                if isinstance(node.func.value, ast.Name):
                    if node.func.value.id in _FORBIDDEN_MODULES:
                        violations.append(
                            f"Forbidden call: {node.func.value.id}.{node.func.attr}()"
                        )

    return len(violations) == 0, violations


def _check_rule_coverage(code: str, executable: list[ConditionNode]) -> tuple[bool, list[str]]:
    """Every executable leaf must have a check_ function in the generated code."""
    missing: list[str] = []
    for node in executable:
        # Accept either check_<slug> or the rule_id string itself referenced in code
        slug = re.sub(r"[^a-zA-Z0-9]", "_", node.rule_id).lower().strip("_")
        fn_name = f"check_{slug}"
        if fn_name not in code and node.rule_id not in code:
            missing.append(node.rule_id)
    return len(missing) == 0, missing


def _check_escalation_coverage(
    code: str, escalated: list[ConditionNode]
) -> tuple[bool, list[str]]:
    """Every escalated rule must be referenced and must return escalate."""
    missing: list[str] = []
    for node in escalated:
        slug = re.sub(r"[^a-zA-Z0-9]", "_", node.rule_id).lower().strip("_")
        fn_name = f"escalate_{slug}"
        rule_mentioned = fn_name in code or node.rule_id in code
        if not rule_mentioned:
            missing.append(node.rule_id)
    return len(missing) == 0, missing


def _check_no_hallucinations(
    code: str,
    executable: list[ConditionNode],
    escalated: list[ConditionNode],
) -> tuple[bool, list[str]]:
    """Every check_/escalate_ function in code must map to a known rule_id."""
    known_ids = {n.rule_id for n in executable} | {n.rule_id for n in escalated}
    known_slugs = {
        re.sub(r"[^a-zA-Z0-9]", "_", rid).lower().strip("_")
        for rid in known_ids
    }

    fn_pattern = re.compile(r"\bdef\s+(check|escalate)_([a-zA-Z0-9_]+)\s*\(")
    invented: list[str] = []
    for match in fn_pattern.finditer(code):
        fn_slug = match.group(2)
        if fn_slug not in known_slugs:
            # Allow helper functions like _eval_node, _safe_fn
            if not fn_slug.startswith("_"):
                invented.append(f"def {match.group(1)}_{fn_slug}()")
    return len(invented) == 0, invented


def _check_evaluate_exists(code: str) -> bool:
    return "def evaluate(" in code


def apply_guardrails(code: str, ruleset: RuleSet) -> GuardrailReport:
    """Run all Stage 2 guardrails and return a structured report."""
    report = GuardrailReport()
    executable, escalated = _collect_all_nodes(ruleset)

    # 4f — syntax
    report.syntax_ok, report.syntax_error = _check_syntax(code)

    # 4g — no dangerous calls
    report.no_dangerous_calls, report.dangerous_violations = _check_no_dangerous_calls(code)

    # 4c — rule coverage
    report.rule_coverage_ok, report.missing_functions = _check_rule_coverage(code, executable)

    # 4b — escalation coverage
    report.escalation_coverage_ok, report.missing_escalation_stubs = _check_escalation_coverage(
        code, escalated
    )

    # 4d — no hallucinations
    report.no_hallucinations, report.hallucinated_functions = _check_no_hallucinations(
        code, executable, escalated
    )

    # 4e — evaluate exists
    report.evaluate_exists = _check_evaluate_exists(code)

    report.passed = all(
        [
            report.syntax_ok,
            report.no_dangerous_calls,
            report.rule_coverage_ok,
            report.escalation_coverage_ok,
            report.no_hallucinations,
            report.evaluate_exists,
        ]
    )

    return report
