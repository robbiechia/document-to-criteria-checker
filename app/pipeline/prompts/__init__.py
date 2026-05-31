"""Stage 1 and Stage 2 prompts package.

Extraction variants:
  direct        — baseline, single LLM call, no reasoning scaffolding
  cot           — chain-of-thought: scope → enumerate → escalation → classify → JSON
  cot_examples  — same CoT structure with concrete HDB classification examples
  agentic       — 4-step pipeline (enumerate → classify → build tree → validate)

All names are re-exported here so existing imports work unchanged.
"""

from app.pipeline.prompts.direct import STAGE1_SYSTEM_DIRECT
from app.pipeline.prompts.cot import STAGE1_SYSTEM_COT
from app.pipeline.prompts.cot_examples import STAGE1_SYSTEM_COT_EXAMPLES
from app.pipeline.prompts.agentic import (  # noqa: F401
    AGENT_STEP1_SYSTEM,
    AGENT_STEP2_SYSTEM,
    AGENT_STEP3_SYSTEM,
)
from app.pipeline.prompts.shared import (
    STAGE1_USER_TEMPLATE,
    STAGE1_USER_TEMPLATE_PDF,
    STAGE1_USER_TEMPLATE_PDF_AND_TEXT,
    RETRY_JSON_ERROR_TEMPLATE,
    RETRY_SCHEMA_ERROR_TEMPLATE,
    STAGE2_LLM_SYSTEM,
    STAGE2_LLM_USER_TEMPLATE,
    STAGE2_HYBRID_REVIEW_SYSTEM,
    STAGE2_HYBRID_REVIEW_USER_TEMPLATE,
    STAGE2_FLOWCHART_SYSTEM,
    STAGE2_FLOWCHART_USER_TEMPLATE,
    STAGE2_TEST_JUDGE_SYSTEM,
    STAGE2_TEST_JUDGE_USER_TEMPLATE,
)

__all__ = [
    "STAGE1_SYSTEM_DIRECT",
    "STAGE1_SYSTEM_COT",
    "STAGE1_SYSTEM_COT_EXAMPLES",
    "AGENT_STEP1_SYSTEM",
    "AGENT_STEP2_SYSTEM",
    "AGENT_STEP3_SYSTEM",
    "STAGE1_USER_TEMPLATE",
    "STAGE1_USER_TEMPLATE_PDF",
    "STAGE1_USER_TEMPLATE_PDF_AND_TEXT",
    "RETRY_JSON_ERROR_TEMPLATE",
    "RETRY_SCHEMA_ERROR_TEMPLATE",
    "STAGE2_LLM_SYSTEM",
    "STAGE2_LLM_USER_TEMPLATE",
    "STAGE2_HYBRID_REVIEW_SYSTEM",
    "STAGE2_HYBRID_REVIEW_USER_TEMPLATE",
    "STAGE2_FLOWCHART_SYSTEM",
    "STAGE2_FLOWCHART_USER_TEMPLATE",
    "STAGE2_TEST_JUDGE_SYSTEM",
    "STAGE2_TEST_JUDGE_USER_TEMPLATE",
]
