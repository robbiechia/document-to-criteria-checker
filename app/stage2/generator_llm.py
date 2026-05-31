"""Stage 2 LLM-based code generator.

Sends the RuleSet JSON directly to the LLM and asks it to produce Python
constraint functions following the same structure as the deterministic generator.
"""

from __future__ import annotations

import os
import app.config as _cfg
from typing import Optional

from app.models.schemas import RuleSet
from app.pipeline.prompts import (
    STAGE2_LLM_SYSTEM,
    STAGE2_LLM_USER_TEMPLATE,
)
from app.utils.llm_client import CompletionResult, complete


def generate(
    ruleset: RuleSet,
    model: Optional[str] = None,
    max_tokens: int = 8192,
) -> tuple[str, CompletionResult]:
    """Generate Python constraint code via LLM from a validated RuleSet.

    Returns (code_str, completion_metadata).
    """
    model_name = model or _cfg.get("MODEL_STAGE2", "openai/gpt-4.1")
    ruleset_json = ruleset.model_dump_json(indent=2)

    user_message = STAGE2_LLM_USER_TEMPLATE.format(
        ruleset_json=ruleset_json,
        constraint_scenario=ruleset.constraint_scenario,
    )

    completion = complete(
        system=STAGE2_LLM_SYSTEM,
        user=user_message,
        model=model_name,
        max_tokens=max_tokens,
        temperature=0.0,
    )

    return completion.text, completion
