"""
Multi-step agentic extraction pipeline for Stage 1.

Each agent call has a single focused task. Output flows from one step to the next.
Different models are used where their strengths apply:
  Steps 1–3: MODEL_STAGE1 (Gemini Flash) — fast reading and reasoning
  Step 4:    MODEL_STAGE2 (GPT-4.1)      — independent validation from a different model family
  Step 5:    Deterministic               — RuleSet assembly and Pydantic validation

Step outputs are optionally saved to disk for debugging and manual inspection.
"""

from __future__ import annotations

import json
import os
import pathlib
import warnings
from dataclasses import dataclass, field
from typing import Any, Optional

from app.models.schemas import RuleSet
from app.pipeline.guardrails import apply_guardrail, apply_safety_gate
from app.pipeline.prompts.agentic import (
    AGENT_STEP1_SYSTEM,
    AGENT_STEP1_USER_TEMPLATE,
    AGENT_STEP2_SYSTEM,   # classify + build tree (collapsed)
    AGENT_STEP2_USER_TEMPLATE,
    AGENT_STEP3_SYSTEM,   # validate (was step 4)
    AGENT_STEP3_USER_TEMPLATE,
)
from app.pipeline.prompts.shared import RETRY_SCHEMA_ERROR_TEMPLATE
from app.utils.llm_client import CompletionResult, complete, complete_with_pdf
import app.config as _cfg


@dataclass
class StepResult:
    step: int
    name: str
    model: str
    data: dict[str, Any]
    completion: CompletionResult


@dataclass
class AgenticResult:
    ruleset: RuleSet
    steps: list[StepResult] = field(default_factory=list)

    @property
    def total_input_tokens(self) -> int:
        return sum(s.completion.input_tokens for s in self.steps)

    @property
    def total_output_tokens(self) -> int:
        return sum(s.completion.output_tokens for s in self.steps)

    @property
    def total_latency_ms(self) -> float:
        return sum(s.completion.latency_ms for s in self.steps)

    def step_summary(self) -> list[dict]:
        return [
            {
                "step": s.step,
                "name": s.name,
                "model": s.model,
                "input_tokens": s.completion.input_tokens,
                "output_tokens": s.completion.output_tokens,
                "latency_ms": round(s.completion.latency_ms),
            }
            for s in self.steps
        ]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_step(
    step_num: int,
    name: str,
    system: str,
    user: str,
    model: str,
    max_retries: int = 2,
    save_path: Optional[pathlib.Path] = None,
) -> tuple[dict[str, Any], CompletionResult]:
    """Call the LLM, parse JSON output, retry on failure."""
    last_exc: Exception | None = None
    retry_user = user

    for attempt in range(max_retries + 1):
        completion = complete(system=system, user=retry_user, model=model, temperature=0.0)
        try:
            result = json.loads(completion.text)
        except json.JSONDecodeError as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            retry_user = (
                f"Your previous response was not valid JSON.\n"
                f"Error: {exc}\n\n"
                f"Please output only valid JSON as described.\n"
                f"Original task (abbreviated):\n{user[:600]}"
            )
            continue

        if save_path is not None:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

        return result, completion

    raise RuntimeError(
        f"Agent step {step_num} '{name}' failed JSON parsing "
        f"after {max_retries + 1} attempts: {last_exc}"
    )


