"""Stage 1 extraction: policy document to validated RuleSet JSON.

Two code paths:
  Single-call variants (direct, cot_v1, cot_v2, cot_v3, hybrid)
    → one LLM call, full document in context
  Agentic variant
    → 4 focused LLM calls (enumerate → classify → build tree → validate)
      dispatched through agent_pipeline.run()
"""

from __future__ import annotations

import json
import os
import pathlib
import warnings
from typing import Optional, Union

from app.models.schemas import RuleSet
from app.pipeline.guardrails import apply_guardrail, apply_safety_gate
from app.pipeline.pdf_parser import (
    extract_chunks,
    extract_hints,
    filter_obligation_chunks,
    format_for_llm,
)
from app.pipeline.prompts import (
    RETRY_JSON_ERROR_TEMPLATE,
    RETRY_SCHEMA_ERROR_TEMPLATE,
    STAGE1_SYSTEM_COT,
    STAGE1_SYSTEM_COT_EXAMPLES,
    STAGE1_SYSTEM_DIRECT,
    STAGE1_USER_TEMPLATE,
    STAGE1_USER_TEMPLATE_PDF,
    STAGE1_USER_TEMPLATE_PDF_AND_TEXT,
)
from app.utils.llm_client import (
    CompletionResult,
    complete,
    complete_with_pdf,
    complete_with_pdf_and_text,
)
import app.config as _cfg

SINGLE_CALL_VARIANTS = {
    "direct":        STAGE1_SYSTEM_DIRECT,
    "cot":           STAGE1_SYSTEM_COT,
    "cot_examples":  STAGE1_SYSTEM_COT_EXAMPLES,
}

# direct_pdf passes the raw PDF file to Gemini instead of pdfplumber text
# direct_pdf_text passes both pdfplumber text AND the raw PDF file
# cot_pdf: CoT system prompt + native PDF (no pdfplumber text)
ALL_VARIANTS = set(SINGLE_CALL_VARIANTS) | {"agentic", "agentic_pdf", "direct_pdf", "direct_pdf_text", "cot_pdf"}

MIN_EXPECTED_CONDITIONS = int(_cfg.get("MIN_EXPECTED_CONDITIONS", 8))
STAGE1_MAX_TOKENS = int(_cfg.get("STAGE1_MAX_TOKENS", 32768))


def _count_leaf_conditions(ruleset: RuleSet) -> int:
    def count(node) -> int:
        if node.is_leaf:
            return 1
        return sum(count(child) for child in node.conditions)
    return count(ruleset.root)


_NODE_FIELD_ALIASES = {
    "name": "rule_id",
    "id": "rule_id",
    "clause": "source_clause",
    "text": "source_clause",
    "verbatim": "source_clause",
    "type": "condition_type",
    "value": "threshold",
    "field": "field_required",
    "note": "escalation_note",
}


