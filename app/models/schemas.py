from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


class ConditionType(str, Enum):
    THRESHOLD     = "threshold"
    MEMBERSHIP    = "membership"
    TEMPORAL      = "temporal"
    COMPUTATION   = "computation"
    COMPOSITE     = "composite"
    SEQUENTIAL    = "sequential"
    EXISTENCE     = "existence"       # row-exists check — codeable, escalates if field missing
    DISCRETIONARY = "discretionary"   # officer judgment — the ONLY non-codeable type


# Only discretionary is unconditionally escalated.
ESCALATED_TYPES = {
    ConditionType.DISCRETIONARY,
}

CODEABLE_TYPES = {
    ConditionType.THRESHOLD,
    ConditionType.MEMBERSHIP,
    ConditionType.TEMPORAL,
    ConditionType.COMPUTATION,
    ConditionType.COMPOSITE,
    ConditionType.SEQUENTIAL,
    ConditionType.EXISTENCE,
}


class LogicOperator(str, Enum):
    LTE = "lte"
    LT = "lt"
    GTE = "gte"
    GT = "gt"
    EQ = "eq"
    IN = "in"
    NOT_IN = "not_in"


class ConditionNode(BaseModel):
    """A node in the constraint tree: either a leaf condition or AND/OR node."""

    rule_id: str
    description: str
    source_clause: str

    logic: Literal["AND", "OR"] = "AND"
    conditions: list[ConditionNode] = Field(default_factory=list)

    condition_type: Optional[ConditionType] = None
    field_required: Optional[str] = None
    operator: Optional[LogicOperator] = None
    threshold: Optional[Union[float, int, str, bool, list[str]]] = None

    entitlement: Optional[str] = None      # e.g. "$80,000 Family Grant"
    escalation_note: Optional[str] = None  # free-text reason for escalation

    allow_example: Optional[dict] = None
    deny_example: Optional[dict] = None

    escalated: bool = False

    hallucination_risk: bool = False
    hallucination_note: Optional[str] = None

    @property
    def is_leaf(self) -> bool:
        return len(self.conditions) == 0

    @property
    def should_escalate(self) -> bool:
        """Return True only when this condition genuinely cannot generate executable code.

        Rules:
        - DISCRETIONARY type → always uncodeable (no evaluatable rule)
        - condition_type is None AND escalated=True → no type assigned, model flagged it
        - All other types (threshold, membership, temporal, existence, etc.)
          → codeable regardless of escalated flag. The model over-applies escalated=True;
          we generate code and flag hallucination_risk for review instead.
        """
        if self.condition_type == ConditionType.DISCRETIONARY:
            return True
        if self.condition_type is None and self.escalated:
            return True
        return False

    @model_validator(mode="after")
    def validate_node_shape(self) -> ConditionNode:
        if self.is_leaf and self.condition_type is None and not self.escalated:
            raise ValueError(
                f"Leaf node {self.rule_id} must have condition_type. "
                "If this condition cannot be evaluated, set escalated=true."
            )
        if not self.is_leaf and self.condition_type is not None:
            raise ValueError(
                f"Intermediate node {self.rule_id} must not have condition_type."
            )
        # Auto-set escalated for discretionary only — the one truly uncodeable type
        if self.condition_type == ConditionType.DISCRETIONARY and not self.escalated:
            self.escalated = True
        return self


class RuleSet(BaseModel):
    """Top-level output of Stage 1 extraction."""

    policy_name: str = "unknown"
    policy_version: str = "unknown"
    source_document: str = "unknown"
    extracted_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    constraint_scenario: str = ""

    extraction_variant: Literal[
        "direct", "direct_pdf", "direct_pdf_text",
        "cot", "cot_pdf", "cot_examples",
        "cot_v1", "cot_v2", "cot_v3", "cot_v4", "hybrid",
        "agentic", "agentic_pdf",
    ]
    model_used: str
    hints_used: bool = False
    chunking_strategy: Literal["full", "section", "sliding"] = "full"

    root: ConditionNode
    escalated_rules: list[ConditionNode] = Field(default_factory=list)
    cot_reasoning: Optional[str] = None
    hallucination_risk_count: int = 0


class Verdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


class RuleResult(BaseModel):
    verdict: Verdict
    rule_id: str
    clause: str
    reason: str
    condition_type: Optional[ConditionType] = None


class EvaluationResult(BaseModel):
    overall_verdict: Verdict
    results: list[RuleResult]
    escalated_rule_ids: list[str] = Field(default_factory=list)
    profile_id: Optional[str] = None