def _patch_node(node: dict) -> None:
    """Minimal normalisation to make agent-produced nodes pass Pydantic validation."""
    if not isinstance(node, dict):
        return
    if not node.get("source_clause"):
        node["source_clause"] = node.get("description", node.get("rule_id", ""))
    if not node.get("description"):
        node["description"] = node.get("source_clause", node.get("rule_id", ""))
    if not node.get("rule_id"):
        node["rule_id"] = f"AUTO-{id(node)}"
    # Intermediate node with no children and no condition_type → escalate
    if not node.get("conditions") and not node.get("condition_type") and not node.get("escalated"):
        node["escalated"] = True
    # Intermediate node with children must not have condition_type
    if node.get("conditions") and node.get("condition_type"):
        node.pop("condition_type", None)
    # Remap legacy and deprecated condition types — must match extractor._normalize_node
    _REMAP = {
        "history":          "existence",   # old name
        "spatial":          "membership",  # old name — checkable as membership
        "composite":        "membership",  # old name — checkable as membership
        "more_info_needed": "membership",  # deprecated — all conditions are codeable
    }
    ct = node.get("condition_type")
    if ct in _REMAP:
        node["condition_type"] = _REMAP[ct]
        if ct != "history":
            node["escalated"] = False  # remapped types are not auto-escalated
    # Drop invalid operators — Pydantic will reject unknown values
    _VALID_OPS = {"lte", "lt", "gte", "gt", "eq", "in", "not_in"}
    if node.get("operator") and node["operator"] not in _VALID_OPS:
        node.pop("operator", None)
    # Filter non-dict children
    children = [c for c in node.get("conditions", []) if isinstance(c, dict)]
    node["conditions"] = children
    for child in children:
        _patch_node(child)


def _assemble_ruleset(
    tree_data: dict[str, Any],
    policy_name: str,
    source_document: str,
    constraint_scenario: str,
    model_used: str,
    step_models: dict[str, str],
) -> RuleSet:
    """Wrap the validated tree data in a full RuleSet and run Pydantic validation."""
    root = tree_data.get("root", {})
    escalated = [r for r in tree_data.get("escalated_rules", []) if isinstance(r, dict)]
    _patch_node(root)
    for rule in escalated:
        _patch_node(rule)

    payload = {
        "policy_name": policy_name,
        "policy_version": "extracted",
        "source_document": source_document,
        "constraint_scenario": constraint_scenario,
        "extraction_variant": "agentic",
        "model_used": f"agentic:{model_used}",
        "hints_used": False,
        "chunking_strategy": "full",
        "root": root,
        "escalated_rules": escalated,
        "cot_reasoning": json.dumps(step_models),
    }

    for attempt in range(3):
        try:
            return RuleSet.model_validate(payload)
        except Exception as exc:
            if attempt >= 2:
                raise RuntimeError(
                    f"RuleSet assembly failed after {attempt + 1} attempts: {exc}"
                ) from exc
            # Surface the Pydantic error back to the validator step on retry
            warnings.warn(
                f"Assembly attempt {attempt + 1} failed Pydantic validation: {exc}",
                UserWarning,
            )
            # Try to fix the root node shape if it's clearly malformed
            if "root" in str(exc) and payload["root"]:
                root = payload["root"]
                if "conditions" not in root:
                    root["conditions"] = []
                if "logic" not in root:
                    root["logic"] = "AND"

    raise RuntimeError("RuleSet assembly failed after all retries")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

MIN_EXPECTED_CONDITIONS = int(_cfg.get("MIN_EXPECTED_CONDITIONS", 8))