def _normalize_node(node: dict, _counter: list | None = None) -> None:
    """Fill missing required fields and remap common field aliases on nodes."""
    if not node:
        return
    if _counter is None:
        _counter = [0]

    # Remap field aliases
    for alias, canonical in _NODE_FIELD_ALIASES.items():
        if alias in node and canonical not in node:
            node[canonical] = node.pop(alias)
    # Remap alternative children keys to "conditions"
    for children_alias in ("all", "any", "rules", "criteria", "items", "children"):
        if children_alias in node and "conditions" not in node:
            node["conditions"] = node.pop(children_alias)
            break

    # Synthesise rule_id if still missing
    if not node.get("rule_id"):
        _counter[0] += 1
        node["rule_id"] = f"AUTO-{_counter[0]:03d}"

    if not node.get("description"):
        node["description"] = node.get("source_clause", node.get("rule_id", ""))
    if not node.get("source_clause"):
        node["source_clause"] = node.get("description", "")
    # escalation_reason is no longer in the schema — move to escalation_note
    old_reason = node.pop("escalation_reason", None)
    if old_reason and not node.get("escalation_note"):
        node["escalation_note"] = str(old_reason)
    # ambiguity fields are no longer in the schema — drop silently
    node.pop("ambiguity_flag", None)
    node.pop("ambiguity_note", None)
    node.pop("interpretation_assumed", None)
    # Remap legacy condition types to current schema
    _TYPE_REMAP = {
        "history": "existence",
        "spatial": "more_info_needed",
        "composite": "more_info_needed",
    }
    ct = node.get("condition_type")
    if ct in _TYPE_REMAP:
        node["condition_type"] = _TYPE_REMAP[ct]
        if not node.get("escalated"):
            node["escalated"] = True
        if not node.get("escalation_note"):
            node["escalation_note"] = f"remapped from legacy type '{ct}'"

    # Intermediate nodes must not have condition_type; leaf nodes without one → escalate
    if node.get("conditions") and node.get("condition_type"):
        node.pop("condition_type", None)
    if not node.get("conditions") and not node.get("condition_type") and not node.get("escalated"):
        node["escalated"] = True
        if not node.get("escalation_note"):
            node["escalation_note"] = "condition_type missing — cannot evaluate"
    # Drop invalid operators
    _VALID_OPS = {"lte", "lt", "gte", "gt", "eq", "in", "not_in"}
    if node.get("operator") and node["operator"] not in _VALID_OPS:
        node.pop("operator", None)
    # threshold must be scalar/list — if model returned a dict, stringify it
    threshold = node.get("threshold")
    if isinstance(threshold, dict):
        node["threshold"] = str(threshold)
    # Filter out non-dict children and recurse on valid ones
    valid_children = [c for c in node.get("conditions", []) if isinstance(c, dict)]
    if len(valid_children) != len(node.get("conditions", [])):
        node["conditions"] = valid_children
    for child in valid_children:
        _normalize_node(child, _counter)


def _patch_source_clauses(node: dict) -> dict:
    """Fill missing or empty source_clause on any node from its description.

    Models sometimes omit source_clause on the ROOT container or intermediate nodes.
    Filling from description allows validation to pass; the guardrail will then flag
    any leaf whose source_clause doesn't fuzzy-match the actual document text.
    """
    if not node:
        return node
    if not node.get("source_clause"):
        node["source_clause"] = node.get("description", "")
    for child in node.get("conditions", []):
        _patch_source_clauses(child)
    return node


def _parse_and_validate(
    raw: str,
    user_message: str,
    model: str,
    variant: str,
    model_used: str,
    hints_used: bool,
    chunking: str,
    max_retries: int = 1,
) -> tuple[RuleSet, int]:
    retry_count = 0
    current_raw = raw
    system = SINGLE_CALL_VARIANTS.get(variant, STAGE1_SYSTEM_DIRECT)

    for attempt in range(max_retries + 1):
        try:
            data = json.loads(current_raw)
            # Unwrap if model added an outer key e.g. {"RuleSet": {...}} or {"ruleset": {...}}
            if isinstance(data, dict) and len(data) == 1:
                outer_key = next(iter(data))
                if outer_key.lower() in ("ruleset", "rule_set", "result", "output"):
                    data = data[outer_key]
            # Remap common field name variants to schema field names
            if isinstance(data, dict):
                _FIELD_ALIASES = {
                    "name": "policy_name",
                    "version": "policy_version",
                    "document": "source_document",
                    "scenario": "constraint_scenario",
                }
                for alias, canonical in _FIELD_ALIASES.items():
                    if alias in data and canonical not in data:
                        data[canonical] = data.pop(alias)
        except json.JSONDecodeError as exc:
            if attempt >= max_retries:
                raise RuntimeError(
                    f"JSON parse failed after {attempt + 1} attempts: {exc}"
                ) from exc
            retry_count += 1
            current_raw = complete(
                system=system,
                user=RETRY_JSON_ERROR_TEMPLATE.format(
                    error=str(exc),
                    previous_response_start=current_raw[:500],
                    original_user_message=user_message[:1000],
                ),
                model=model,
            ).text
            continue

        try:
            data["extraction_variant"] = variant
            data["model_used"] = model_used
            data["hints_used"] = hints_used
            data["chunking_strategy"] = chunking
            # If root is missing but top-level conditions exist, wrap them
            if "root" not in data and "conditions" in data:
                data["root"] = {
                    "rule_id": "ROOT",
                    "description": data.get("policy_name", "Eligibility conditions"),
                    "source_clause": data.get("policy_name", "Eligibility conditions"),
                    "logic": "AND",
                    "conditions": data.pop("conditions"),
                }
            if "root" in data:
                _normalize_node(data["root"])
            # Filter non-dict entries from escalated_rules (model sometimes puts rule_id strings)
            if "escalated_rules" in data:
                data["escalated_rules"] = [
                    r for r in data["escalated_rules"] if isinstance(r, dict)
                ]
            for rule in data.get("escalated_rules", []):
                _normalize_node(rule)
            return RuleSet.model_validate(data), retry_count
        except Exception as exc:
            if attempt >= max_retries:
                raise RuntimeError(
                    f"Schema validation failed after {attempt + 1} attempts: {exc}"
                ) from exc
            retry_count += 1
            current_raw = complete(
                system=system,
                user=RETRY_SCHEMA_ERROR_TEMPLATE.format(
                    pydantic_error=str(exc)[:1000],
                    original_user_message=user_message[:1000],
                ),
                model=model,
            ).text

    raise RuntimeError("Extraction failed after all retries")


