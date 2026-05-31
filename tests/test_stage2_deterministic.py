"""Tests for Stage 2 deterministic code generator."""

import ast
import pytest

from app.models.schemas import (
    ConditionNode,
    ConditionType,
    LogicOperator,
    RuleSet,
)
from app.stage2 import generator_deterministic


def _simple_ruleset(root: ConditionNode) -> RuleSet:
    return RuleSet(
        policy_name="Test Policy",
        policy_version="2026",
        source_document="test.pdf",
        constraint_scenario="Test extraction",
        extraction_variant="direct",
        model_used="test",
        root=root,
    )


def _age_leaf() -> ConditionNode:
    return ConditionNode(
        rule_id="TEST-AGE-001",
        description="Buyer must be at least 21",
        source_clause="The buyer must be at least 21 years old.",
        condition_type=ConditionType.THRESHOLD,
        field_required="buyer_age",
        operator=LogicOperator.GTE,
        threshold=21,
    )


def _citizenship_leaf() -> ConditionNode:
    return ConditionNode(
        rule_id="TEST-CITIZEN-001",
        description="Must be SC or SPR",
        source_clause="Applicant must be a Singapore Citizen or SPR.",
        condition_type=ConditionType.MEMBERSHIP,
        field_required="buyer_citizenship",
        operator=LogicOperator.IN,
        threshold=["SC", "SPR"],
    )


def _existence_leaf() -> ConditionNode:
    return ConditionNode(
        rule_id="TEST-HISTORY-001",
        description="Must be first-timer — no prior grant records",
        source_clause="Applicant must not have received prior grants.",
        condition_type=ConditionType.EXISTENCE,
        escalated=True,
        escalation_note="requires existence check against HDB grant history records",
        field_required="first_timer",
    )


def test_generates_valid_python_for_threshold():
    ruleset = _simple_ruleset(_age_leaf())
    code = generator_deterministic.generate(ruleset)
    ast.parse(code)  # must not raise


def test_generates_check_function_for_threshold():
    ruleset = _simple_ruleset(_age_leaf())
    code = generator_deterministic.generate(ruleset)
    assert "def check_test_age_001(" in code
    assert "buyer_age" in code
    assert ">= 21" in code


def test_generates_valid_python_for_membership():
    ruleset = _simple_ruleset(_citizenship_leaf())
    code = generator_deterministic.generate(ruleset)
    ast.parse(code)
    assert "def check_test_citizen_001(" in code
    assert "buyer_citizenship" in code
    assert "SC" in code
    assert "SPR" in code


def test_escalation_stub_generated():
    leaf = _existence_leaf()
    ruleset = RuleSet(
        policy_name="Test",
        policy_version="2026",
        source_document="test.pdf",
        constraint_scenario="Test",
        extraction_variant="direct",
        model_used="test",
        root=ConditionNode(
            rule_id="ROOT",
            description="Root",
            source_clause="Root clause",
            logic="AND",
            conditions=[],
            escalated=True,
            escalation_note="all conditions require escalation",
        ),
        escalated_rules=[leaf],
    )
    code = generator_deterministic.generate(ruleset)
    ast.parse(code)
    assert "def escalate_test_history_001(" in code
    assert "escalate" in code


def test_evaluate_function_exists():
    ruleset = _simple_ruleset(_age_leaf())
    code = generator_deterministic.generate(ruleset)
    assert "def evaluate(" in code


def test_dispatch_table_populated():
    ruleset = _simple_ruleset(_age_leaf())
    code = generator_deterministic.generate(ruleset)
    assert "_DISPATCH" in code
    assert "TEST-AGE-001" in code


def test_tree_constant_present():
    ruleset = _simple_ruleset(_age_leaf())
    code = generator_deterministic.generate(ruleset)
    assert "_TREE" in code


def test_or_node_preserved():
    child_a = _age_leaf()
    child_b = _citizenship_leaf()
    or_node = ConditionNode(
        rule_id="OR-ROOT",
        description="Age or citizenship",
        source_clause="One of the following must apply.",
        logic="OR",
        conditions=[child_a, child_b],
    )
    ruleset = _simple_ruleset(or_node)
    code = generator_deterministic.generate(ruleset)
    ast.parse(code)
    # OR logic should appear in the tree encoding
    assert "'OR'" in code or '"OR"' in code


def test_evaluate_returns_allow_for_valid_profile():
    ruleset = _simple_ruleset(_age_leaf())
    code = generator_deterministic.generate(ruleset)
    namespace: dict = {}
    exec(compile(code, "<generated>", "exec"), namespace)
    evaluate = namespace["evaluate"]
    result = evaluate({"buyer_age": 25})
    assert result["overall_verdict"] == "allow"


def test_evaluate_returns_deny_for_invalid_profile():
    ruleset = _simple_ruleset(_age_leaf())
    code = generator_deterministic.generate(ruleset)
    namespace: dict = {}
    exec(compile(code, "<generated>", "exec"), namespace)
    evaluate = namespace["evaluate"]
    result = evaluate({"buyer_age": 18})
    assert result["overall_verdict"] == "deny"


def test_evaluate_returns_escalate_for_missing_field():
    ruleset = _simple_ruleset(_age_leaf())
    code = generator_deterministic.generate(ruleset)
    namespace: dict = {}
    exec(compile(code, "<generated>", "exec"), namespace)
    evaluate = namespace["evaluate"]
    result = evaluate({})
    assert result["overall_verdict"] == "escalate"
