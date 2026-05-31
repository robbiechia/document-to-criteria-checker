"""Generate a pytest test suite from a RuleSet and optionally validate with an LLM judge.

Stage 2 Step 5 from build.md:
  - Generate test cases from allow_example and deny_example fields in each ConditionNode
  - Generate boundary test cases from condition thresholds
  - Use LLM as judge to verify verdict correctness and add edge cases
  - Output a runnable pytest file
"""

from __future__ import annotations

import json
import os
import app.config as _cfg
import re
import textwrap
from datetime import datetime
from typing import Any, Optional

from app.models.schemas import ConditionNode, RuleSet
from app.pipeline.prompts import (
    STAGE2_TEST_JUDGE_SYSTEM,
    STAGE2_TEST_JUDGE_USER_TEMPLATE,
)
from app.utils.llm_client import complete


def _safe_fn(rule_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", rule_id).lower().strip("_")


def _collect_all_leaves(node: ConditionNode) -> list[ConditionNode]:
    if node.is_leaf:
        return [node]
    leaves = []
    for child in node.conditions:
        leaves.extend(_collect_all_leaves(child))
    return leaves


def _boundary_cases(node: ConditionNode) -> list[dict[str, Any]]:
    """Generate allow/deny boundary test cases from a leaf's threshold."""
    cases: list[dict[str, Any]] = []
    if not node.field_required or node.threshold is None:
        return cases

    threshold = node.threshold
    field = node.field_required

    if node.operator and isinstance(threshold, (int, float)):
        from app.models.schemas import LogicOperator
        if node.operator == LogicOperator.GTE:
            cases.append({
                "profile": {field: threshold},
                "expected": "allow",
                "label": f"{field}={threshold} exactly at gte boundary",
            })
            cases.append({
                "profile": {field: threshold - 1},
                "expected": "deny",
                "label": f"{field}={threshold - 1} just below gte boundary",
            })
        elif node.operator == LogicOperator.LTE:
            cases.append({
                "profile": {field: threshold},
                "expected": "allow",
                "label": f"{field}={threshold} exactly at lte boundary",
            })
            cases.append({
                "profile": {field: threshold + 1},
                "expected": "deny",
                "label": f"{field}={threshold + 1} just above lte boundary",
            })
        elif node.operator == LogicOperator.EQ:
            cases.append({
                "profile": {field: threshold},
                "expected": "allow",
                "label": f"{field}={threshold!r} exact match",
            })
        cases.append({
            "profile": {},
            "expected": "escalate",
            "label": f"missing field {field!r} → escalate",
        })

    if node.operator and isinstance(threshold, list):
        if threshold:
            cases.append({
                "profile": {field: threshold[0]},
                "expected": "allow",
                "label": f"{field}={threshold[0]!r} in allowed set",
            })
            cases.append({
                "profile": {field: "__NOT_IN_SET__"},
                "expected": "deny",
                "label": f"{field}=__NOT_IN_SET__ not in allowed set",
            })

    return cases


def _generate_test_cases(ruleset: RuleSet) -> list[dict]:
    """Produce test cases from allow/deny examples and threshold boundaries."""
    leaves = _collect_all_leaves(ruleset.root)
    test_cases: list[dict] = []

    for node in leaves:
        if node.should_escalate:
            test_cases.append({
                "test_id": f"test_{_safe_fn(node.rule_id)}_escalates",
                "rule_id": node.rule_id,
                "description": f"Escalated rule {node.rule_id} must return escalate",
                "profile": {},
                "expected": "escalate",
                "source": "escalation",
            })
            continue

        # allow_example / deny_example fields no longer in schema — skip

        # Boundary cases from threshold
        for i, case in enumerate(_boundary_cases(node), 1):
            test_cases.append({
                "test_id": f"test_{_safe_fn(node.rule_id)}_boundary_{i}",
                "rule_id": node.rule_id,
                "description": f"{node.rule_id}: {case['label']}",
                "profile": case["profile"],
                "expected": case["expected"],
                "source": "boundary",
            })

    return test_cases


def validate_with_llm_judge(
    test_cases: list[dict],
    ruleset: RuleSet,
    model: Optional[str] = None,
) -> list[dict]:
    """Use an LLM as judge to verify test case verdicts are correct.

    Returns the validated (and potentially corrected) test cases.
    """
    if not test_cases:
        return test_cases

    model_name = model or _cfg.get("MODEL_STAGE2", "openai/gpt-4.1")
    ruleset_json = ruleset.model_dump_json(indent=2)
    test_cases_json = json.dumps(test_cases, indent=2)

    user_message = STAGE2_TEST_JUDGE_USER_TEMPLATE.format(
        ruleset_json=ruleset_json,
        test_cases_json=test_cases_json,
    )

    completion = complete(
        system=STAGE2_TEST_JUDGE_SYSTEM,
        user=user_message,
        model=model_name,
        max_tokens=4096,
        temperature=0.0,
    )

    try:
        reviews = json.loads(completion.text)
    except (json.JSONDecodeError, ValueError):
        return test_cases  # LLM output wasn't JSON — return unchanged

    corrections: dict[str, str] = {}
    for review in reviews:
        if isinstance(review, dict) and not review.get("correct", True):
            tid = review.get("test_id")
            correction = review.get("correction", {})
            if tid and isinstance(correction, dict) and "expected" in correction:
                corrections[tid] = correction["expected"]

    validated = []
    for case in test_cases:
        if case["test_id"] in corrections:
            corrected = dict(case)
            corrected["expected"] = corrections[case["test_id"]]
            corrected["llm_corrected"] = True
            validated.append(corrected)
        else:
            validated.append(case)

    return validated


def _render_pytest_file(test_cases: list[dict], module_path: str, ruleset: RuleSet) -> str:
    """Render test cases as a runnable pytest file."""
    import_path = module_path.replace("/", ".").replace(".py", "")
    lines = [
        f'"""Auto-generated test suite for {ruleset.policy_name}.',
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f'Source: {ruleset.source_document}',
        '"""',
        "from __future__ import annotations",
        "import pytest",
        f"from {import_path} import evaluate",
        "",
        "",
    ]

    for case in test_cases:
        test_id = re.sub(r"[^a-zA-Z0-9_]", "_", case["test_id"])
        profile_repr = repr(case["profile"])
        expected = case["expected"]
        desc = case["description"].replace('"', '\\"')

        lines.append(f"def {test_id}():")
        lines.append(f'    """' + desc + '"""')
        lines.append(f"    profile = {profile_repr}")
        lines.append(f"    result = evaluate(profile)")
        lines.append(f'    assert result["overall_verdict"] == "{expected}", (')
        lines.append(f'        f"Expected {expected!r} but got {{result[\'overall_verdict\']!r}}\\n"')
        lines.append(f'        f"Profile: {{profile}}"')
        lines.append(f"    )")
        lines.append("")

    return "\n".join(lines)


def generate_test_suite(
    ruleset: RuleSet,
    generated_code_path: str,
    output_path: Optional[str] = None,
    use_llm_judge: bool = True,
    model: Optional[str] = None,
) -> tuple[str, list[dict]]:
    """Generate a pytest test suite for the Stage 2 generated code.

    Args:
        ruleset: the extracted RuleSet
        generated_code_path: path to the generated .py file (for import)
        output_path: where to write the test file (optional)
        use_llm_judge: whether to run the LLM judge validation pass
        model: OpenRouter model string

    Returns (pytest_file_content, test_cases_list).
    """
    test_cases = _generate_test_cases(ruleset)

    if use_llm_judge and test_cases:
        test_cases = validate_with_llm_judge(test_cases, ruleset, model=model)

    pytest_content = _render_pytest_file(test_cases, generated_code_path, ruleset)

    if output_path:
        from pathlib import Path
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(pytest_content, encoding="utf-8")

    return pytest_content, test_cases