def _prepare_document(
    pdf_path: str,
    use_hints: bool,
    variant: str,
) -> tuple[str, str]:
    """Return (formatted_doc_text, raw_doc_text)."""
    chunks = extract_chunks(pdf_path)
    filtered = filter_obligation_chunks(chunks)
    effective_hints = use_hints
    hints = extract_hints(filtered) if effective_hints else None
    formatted = format_for_llm(filtered, hints)
    raw = "\n".join(c.text for c in filtered)
    if len(formatted) > 120_000:
        formatted = formatted[:120_000] + "\n[TRUNCATED]"
    return formatted, raw


def extract_rules(
    pdf_path: str,
    constraint_scenario: Optional[str] = None,
    variant: str = "direct",
    model: Optional[str] = None,
    use_hints: bool = False,
    chunking: str = "full",
    save_raw: Optional[str] = None,
) -> tuple[RuleSet, Union[CompletionResult, list]]:
    """Run Stage 1 extraction and return (RuleSet, completion_metadata).

    For single-call variants, completion_metadata is a CompletionResult.
    For the agentic variant, it is a list of per-step dicts.
    """
    if variant not in ALL_VARIANTS:
        raise ValueError(
            f"Unknown extraction variant: {variant!r}. "
            f"Choose from: {sorted(ALL_VARIANTS)}"
        )
    if chunking not in {"full", "section"}:
        raise ValueError(f"Unknown chunking strategy: {chunking!r}")

    scenario = (
        constraint_scenario
        or "Extract all eligibility conditions from this policy document."
    )
    document_text, raw_document_text = _prepare_document(pdf_path, use_hints, variant)

    # ── Agentic paths ─────────────────────────────────────────────────────────
    if variant in ("agentic", "agentic_pdf"):
        save_dir = str(pathlib.Path(save_raw).parent) if save_raw else None
        if variant == "agentic_pdf":
            from app.pipeline.agent_pipeline import run_pdf as _run_agentic
            result = _run_agentic(
                pdf_path=pdf_path,
                raw_document_text=raw_document_text,
                constraint_scenario=scenario,
                source_document=pathlib.Path(pdf_path).name,
                model_step1=model,
                save_dir=save_dir,
            )
        else:
            from app.pipeline.agent_pipeline import run as _run_agentic
            result = _run_agentic(
                document_text=document_text,
                raw_document_text=raw_document_text,
                constraint_scenario=scenario,
                source_document=pathlib.Path(pdf_path).name,
                model_step1=model,
                save_dir=save_dir,
            )
        return result.ruleset, result.step_summary()

    # ── Native PDF paths (Gemini reads the file directly) ────────────────────
    if variant in ("direct_pdf", "cot_pdf"):
        model_name = model or _cfg.get("MODEL_STAGE1", "google/gemini-2.5-flash")
        user_message = STAGE1_USER_TEMPLATE_PDF.format(constraint_scenario=scenario)
        system_prompt = STAGE1_SYSTEM_COT if variant == "cot_pdf" else STAGE1_SYSTEM_DIRECT

        completion = complete_with_pdf(
            system=system_prompt,
            user=user_message,
            pdf_path=pdf_path,
            model=model_name,
            max_tokens=STAGE1_MAX_TOKENS,
            temperature=0.0,
        )

        if save_raw:
            pathlib.Path(save_raw).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(save_raw).write_text(completion.text, encoding="utf-8")

        # Use pdfplumber text only for the guardrail source-clause check
        _, raw_document_text = _prepare_document(pdf_path, use_hints=False, variant="direct")

        ruleset, _ = _parse_and_validate(
            raw=completion.text,
            user_message=user_message,
            model=model_name,
            variant=variant,  # "direct_pdf" or "cot_pdf"
            model_used=model_name,
            hints_used=False,
            chunking="full",
        )

        if _count_leaf_conditions(ruleset) < MIN_EXPECTED_CONDITIONS:
            warnings.warn(
                f"Only {_count_leaf_conditions(ruleset)} conditions extracted, "
                f"expected >= {MIN_EXPECTED_CONDITIONS}.",
                UserWarning,
            )

        ruleset = apply_guardrail(ruleset, raw_document_text)
        ruleset = apply_safety_gate(ruleset)
        return ruleset, completion

    # ── pdfplumber text + native PDF (combined) ───────────────────────────────
    if variant == "direct_pdf_text":
        model_name = model or _cfg.get("MODEL_STAGE1", "google/gemini-2.5-flash")
        user_message = STAGE1_USER_TEMPLATE_PDF_AND_TEXT.format(
            document_text=document_text,
            constraint_scenario=scenario,
        )

        completion = complete_with_pdf_and_text(
            system=STAGE1_SYSTEM_DIRECT,
            user=user_message,
            pdf_path=pdf_path,
            model=model_name,
            max_tokens=STAGE1_MAX_TOKENS,
            temperature=0.0,
        )

        if save_raw:
            pathlib.Path(save_raw).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(save_raw).write_text(completion.text, encoding="utf-8")

        ruleset, _ = _parse_and_validate(
            raw=completion.text,
            user_message=user_message,
            model=model_name,
            variant="direct_pdf_text",
            model_used=model_name,
            hints_used=False,
            chunking="full",
        )

        if _count_leaf_conditions(ruleset) < MIN_EXPECTED_CONDITIONS:
            warnings.warn(
                f"Only {_count_leaf_conditions(ruleset)} conditions extracted, "
                f"expected >= {MIN_EXPECTED_CONDITIONS}.",
                UserWarning,
            )

        ruleset = apply_guardrail(ruleset, raw_document_text)
        ruleset = apply_safety_gate(ruleset)
        return ruleset, completion

    # ── Single-call path ──────────────────────────────────────────────────────
    model_name = model or _cfg.get("MODEL_STAGE1", "google/gemini-2.5-flash")
    system_prompt = SINGLE_CALL_VARIANTS[variant]
    user_message = STAGE1_USER_TEMPLATE.format(
        document_text=document_text,
        constraint_scenario=scenario,
    )

    completion = complete(
        system=system_prompt,
        user=user_message,
        model=model_name,
        max_tokens=STAGE1_MAX_TOKENS,
        temperature=0.0,
    )

    if save_raw:
        pathlib.Path(save_raw).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(save_raw).write_text(completion.text, encoding="utf-8")

    ruleset, _ = _parse_and_validate(
        raw=completion.text,
        user_message=user_message,
        model=model_name,
        variant=variant,
        model_used=model_name,
        hints_used=use_hints,
        chunking=chunking,
    )

    if _count_leaf_conditions(ruleset) < MIN_EXPECTED_CONDITIONS:
        warnings.warn(
            f"Only {_count_leaf_conditions(ruleset)} conditions extracted, "
            f"expected >= {MIN_EXPECTED_CONDITIONS}. "
            "Review prompt or chunking strategy.",
            UserWarning,
        )

    ruleset = apply_guardrail(ruleset, raw_document_text)
    ruleset = apply_safety_gate(ruleset)
    return ruleset, completion
