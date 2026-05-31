"""Run a Stage 1 extraction experiment and save all outputs."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.pipeline.extractor import extract_rules, ALL_VARIANTS


def run_experiment(
    pdf_path: str,
    scenario: str,
    variant: str,
    model: str | None,
    hints: bool,
    chunking: str,
    label: str,
    out_dir_str: str = "eval/results",
) -> None:
    out_dir = Path(out_dir_str)
    out_dir.mkdir(parents=True, exist_ok=True)

    is_agentic = variant in ("agentic", "agentic_pdf")

    print(f"\nRunning experiment: {label}")
    print(f"  PDF: {pdf_path}")
    print(f"  Variant: {variant} | Hints: {hints} | Chunking: {chunking}")
    if is_agentic:
        print(
            "  Agentic pipeline: 3 LLM steps "
            "(Gemini Flash ×3 → GPT-4.1 validation)"
        )

    ruleset, completion_or_steps = extract_rules(
        pdf_path=pdf_path,
        constraint_scenario=scenario,
        variant=variant,
        model=model,
        use_hints=hints,
        chunking=chunking,
        save_raw=str(out_dir / f"{label}_raw_response.txt") if not is_agentic else None,
        # For agentic, intermediates are saved to the results dir by agent_pipeline
    )

    ruleset_path = out_dir / f"{label}_ruleset.json"
    ruleset_path.write_text(ruleset.model_dump_json(indent=2), encoding="utf-8")

    def count_leaves(node) -> int:
        if node.is_leaf:
            return 1
        return sum(count_leaves(child) for child in node.conditions)

    leaf_count = count_leaves(ruleset.root)
    escalated_count = len(ruleset.escalated_rules)

    print(f"\n  Extracted: {leaf_count} evaluatable conditions")
    print(f"  Escalated: {escalated_count} discretionary/history/spatial conditions")
    print(f"  Hallucination risk: {ruleset.hallucination_risk_count} conditions")
    print(f"  RuleSet saved to: {ruleset_path}")

    # Build log entry — handle single-call and agentic differently
    log_path = out_dir / "experiment_log.md"
    total = leaf_count + escalated_count

    if is_agentic:
        steps = completion_or_steps  # list of per-step dicts
        total_in  = sum(s["input_tokens"]  for s in steps)
        total_out = sum(s["output_tokens"] for s in steps)
        total_ms  = sum(s["latency_ms"]    for s in steps)

        step_table = "\n".join(
            f"| {s['step']} | {s['name']} | {s['model']} | "
            f"{s['input_tokens']} | {s['output_tokens']} | {s['latency_ms']}ms |"
            for s in steps
        )
        token_section = (
            f"- Total input tokens:  {total_in}\n"
            f"- Total output tokens: {total_out}\n"
            f"- Total latency:       {total_ms:.0f}ms\n\n"
            f"| Step | Name | Model | In tokens | Out tokens | Latency |\n"
            f"|------|------|-------|-----------|------------|----------|\n"
            f"{step_table}"
        )
    else:
        comp = completion_or_steps
        print(f"  Tokens: {comp.input_tokens} in / {comp.output_tokens} out")
        print(f"  Latency: {comp.latency_ms:.0f}ms")
        token_section = (
            f"- Input tokens:  {comp.input_tokens}\n"
            f"- Output tokens: {comp.output_tokens}\n"
            f"- Latency:       {comp.latency_ms:.0f}ms\n"
            f"- Model:         {comp.model}"
        )

    log_entry = f"""
## Run — {label}
**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}
**PDF:** {pdf_path}
**Variant:** {variant}
**Hints:** {hints}
**Chunking:** {chunking}

**Extraction summary:**
- Evaluatable conditions: {leaf_count}
- Escalated conditions: {escalated_count}
- Hallucination risk flags: {ruleset.hallucination_risk_count}
{token_section}

**Eval metrics:** run `eval/eval_stage1.py` to populate.

| Metric | Value |
|--------|-------|
| Condition recall | - |
| Type accuracy | - |
| Escalation recall | - |
| OR tree accuracy | - |
| Hallucination rate | {ruleset.hallucination_risk_count}/{total} |

**Manual observations:** fill in after inspection.

---
"""
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(log_entry)
    print(f"  Log entry appended to: {log_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument(
        "--variant",
        default="direct",
        choices=sorted(ALL_VARIANTS),
        metavar="{" + ",".join(sorted(ALL_VARIANTS)) + "}",
    )
    parser.add_argument("--model", default=None,
                        help="OpenRouter model string. Defaults to MODEL_STAGE1 env var.")
    parser.add_argument("--hints", default="false", choices=["true", "false"])
    parser.add_argument("--chunking", default="full", choices=["full", "section"])
    parser.add_argument("--label", required=True)
    parser.add_argument("--out-dir", default="eval/results",
                        help="Directory for output files (default: eval/results)")
    parsed = parser.parse_args()

    run_experiment(
        pdf_path=parsed.pdf,
        scenario=parsed.scenario,
        variant=parsed.variant,
        model=parsed.model,
        hints=parsed.hints == "true",
        chunking=parsed.chunking,
        label=parsed.label,
        out_dir_str=parsed.out_dir,
    )
