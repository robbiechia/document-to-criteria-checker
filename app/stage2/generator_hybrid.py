"""Stage 2 hybrid code generator.

Three-pass pipeline:
  1. Schema interpretation — map extracted field names to user schema (LLM, optional)
  2. Deterministic skeleton — generate structurally correct code from templates
  3. LLM review — fix operator directions, formula stubs, schema substitution,
     OR tree logic, and existence check patterns

The LLM review pass is the only place schema-aware substitution and semantic
correctness checks happen. The deterministic pass guarantees all functions exist
and evaluate() is structurally correct.
"""

from __future__ import annotations

import app.config as _cfg
from dataclasses import dataclass, field
from typing import Optional

from app.models.schemas import RuleSet
from app.pipeline.prompts import (
    STAGE2_HYBRID_REVIEW_SYSTEM,
    STAGE2_HYBRID_REVIEW_USER_TEMPLATE,
)
from app.stage2 import generator_deterministic
from app.stage2.schema_interpreter import interpret as interpret_schema, apply_mapping
from app.utils.llm_client import CompletionResult, complete


@dataclass
class HybridResult:
    deterministic_code: str
    schema_mapping: dict[str, str]
    reviewed_code: str
    review_completion: CompletionResult
    changes_made: bool
    schema_substitutions: int = 0


def _collect_field_names(ruleset: RuleSet) -> list[str]:
    """Extract all distinct field_required values from the RuleSet."""
    names: list[str] = []

    def walk(node) -> None:
        if node.field_required and node.field_required not in names:
            names.append(node.field_required)
        for child in node.conditions:
            walk(child)

    walk(ruleset.root)
    for rule in ruleset.escalated_rules:
        walk(rule)
    return names


def generate(
    ruleset: RuleSet,
    user_schema: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: int = 16384,
) -> HybridResult:
    """Generate constraint code using the hybrid three-pass approach.

    Args:
        ruleset:     validated RuleSet from Stage 1
        user_schema: optional schema description (free text, JSON Schema, TypedDict, etc.)
                     If None, field names from the RuleSet are used verbatim.
        model:       LLM for the review pass (defaults to MODEL_STAGE2)
        max_tokens:  token budget for the review pass
    """
    model_name = model or _cfg.get("MODEL_STAGE2", "openai/gpt-5.4")

    # ── Pass 1: Schema interpretation ────────────────────────────────────────
    field_names = _collect_field_names(ruleset)
    schema_mapping = interpret_schema(field_names, user_schema, model=model_name)
    substitutions = sum(1 for k, v in schema_mapping.items() if k != v)

    # ── Pass 2: Deterministic skeleton ───────────────────────────────────────
    deterministic_code = generator_deterministic.generate(ruleset)

    # Apply schema field name substitutions from the mapping
    if substitutions > 0:
        deterministic_code = apply_mapping(deterministic_code, schema_mapping)

    # ── Pass 3: LLM review ───────────────────────────────────────────────────
    ruleset_json = ruleset.model_dump_json(indent=2)
    schema_context = (
        f"\nUser schema provided:\n{user_schema}\n\nField mapping applied: {schema_mapping}"
        if user_schema else "\nNo user schema provided — field names from RuleSet used verbatim."
    )
    user_message = STAGE2_HYBRID_REVIEW_USER_TEMPLATE.format(
        ruleset_json=ruleset_json + schema_context,
        deterministic_code=deterministic_code,
    )

    completion = complete(
        system=STAGE2_HYBRID_REVIEW_SYSTEM,
        user=user_message,
        model=model_name,
        max_tokens=max_tokens,
        temperature=0.0,
    )

    reviewed_code = completion.text
    changes_made = reviewed_code.strip() != deterministic_code.strip()

    return HybridResult(
        deterministic_code=deterministic_code,
        schema_mapping=schema_mapping,
        reviewed_code=reviewed_code,
        review_completion=completion,
        changes_made=changes_made,
        schema_substitutions=substitutions,
    )
