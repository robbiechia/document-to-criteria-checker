#!/usr/bin/env bash
# =============================================================================
# Document-to-Criteria Checker — Evaluation Runner
#
# QUICK START (reproduce Exp 1):
#   ./run_eval.sh exp1
#
# SINGLE DOCUMENT:
#   ./run_eval.sh stage1  --pdf data/pdf/chas_eligibility.pdf \
#                         --scenario "Who is eligible for CHAS?"
#
# STAGE 2 CODE GENERATION:
#   ./run_eval.sh stage2  --ruleset eval/results/exp1/exp1_chas_eligibility_ruleset.json \
#                         --language python
#
# END-TO-END (extract + generate):
#   ./run_eval.sh both    --pdf data/pdf/ica_singapore_citizenship_eligibility.pdf \
#                         --language sql \
#                         --schema data/demo_schema_singapore_citizenship.sql
#
# Prerequisites:
#   pip install -r requirements.txt
#   Add OPENROUTER_API_KEY to .env  (or export it)
# =============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

STAGE=""
PDF_PATH=""
SCENARIO="Extract all eligibility conditions from this policy document."
RULESET_PATH=""
CORPUS_PATH="data/annotation/annotations.csv"
LANGUAGE="python"
VARIANT=""
LABEL=""
SCHEMA=""
OUT_DIR="eval/results"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
header()  { echo -e "\n${BOLD}${CYAN}══ $* ══${NC}"; }

usage() {
cat <<'USAGE'

Document-to-Criteria Checker — Evaluation Runner

NAMED EXPERIMENTS (run all 4 documents in one command)
  ./run_eval.sh exp1              Experiment 1 — direct variant, annotations.csv
  ./run_eval.sh exp1 --variant cot_examples
                                  Re-run with a different extraction variant

SINGLE DOCUMENT — STAGE 1
  ./run_eval.sh stage1 --pdf data/pdf/chas_eligibility.pdf \
                        --scenario "Who is eligible for CHAS?"

  Options:
    --pdf        PATH   Document to extract from (PDF or image)
    --scenario   TEXT   What to extract  [default: "Extract all eligibility..."]
    --corpus     PATH   Annotation CSV for eval metrics
                        [default: data/annotation/annotations.csv]
    --variant    NAME   direct | cot | cot_examples | agentic
                        [default: reads STAGE1_VARIANT from config.json]
    --label      NAME   Output file label  [default: derived from filename]
    --out-dir    PATH   Output directory   [default: eval/results]

STAGE 2 — CODE GENERATION
  ./run_eval.sh stage2 --ruleset eval/results/exp1/exp1_chas_eligibility_ruleset.json \
                        --language python

  Options:
    --ruleset    PATH   RuleSet JSON from Stage 1 (required)
    --language   LANG   python | sql            [default: python]
    --schema     PATH   Schema file (.sql/.py/.json) or inline text for
                        field name mapping      [optional]
    --out-dir    PATH   Output directory        [default: eval/results]

END-TO-END (stage1 + stage2)
  ./run_eval.sh both --pdf data/pdf/ica_singapore_citizenship_eligibility.pdf \
                      --scenario "Eligibility to become a Singapore Citizen" \
                      --language sql \
                      --schema data/demo_schema_singapore_citizenship.sql

ENVIRONMENT
  OPENROUTER_API_KEY   Required for all LLM calls. Add to .env or export.
  PYTHON_BIN           Python binary  [default: python3]

OUTPUTS  (written to --out-dir, default eval/results/)
  Stage 1:  <label>_ruleset.json     extracted RuleSet
            <label>_stage1.json      eval metrics (recall, type accuracy, etc.)
  Stage 2:  <label>_generated.py/.sql  constraint code
            <label>_review.md          human review checklist

USAGE
}

require_file() { [[ -f "$1" ]] || error "File not found: $1"; }

require_env() {
  [[ -n "${!1:-}" ]] || error "Missing: $1\n       Add it to .env or: export $1=..."
}

check_python() {
  "$PYTHON_BIN" --version >/dev/null 2>&1 || \
    error "Python not found. Activate your venv or set PYTHON_BIN."
  "$PYTHON_BIN" -c "import app.config" >/dev/null 2>&1 || \
    error "Dependencies not installed. Run: pip install -r requirements.txt"
}

load_dotenv() { [[ -f .env ]] && set -a && source .env && set +a || true; }

get_variant() {
  if [[ -n "$VARIANT" ]]; then echo "$VARIANT"; return; fi
  "$PYTHON_BIN" -c "import app.config as c; print(c.get('STAGE1_VARIANT','cot_examples'))" 2>/dev/null \
    || echo "cot_examples"
}

# ---------------------------------------------------------------------------
# run_one_doc  — extract + eval for a single PDF
# Called by both run_stage1 and run_exp
# ---------------------------------------------------------------------------

