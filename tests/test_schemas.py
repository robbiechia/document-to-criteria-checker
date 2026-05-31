import pytest
from pydantic import ValidationError

from app.models.schemas import ConditionNode, ConditionType, LogicOperator, RuleSet


def test_leaf_node_requires_condition_type():
    with pytest.raises(ValidationError):
        ConditionNode(
            rule_id="TEST-001",
            description="Missing type",
            source_clause="The applicant must qualify.",
        )


def test_threshold_leaf_node():
    leaf = ConditionNode(
        rule_id="TEST-AGE",
        description="Age threshold",
        source_clause="The applicant must be at least 21 years old.",
        condition_type=ConditionType.THRESHOLD,
        field_required="buyer_age",
        operator=LogicOperator.GTE,
        threshold=21,
    )
    assert leaf.is_leaf
    assert not leaf.should_escalate


def test_or_intermediate_node():
    child_a = ConditionNode(
        rule_id="A",
        description="Family nucleus",
        source_clause="Option A",
        condition_type=ConditionType.MEMBERSHIP,
        field_required="nucleus_type",
        operator=LogicOperator.EQ,
        threshold="family",
    )
    child_b = ConditionNode(
        rule_id="B",
        description="Single with parents",
        source_clause="Option B",
        condition_type=ConditionType.MEMBERSHIP,
        field_required="nucleus_type",
        operator=LogicOperator.EQ,
        threshold="single_with_parents",
    )
    node = ConditionNode(
        rule_id="OR-001",
        description="Nucleus options",
        source_clause="Applicant must qualify under one of these schemes.",
        logic="OR",
        conditions=[child_a, child_b],
    )
    assert not node.is_leaf
    assert node.logic == "OR"


def test_discretionary_auto_escalates():
    leaf = ConditionNode(
        rule_id="DISCRETIONARY-001",
        description="HDB officer assessment required",
        source_clause="Subject to HDB's assessment.",
        condition_type=ConditionType.DISCRETIONARY,
    )
    assert leaf.should_escalate
    assert leaf.escalated  # auto-set by model_validator


def test_ruleset_validates():
    root = ConditionNode(
        rule_id="ROOT-CHILD",
        description="Age",
        source_clause="The applicant must be at least 21 years old.",
        condition_type=ConditionType.THRESHOLD,
        field_required="buyer_age",
        operator=LogicOperator.GTE,
        threshold=21,
    )
    ruleset = RuleSet(
        policy_name="Test policy",
        policy_version="2026",
        source_document="test.pdf",
        constraint_scenario="Test extraction",
        extraction_variant="direct",
        model_used="test-model",
        root=root,
    )
    assert ruleset.root.rule_id == "ROOT-CHILD"