def run(
    document_text: str,
    raw_document_text: str,
    constraint_scenario: str,
    source_document: str,
    model_step1: Optional[str] = None,
    model_step3: Optional[str] = None,
    save_dir: Optional[str] = None,
) -> AgenticResult:
    """
    Run the 3-LLM-step agentic extraction pipeline.

    Steps 1–2 use MODEL_STAGE1 (Gemini); Step 3 uses MODEL_STAGE2 (GPT-5.4).

    Args:
        document_text:       formatted document text
        raw_document_text:   plain text for guardrail source-clause verification
        constraint_scenario: what to extract
        source_document:     filename of source PDF
        model_step1:         model for steps 1–2 (defaults to MODEL_STAGE1)
        model_step3:         model for step 3 validation (defaults to MODEL_STAGE2)
        save_dir:            if set, intermediate JSON files are written here
    """
    m1 = model_step1 or _cfg.get("MODEL_STAGE1", "google/gemini-3.5-flash")
    m3 = model_step3 or _cfg.get("MODEL_STAGE2", "openai/gpt-5.4")

    out = pathlib.Path(save_dir) if save_dir else None
    steps: list[StepResult] = []

    # ── Step 1: Enumerate ────────────────────────────────────────────────────
    print("  [Agent 1/3] Enumerating conditions…")
    s1_user = AGENT_STEP1_USER_TEMPLATE.format(
        document_text=document_text,
        constraint_scenario=constraint_scenario,
    )
    s1_data, s1_comp = _run_step(
        1, "enumerate", AGENT_STEP1_SYSTEM, s1_user, m1,
        save_path=out / "step1_candidates.json" if out else None,
    )
    steps.append(StepResult(1, "enumerate", m1, s1_data, s1_comp))

    candidates = s1_data.get("candidates", [])
    print(f"     → {len(candidates)} candidates found")
    if len(candidates) < MIN_EXPECTED_CONDITIONS:
        warnings.warn(
            f"Step 1 found only {len(candidates)} candidates "
            f"(expected >= {MIN_EXPECTED_CONDITIONS}).",
            UserWarning,
        )

    # ── Step 2: Classify + build tree (collapsed) ─────────────────────────────
    print("  [Agent 2/3] Classifying and building tree…")
    s2_user = AGENT_STEP2_USER_TEMPLATE.format(
        document_text=document_text,
        candidates_json=json.dumps(candidates, indent=2),
    )
    s2_data, s2_comp = _run_step(
        2, "classify_tree", AGENT_STEP2_SYSTEM, s2_user, m1,
        save_path=out / "step2_tree.json" if out else None,
    )
    steps.append(StepResult(2, "classify_tree", m1, s2_data, s2_comp))

    escalated_count = len(s2_data.get("escalated_rules", []))
    print(f"     → tree built, {escalated_count} escalated rules")

    # ── Step 3: Validate (GPT-5.4) ───────────────────────────────────────────
    print(f"  [Agent 3/3] Validating with {m3}…")
    s3_user = AGENT_STEP3_USER_TEMPLATE.format(
        document_text=document_text,
        tree_json=json.dumps(s2_data, indent=2),
    )
    s3_data, s3_comp = _run_step(
        3, "validate", AGENT_STEP3_SYSTEM, s3_user, m3,
        save_path=out / "step3_validated.json" if out else None,
    )
    steps.append(StepResult(3, "validate", m3, s3_data, s3_comp))
    print("     → validation complete")

    # ── Step 4: Assemble RuleSet (deterministic) ─────────────────────────────
    print("  [Step 4/4] Assembling RuleSet…")
    policy_name = pathlib.Path(source_document).stem.replace("_", " ").title()
    ruleset = _assemble_ruleset(
        tree_data=s3_data,
        policy_name=policy_name,
        source_document=source_document,
        constraint_scenario=constraint_scenario,
        model_used=m1,
        step_models={"steps_1_2": m1, "step_3": m3},
    )

    # Count leaf conditions for partial-extraction warning
    def _count_leaves(node) -> int:
        if not node.conditions:
            return 1
        return sum(_count_leaves(c) for c in node.conditions)

    leaf_count = _count_leaves(ruleset.root)
    if leaf_count < MIN_EXPECTED_CONDITIONS:
        warnings.warn(
            f"Only {leaf_count} executable conditions in final RuleSet "
            f"(expected >= {MIN_EXPECTED_CONDITIONS}).",
            UserWarning,
        )

    # ── Guardrails ────────────────────────────────────────────────────────────
    ruleset = apply_guardrail(ruleset, raw_document_text, is_image=False)
    ruleset = apply_safety_gate(ruleset)

    return AgenticResult(ruleset=ruleset, steps=steps)