run_one_doc() {
  local pdf="$1"
  local scenario="$2"
  local variant="$3"
  local label="$4"
  local out="$5"
  local corpus="$6"

  require_file "$pdf"

  info "Extracting → $(basename "$pdf")"
  info "  Variant : $variant  |  Label : $label"

  "$PYTHON_BIN" eval/run_stage1_experiment.py \
    --pdf      "$pdf"       \
    --scenario "$scenario"  \
    --variant  "$variant"   \
    --label    "$label"     \
    --out-dir  "$out"

  local ruleset="$out/${label}_ruleset.json"
  success "RuleSet → $ruleset"

  # Evaluate if corpus has rows for this doc
  local doc_id
  doc_id="$(basename "${pdf%.*}")"

  if [[ -f "$corpus" ]]; then
    local row_count
    row_count="$("$PYTHON_BIN" -c "
import csv
with open('$corpus', encoding='utf-8') as f:
    rows = [r for r in csv.DictReader(f)
            if r.get('doc_id','').strip() == '$doc_id'
            and r.get('clause_verbatim','').strip()]
print(len(rows))
" 2>/dev/null || echo "0")"

    if [[ "$row_count" -gt 0 ]]; then
      "$PYTHON_BIN" eval/eval_stage1.py \
        --ruleset "$ruleset"  \
        --corpus  "$corpus"   \
        --doc-id  "$doc_id"   \
        --label   "$label"    \
        --out-dir "$out"
      success "Eval metrics → $out/${label}_stage1.json"
    else
      warn "No corpus rows for '$doc_id' — extraction saved, eval skipped."
    fi
  else
    warn "No corpus found at $corpus — extraction saved, eval skipped."
  fi
}

# ---------------------------------------------------------------------------
# Named experiment: exp1
# All 4 annotated documents, direct variant, annotations.csv
# ---------------------------------------------------------------------------

run_exp1() {
  local variant
  variant="$(get_variant)"
  local exp_label="exp1"
  local out="$OUT_DIR/exp1"
  local corpus="data/annotation/annotations.csv"

  require_env OPENROUTER_API_KEY
  require_file "$corpus"
  mkdir -p "$out"

  header "Experiment 1 — variant: $variant — 4 documents"
  echo "  Corpus  : $corpus"
  echo "  Output  : $out/"
  echo ""

  local -A SCENARIOS=(
    [cpf_housing_grants_eligibility]="Extract all eligibility conditions and grant amounts for CPF Housing Grants for families buying resale flats."
    [gst_voucher_eligibility]="Extract all eligibility criteria and payout tiers for the GST Voucher Scheme 2025."
    [ica_singapore_citizenship_eligibility]="Extract all eligibility criteria and formalities to become a Singapore Citizen."
    [chas_eligibility]="Extract all eligibility criteria and subsidy tiers for the Community Health Assist Scheme (CHAS)."
  )

  local docs=(
    cpf_housing_grants_eligibility
    gst_voucher_eligibility
    ica_singapore_citizenship_eligibility
    chas_eligibility
  )

  for doc in "${docs[@]}"; do
    local pdf="data/pdf/${doc}.pdf"
    local label="${exp_label}_${doc}"
    local scenario="${SCENARIOS[$doc]}"
    echo ""
    run_one_doc "$pdf" "$scenario" "$variant" "$label" "$out" "$corpus"
  done

  # Print summary table
  echo ""
  header "Experiment 1 Results Summary"
  "$PYTHON_BIN" << PYEOF
import json, os
docs = [
    ("${docs[0]}", "CPF Housing Grants"),
    ("${docs[1]}", "GST Voucher"),
    ("${docs[2]}", "ICA Citizenship"),
    ("${docs[3]}", "CHAS"),
]
hdr = f"{'Document':<26} {'Recall':>8} {'TypeAcc':>8} {'EscRec':>7} {'OR acc':>7} {'Hall':>6}"
print(hdr)
print("-" * 64)
tf=tt=tc=tft=ef=et=ogc=ogt=hc=he=0
for doc_id, name in docs:
    path = f"$out/exp1_{doc_id}_stage1.json"
    if not os.path.exists(path):
        print(f"  {name:<24} (eval not run)")
        continue
    d = json.load(open(path))
    print(
        f"  {name:<24}"
        f" {d['condition_recall']:>7.1%}"
        f" {d['type_accuracy']:>7.1%}"
        f" {d['escalation_recall']:>6.1%}"
        f" {d['or_group_accuracy']:>6.1%}"
        f" {d['hallucination_rate']:>5.1%}"
    )
    tf+=d["conditions_found"]; tt+=d["conditions_total"]
    tc+=d["type_correct"];     tft+=d["typed_matched_total"]
    ef+=d["escalated_found"];  et+=d["escalated_total"]
    ogc+=d["correct_or_groups"]; ogt+=d["total_or_groups"]
    hc+=d["hallucination_count"]; he+=d["conditions_found"]
if tt:
    print("-" * 64)
    print(
        f"  {'OVERALL':<24}"
        f" {tf/tt:>7.1%}"
        f" {(tc/tft if tft else 0):>7.1%}"
        f" {(ef/et if et else 0):>6.1%}"
        f" {(ogc/ogt if ogt else 0):>6.1%}"
        f" {(hc/he if he else 0):>5.1%}"
    )
    print(f"  {tf}/{tt} conditions matched")
PYEOF
}

