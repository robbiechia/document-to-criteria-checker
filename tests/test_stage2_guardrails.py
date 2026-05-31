"""Tests for Stage 2 guardrails."""

import pytest

from app.models.schemas import (
    ConditionNode,
    ConditionType,
    LogicOperator,
    RuleSet,
)
from app.stage2 import generator_deterministic
from app.stage2.guardrails import apply_guardrails


def _leaf(rule_id: str = "TEST-001", condition_type=ConditionType.THRESHOLD) -> ConditionNode:
    return ConditionNode(
        rule_id=rule_id,
        description=f"Test condition {rule_id}",
        source_clause="The applicant must be at least 21 years old.",
        condition_type=condition_type,
        field_required="buyer_age",
        operator=LogicOperator.GTE,
        threshold=21,
    )


def _simple_ruleset(leaf: ConditionNode) -> RuleSet:
    return RuleSet(
        policy_name="Test",
        policy_version="2026",
        source_document="test.pdf",
        constraint_scenario="Test",
        extraction_variant="direct",
        model_used="test",
        root=leaf,
    )


def test_deterministic_code_passes_all_guardrails():
    ruleset = _simple_ruleset(_leaf())
    code = generator_deterministic.generate(ruleset)
    report = apply_guardrails(code, ruleset)
    assert report.syntax_ok
    assert report.no_dangerous_calls
    assert report.rule_coverage_ok
    assert report.evaluate_exists
    assert report.passed


def test_bad_syntax_fails():
    ruleset = _simple_ruleset(_leaf())
    bad_code = "def foo(:\n    pass"
    report = apply_guardrails(bad_code, ruleset)
    assert not report.syntax_ok
    assert not report.passed


def test_dangerous_import_caught():
    ruleset = _simple_ruleset(_leaf())
    code = generator_deterministic.generate(ruleset)
    code += "\nimport os\nos.system('echo hello')\n"
    report = apply_guardrails(code, ruleset)
    assert not report.no_dangerous_calls
    assert "Forbidden import: os" in report.dangerous_violations
    assert not report.passed


def test_dangerous_call_caught():
    ruleset = _simple_ruleset(_leaf())
    code = generator_deterministic.generate(ruleset)
    code += "\nresult = eval('1+1')\n"
    report = apply_guardrails(code, ruleset)
    assert not report.no_dangerous_calls
    assert not report.passed


def test_missing_check_function_detected():
    ruleset = _simple_ruleset(_leaf("MISSING-001"))
    code = "def evaluate(profile): return {}\n_DISPATCH = {}\n_TREE = ('X', 'AND', [])\n"
    report = apply_guardrails(code, ruleset)
    assert not report.rule_coverage_ok
    assert "MISSING-001" in report.missing_functions


def test_evaluate_missing_detected():
    ruleset = _simple_ruleset(_leaf())
    code = generator_deterministic.generate(ruleset)
    # Remove the evaluate function
    code = code.replace("def evaluate(", "def _hidden_evaluate(")
    report = apply_guardrails(code, ruleset)
    assert not report.evaluate_exists
    assert not report.passed
