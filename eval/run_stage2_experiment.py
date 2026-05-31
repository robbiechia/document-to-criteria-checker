"""Run a Stage 2 code generation experiment and save all outputs."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.models.schemas import RuleSet
from app.stage2 import generator_deterministic, generator_llm, generator_hybrid
from app.stage2.guardrails import apply_guardrails
from app.stage2.decision_tree import to_tree_json, generate_mermaid
from app.stage2.test_suite import generate_test_suite


def run_experiment(
    ruleset_path: str,
    variant: str,
    model: str | None,
    label: str,
    generate_flowchart: bool,
    generate_tests: bool,
    use_llm_judge: bool,
    user_schema: str | None = None,
) -> None:
    out_dir = Path("eval/results")
    out_dir.mkdir(parents=True, exist_ok=True)

    ruleset_data = json.loads(Path(ruleset_path).read_text(encoding="utf-8"))
    # Normalise legacy field names (e.g. more_info_needed → membership) before validation
    from app.pipeline.extractor import _normalize_node
    if "root" in ruleset_data:
        _normalize_node(ruleset_data["root"])
    for r in ruleset_data.get("escalated_rules", []):
        if isinstance(r, dict):
            _normalize_node(r)
    ruleset_data["escalated_rules"] = [
        r for r in ruleset_data.get("escalated_rules", []) if isinstance(r, dict)
    ]
    ruleset = RuleSet.model_validate(ruleset_data)

    print(f"\nRunning Stage 2 experiment: {label}")
    print(f"  RuleSet: {ruleset_path}")
    print(f"  Variant: {variant} | Model: {model}")

    # ── Generate code ──────────────────────────────────────────────────────
    if variant == "sql":
        from app.stage2.generator_sql import generate as gen_sql
        code = gen_sql(ruleset)
        input_tokens = output_tokens = 0
        latency_ms = 0.0
        provider = "N/A (deterministic SQL)"
    elif variant == "deterministic":
        code = generator_deterministic.generate(ruleset)
        input_tokens = output_tokens = 0
        latency_ms = 0.0
        provider = "N/A (deterministic)"
    elif variant == "llm":
        code, completion = generator_llm.generate(ruleset, model=model)
        input_tokens = completion.input_tokens
        output_tokens = completion.output_tokens
        latency_ms = completion.latency_ms
        provider = completion.provider
    elif variant == "hybrid":
        result = generator_hybrid.generate(ruleset, user_schema=user_schema, model=model)
        code = result.reviewed_code
        input_tokens = result.review_completion.input_tokens
        output_tokens = result.review_completion.output_tokens
        latency_ms = result.review_completion.latency_ms
        provider = result.review_completion.provider
        print(f"  Changes from LLM review: {'yes' if result.changes_made else 'no'}")
        if result.schema_substitutions:
            print(f"  Schema substitutions applied: {result.schema_substitutions}")
            print(f"  Field mapping: {result.schema_mapping}")
        determ_path = out_dir / f"{label}_deterministic_baseline.py"
        determ_path.write_text(result.deterministic_code, encoding="utf-8")
    else:
        raise ValueError(f"Unknown variant: {variant}")

    ext = ".sql" if variant == "sql" else ".py"
    code_path = out_dir / f"{label}_generated{ext}"
    code_path.write_text(code, encoding="utf-8")
    print(f"  Code saved to: {code_path}")

    # ── Guardrails ─────────────────────────────────────────────────────────
    if variant == "sql":
        from app.stage2.guardrails_sql import apply_sql_guardrails
        guard_report = apply_sql_guardrails(code, ruleset)
    else:
        guard_report = apply_guardrails(code, ruleset)
    guard_path = out_dir / f"{label}_guardrails.json"
    guard_path.write_text(json.dumps(guard_report.summary(), indent=2), encoding="utf-8")

    passed_icon = "✓" if guard_report.passed else "✗"
    print(f"\n  Guardrails {passed_icon}: {'PASS' if guard_report.passed else 'FAIL'}")
    if not guard_report.syntax_ok:
        print(f"    Syntax error: {guard_report.syntax_error}")
    if guard_report.missing_functions:
        print(f"    Missing functions: {guard_report.missing_functions}")
    if guard_report.dangerous_violations:
        print(f"    Dangerous calls: {guard_report.dangerous_violations}")
    if guard_report.hallucinated_functions:
        print(f"    Possible hallucinations: {guard_report.hallucinated_functions}")

    # ── Decision tree ──────────────────────────────────────────────────────
    tree_dict = to_tree_json(ruleset)
    tree_path = out_dir / f"{label}_decision_tree.json"
    tree_path.write_text(json.dumps(tree_dict, indent=2), encoding="utf-8")
    print(f"  Decision tree saved to: {tree_path}")

    if generate_flowchart:
        mermaid, was_generated = generate_mermaid(tree_dict, model=model)
        mermaid_path = out_dir / f"{label}_flowchart.mmd"
        mermaid_path.write_text(mermaid, encoding="utf-8")
        if was_generated:
            print(f"  Mermaid flowchart saved to: {mermaid_path}")
        else:
            print(f"  Flowchart skipped (tree too large). Note saved to: {mermaid_path}")

    # ── Test suite ─────────────────────────────────────────────────────────
    if generate_tests:
        test_path = out_dir / f"{label}_tests.py"
        pytest_content, test_cases = generate_test_suite(
            ruleset=ruleset,
            generated_code_path=str(code_path),
            output_path=str(test_path),
            use_llm_judge=use_llm_judge,
            model=model,
        )
        print(f"  Test suite ({len(test_cases)} cases) saved to: {test_path}")

    # ── Human review checklist ─────────────────────────────────────────────
    from eval.eval_stage2 import eval_stage2 as run_eval_stage2
    run_eval_stage2(
        code_path=str(code_path),
        ruleset_path=ruleset_path,
        guardrail_path=str(guard_path),
        label=label,
    )

    # ── Experiment log ─────────────────────────────────────────────────────
    log_path = out_dir / "experiment_log.md"
    guard_summary = guard_report.summary()
    log_entry = f"""