# ---------------------------------------------------------------------------
# Stage 1 — single document
# ---------------------------------------------------------------------------

run_stage1() {
  [[ -n "$PDF_PATH" ]] || error "Stage 1 requires --pdf <path>"
  require_file "$PDF_PATH"
  require_env OPENROUTER_API_KEY

  local variant; variant="$(get_variant)"
  if [[ -z "$LABEL" ]]; then
    LABEL="$(basename "${PDF_PATH%.*}")_$(date +%Y%m%d_%H%M%S)"
  fi
  mkdir -p "$OUT_DIR"

  header "Stage 1 — $(basename "$PDF_PATH")"
  run_one_doc "$PDF_PATH" "$SCENARIO" "$variant" "$LABEL" "$OUT_DIR" "$CORPUS_PATH"
  RULESET_PATH="$OUT_DIR/${LABEL}_ruleset.json"
}

# ---------------------------------------------------------------------------
# Stage 2 — code generation
# ---------------------------------------------------------------------------

run_stage2() {
  [[ -n "$RULESET_PATH" ]] || error "Stage 2 requires --ruleset <path>"
  require_file "$RULESET_PATH"
  mkdir -p "$OUT_DIR"

  if [[ -z "$LABEL" ]]; then
    LABEL="s2_$(basename "${RULESET_PATH%_ruleset.json}")_$(date +%Y%m%d_%H%M%S)"
  fi

  local s2_variant
  if [[ "$LANGUAGE" == "sql" ]]; then
    s2_variant="sql"
  else
    s2_variant="$("$PYTHON_BIN" -c "import app.config as c; print(c.get('STAGE2_VARIANT','hybrid'))" 2>/dev/null || echo "hybrid")"
  fi

  [[ "$s2_variant" == "deterministic" || "$s2_variant" == "sql" ]] || require_env OPENROUTER_API_KEY

  header "Stage 2 — $(basename "$RULESET_PATH")"
  info "Language : $LANGUAGE  (variant: $s2_variant)"
  [[ -n "$SCHEMA" ]] && info "Schema   : $SCHEMA"
  echo ""

  local schema_arg=()
  if [[ -n "$SCHEMA" ]]; then
    if [[ -f "$SCHEMA" ]]; then
      local schema_text; schema_text="$(cat "$SCHEMA")"
      schema_arg=(--schema "$schema_text")
    else
      schema_arg=(--schema "$SCHEMA")
    fi
  fi

  "$PYTHON_BIN" eval/run_stage2_experiment.py \
    --ruleset "$RULESET_PATH"      \
    --variant "$s2_variant"        \
    --label   "$LABEL"             \
    "${schema_arg[@]}"

  local ext=".py"; [[ "$LANGUAGE" == "sql" ]] && ext=".sql"
  success "Code     → $OUT_DIR/${LABEL}_generated${ext}"
  local review="$OUT_DIR/${LABEL}_review.md"
  [[ -f "$review" ]] && success "Review   → $review" && \
    info "Open $review, mark each condition pass/fail, approve or reject."
}

# ---------------------------------------------------------------------------
# Argument parsing & dispatch
# ---------------------------------------------------------------------------

[[ $# -eq 0 ]] && usage && exit 0

STAGE="$1"; shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pdf)      PDF_PATH="$2";     shift 2 ;;
    --scenario) SCENARIO="$2";    shift 2 ;;
    --ruleset)  RULESET_PATH="$2"; shift 2 ;;
    --corpus)   CORPUS_PATH="$2"; shift 2 ;;
    --language) LANGUAGE="$2";    shift 2 ;;
    --variant)  VARIANT="$2";     shift 2 ;;
    --label)    LABEL="$2";       shift 2 ;;
    --schema)   SCHEMA="$2";      shift 2 ;;
    --out-dir)  OUT_DIR="$2";     shift 2 ;;
    -h|--help)  usage; exit 0 ;;
    *) error "Unknown option: $1\nRun ./run_eval.sh --help" ;;
  esac
done

load_dotenv
check_python

case "$STAGE" in
  exp1)
    run_exp1
    ;;

  stage1)
    run_stage1
    ;;

  stage2)
    run_stage2
    ;;

  both)
    run_stage1
    echo ""
    info "Stage 1 done. Running Stage 2…"
    echo ""
    run_stage2
    ;;

  help|--help|-h)
    usage
    ;;

  *)
    error "Unknown stage: '$STAGE'\nChoose: exp1, stage1, stage2, both\nRun ./run_eval.sh --help"
    ;;
esac

echo ""
success "Done. Results in $OUT_DIR/"
