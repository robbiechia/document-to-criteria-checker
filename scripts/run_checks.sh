#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PDF_PATH="data/pdf/cpf_housing_grants_eligibility.pdf"
CORPUS_PATH="data/annotation/annotations.csv"
DOC_ID="cpf_housing_grants_eligibility"
SCENARIO="Extract all eligibility conditions for a household applying for CPF Housing Grants for a resale flat"

PYTHON_BIN="${PYTHON_BIN:-python}"

usage() {
  cat <<EOF
Usage:
  scripts/run_checks.sh <command>

Setup:
  source .venv/bin/activate
  export OPENROUTER_API_KEY=sk-or-...

Models (set in .env):
  Stage 1 — google/gemini-2.5-flash  (MODEL_STAGE1)
  Stage 2 — openai/gpt-4.1           (MODEL_STAGE2)

Commands:
  unit             Run all local unit tests (no API calls).
  guardrail        Run synthetic hallucination guardrail validation (no API calls).
  parser           Run PDF parser sanity check (no API calls).
  local-all        Run unit + guardrail + parser.

  ui               Launch the Streamlit web interface.

  # Stage 1 experiments (require OPENROUTER_API_KEY)
  exp-direct       Baseline: single LLM call, no reasoning.
  exp-cot          Chain-of-thought: 5-step reasoning, escalation before classification.
  exp-cot-ex       CoT with concrete HDB classification examples.
  exp-agentic      Agentic: 4-step pipeline (Gemini Flash ×3 → GPT-4.1 validation).

  eval-direct      Evaluate direct output against annotation corpus.
  eval-cot         Evaluate CoT output.
  eval-cot-ex      Evaluate CoT-with-examples output.
  eval-agentic     Evaluate agentic output.

  # Stage 2 generator experiments (set RULESET= before running)
  s2-determ        Deterministic code generation (no LLM).
  s2-llm           LLM code generation with GPT-4.1.
  s2-hybrid        Deterministic baseline + GPT-4.1 review.
  eval-s2          Generate human review checklist for a generated code file.
                   Requires: CODE=eval/results/<label>_generated.py
                   Optional: RULESET=... GUARDRAILS=eval/results/<label>_guardrails.json

Results:
  RuleSet JSON:     eval/results/<label>_ruleset.json
  Eval metrics:     eval/results/<label>_stage1.json
  Generated code:   eval/results/<label>_generated.py
  Guardrail report: eval/results/<label>_guardrails.json
  Experiment log:   eval/results/experiment_log.md

Environment:
  OPENROUTER_API_KEY  required for all LLM commands
  RULESET             path to ruleset JSON for s2-* commands
EOF
}

require_file() {
  if [[ ! -f "$1" ]]; then
    echo "Missing required file: $1" >&2
    exit 1
  fi
}

require_env() {
  if [[ -z "${!1:-}" ]]; then
    echo "Missing required environment variable: $1" >&2
    echo "Tip: add OPENROUTER_API_KEY to your .env file or export it." >&2
    exit 1
  fi
}

run_unit()     { "$PYTHON_BIN" -m pytest tests/ -q; }
run_guardrail(){ "$PYTHON_BIN" eval/test_guardrail.py; }

run_parser() {
  require_file "$PDF_PATH"
  "$PYTHON_BIN" -c "
from app.pipeline.pdf_parser import extract_chunks, extract_hints, filter_obligation_chunks
chunks = extract_chunks('$PDF_PATH')
filtered = filter_obligation_chunks(chunks)
hints = extract_hints(filtered)
print(f'Chunks: {len(chunks)} total, {len(filtered)} filtered')
print(f'Numeric hints: {[h[\"value\"] for h in hints.numeric_candidates[:8]]}')
print(f'Duration hints: {[h[\"value\"] for h in hints.date_candidates[:8]]}')
"
}

run_ui() { "$PYTHON_BIN" -m streamlit run app/ui/streamlit_app.py; }

run_s1() {
  local variant="$1"
  local label="$2"
  require_env OPENROUTER_API_KEY
  require_file "$PDF_PATH"
  require_file "$CORPUS_PATH"
  "$PYTHON_BIN" eval/run_stage1_experiment.py \
    --pdf "$PDF_PATH" \
    --scenario "$SCENARIO" \
    --variant "$variant" \
    --label "$label"
}

run_s1_eval() {
  local label="$1"
  require_file "eval/results/${label}_ruleset.json"
  require_file "$CORPUS_PATH"
  "$PYTHON_BIN" eval/eval_stage1.py \
    --ruleset "eval/results/${label}_ruleset.json" \
    --corpus "$CORPUS_PATH" \
    --doc-id "$DOC_ID" \
    --label "$label"
}

run_s2() {
  local variant="$1"
  local ruleset="${RULESET:-}"
  if [[ -z "$ruleset" ]]; then
    echo "Set RULESET=eval/results/<label>_ruleset.json before running s2 commands." >&2
    exit 1
  fi
  require_file "$ruleset"
  local label="s2_${variant}_$(basename "${ruleset%_ruleset.json}")"
  if [[ "$variant" != "determ" ]]; then
    require_env OPENROUTER_API_KEY
  fi
  "$PYTHON_BIN" eval/run_stage2_experiment.py \
    --ruleset "$ruleset" \
    --variant "${variant/determ/deterministic}" \
    --label "$label" \
    --flowchart \
    --tests
}

command="${1:-}"

case "$command" in
  unit)         run_unit ;;
  guardrail)    run_guardrail ;;
  parser)       run_parser ;;
  local-all)    run_unit && run_guardrail && run_parser ;;
  ui)           run_ui ;;

  exp-direct)   run_s1 "direct"       "exp0_direct" ;;
  exp-cot)      run_s1 "cot"          "exp1_cot" ;;
  exp-cot-ex)   run_s1 "cot_examples" "exp2_cot_examples" ;;
  exp-agentic)  run_s1 "agentic"      "exp3_agentic" ;;

  eval-direct)  run_s1_eval "exp0_direct" ;;
  eval-cot)     run_s1_eval "exp1_cot" ;;
  eval-cot-ex)  run_s1_eval "exp2_cot_examples" ;;
  eval-agentic) run_s1_eval "exp3_agentic" ;;

  s2-determ)    run_s2 "determ" ;;
  s2-llm)       run_s2 "llm" ;;
  s2-hybrid)    run_s2 "hybrid" ;;

  eval-s2)
    code="${CODE:-}"
    if [[ -z "$code" ]]; then
      echo "Set CODE=eval/results/<label>_generated.py before running eval-s2." >&2; exit 1
    fi
    require_file "$code"
    label="$(basename "${code%_generated.py}")"
    ruleset="${RULESET:-eval/results/${label}_ruleset.json}"
    guardrails="${GUARDRAILS:-eval/results/${label}_guardrails.json}"
    "$PYTHON_BIN" eval/eval_stage2.py \
      --code "$code" \
      --ruleset "$ruleset" \
      --guardrails "$guardrails" \
      --label "$label"
    ;;

  ""|help|--help|-h) usage ;;
  *)
    echo "Unknown command: $command" >&2; echo >&2; usage >&2; exit 1 ;;
esac