## Stage 2 Run — {label}
**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}
**RuleSet:** {ruleset_path}
**Policy:** {ruleset.policy_name}
**Variant:** {variant}
**Model:** {model or 'default'}
**Provider:** {provider}

**Generation summary:**
- Input tokens: {input_tokens}
- Output tokens: {output_tokens}
- Latency: {latency_ms:.0f}ms

**Guardrails:** {'PASS' if guard_report.passed else 'FAIL'}

| Check | Result |
|-------|--------|
| Syntax | {'✓' if guard_summary['syntax_ok'] else '✗'} |
| No dangerous calls | {'✓' if guard_summary['no_dangerous_calls'] else '✗'} |
| Rule coverage | {'✓' if guard_summary['rule_coverage_ok'] else '✗'} |
| Escalation coverage | {'✓' if guard_summary['escalation_coverage_ok'] else '✗'} |
| No hallucinations | {'✓' if guard_summary['no_hallucinations'] else '✗'} |
| evaluate() exists | {'✓' if guard_summary['evaluate_exists'] else '✗'} |

**Human review:** see eval/results/{label}_review.md

---
"""
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(log_entry)
    print(f"  Log entry appended to: {log_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a Stage 2 code generation experiment")
    parser.add_argument("--ruleset", required=True, help="Path to Stage 1 ruleset JSON")
    parser.add_argument(
        "--variant",
        default="deterministic",
        choices=["deterministic", "llm", "hybrid", "sql"],
    )
    parser.add_argument("--model", default=None,
                        help="OpenRouter model string. Defaults to MODEL_STAGE2 env var.")
    parser.add_argument("--label", required=True, help="Experiment label for output files")
    parser.add_argument("--flowchart", action="store_true", help="Generate Mermaid flowchart")
    parser.add_argument("--tests", action="store_true", help="Generate test suite")
    parser.add_argument(
        "--no-llm-judge",
        action="store_true",
        help="Skip LLM judge validation of test cases",
    )
    parser.add_argument(
        "--schema", default=None,
        help="User schema description (free text, JSON Schema, TypedDict, or variable list). "
             "Used by hybrid variant to map extracted field names to schema field names."
    )
    args = parser.parse_args()

    run_experiment(
        ruleset_path=args.ruleset,
        variant=args.variant,
        model=args.model,
        label=args.label,
        generate_flowchart=args.flowchart,
        generate_tests=args.tests,
        use_llm_judge=not args.no_llm_judge,
        user_schema=args.schema,
    )