def run_pdf(
    pdf_path: str,
    raw_document_text: str,
    constraint_scenario: str,
    source_document: str,
    model_step1: Optional[str] = None,
    model_step3: Optional[str] = None,
    save_dir: Optional[str] = None,
) -> AgenticResult:
    """Agentic pipeline where Step 1 reads the PDF natively (no pdfplumber text).
    Steps 2 and 3 operate on the enumerated candidates as normal.
    """
    m1 = model_step1 or _cfg.get("MODEL_STAGE1", "google/gemini-3.5-flash")
    m3 = model_step3 or _cfg.get("MODEL_STAGE2", "openai/gpt-5.4")
    max_tokens = int(_cfg.get("STAGE1_MAX_TOKENS", 32768))

    out = pathlib.Path(save_dir) if save_dir else None
    steps: list[StepResult] = []

    # ── Step 1: Enumerate from raw PDF ───────────────────────────────────────
    print("  [Agent 1/3] Enumerating from PDF…")
    s1_user = AGENT_STEP1_USER_TEMPLATE.format(
        document_text="[See attached PDF]",
        constraint_scenario=constraint_scenario,
    )
    # Use PDF-capable completion for Step 1 only
    s1_comp = complete_with_pdf(
        system=AGENT_STEP1_SYSTEM,
        user=s1_user,
        pdf_path=pdf_path,
        model=m1,
        max_tokens=max_tokens,
        temperature=0.0,
    )
    try:
        s1_data = json.loads(s1_comp.text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Step 1 PDF enumeration failed JSON parse: {exc}") from exc

    if out:
        (out / "step1_candidates.json").parent.mkdir(parents=True, exist_ok=True)
        (out / "step1_candidates.json").write_text(json.dumps(s1_data, indent=2))

    steps.append(StepResult(1, "enumerate_pdf", m1, s1_data, s1_comp))
    candidates = s1_data.get("candidates", [])
    print(f"     → {len(candidates)} candidates found")

    # Steps 2 and 3 are identical to the text pipeline
    # Pass document text as empty since candidates already extracted
    _dummy_text = "(candidates provided from Step 1)"

    # ── Step 2: Classify + build tree ────────────────────────────────────────
    print("  [Agent 2/3] Classifying and building tree…")
    s2_user = AGENT_STEP2_USER_TEMPLATE.format(
        document_text=_dummy_text,
        candidates_json=json.dumps(candidates, indent=2),
    )
    s2_data, s2_comp = _run_step(
        2, "classify_tree", AGENT_STEP2_SYSTEM, s2_user, m1,
        save_path=out / "step2_tree.json" if out else None,
    )
    steps.append(StepResult(2, "classify_tree", m1, s2_data, s2_comp))
    print(f"     → tree built, {len(s2_data.get('escalated_rules', []))} escalated rules")

    # ── Step 3: Validate ─────────────────────────────────────────────────────
    print(f"  [Agent 3/3] Validating with {m3}…")
    s3_user = AGENT_STEP3_USER_TEMPLATE.format(
        document_text=_dummy_text,
        tree_json=json.dumps(s2_data, indent=2),
    )
    s3_data, s3_comp = _run_step(
        3, "validate", AGENT_STEP3_SYSTEM, s3_user, m3,
        save_path=out / "step3_validated.json" if out else None,
    )
    steps.append(StepResult(3, "validate", m3, s3_data, s3_comp))
    print("     → validation complete")

    # ── Step 4: Assemble ─────────────────────────────────────────────────────
    print("  [Step 4/4] Assembling RuleSet…")
    policy_name = pathlib.Path(source_document).stem.replace("_", " ").title()
    ruleset = _assemble_ruleset(
        tree_data=s3_data,
        policy_name=policy_name,
        source_document=source_document,
        constraint_scenario=constraint_scenario,
        model_used=m1,
        step_models={"steps_1_2": m1, "step_3": m3},
    )

    def _count_leaves(node) -> int:
        if not node.conditions:
            return 1
        return sum(_count_leaves(c) for c in node.conditions)

    if _count_leaves(ruleset.root) < MIN_EXPECTED_CONDITIONS:
        warnings.warn(
            f"Only {_count_leaves(ruleset.root)} executable conditions extracted.",
            UserWarning,
        )

    ruleset = apply_guardrail(ruleset, raw_document_text, is_image=False)
    ruleset = apply_safety_gate(ruleset)
    return AgenticResult(ruleset=ruleset, steps=steps)
