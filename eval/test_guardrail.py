"""Validate that the hallucination guardrail catches invented conditions."""

from __future__ import annotations

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.pipeline.guardrails import verify_source_clause

SYNTHETIC_DOCUMENT = """
SYNTHETIC ELIGIBILITY POLICY FOR GUARDRAIL TESTING

Section 1: Age Requirements
1.1 The applicant must be at least 25 years old at the time of application.
1.2 The applicant must not be older than 60 years old at the time of application.

Section 2: Citizenship Requirements
2.1 The applicant must be a Singapore Citizen or Singapore Permanent Resident.
2.2 Singapore Permanent Residents must have held their status for at least 2 years.

Section 3: Income Requirements
3.1 The household income must not exceed $10,000 per month.
3.2 Income shall be calculated based on the average of the last 12 months.
"""

KNOWN_CONDITIONS = [
    "The applicant must be at least 25 years old at the time of application.",
    "The applicant must not be older than 60 years old at the time of application.",
    "The applicant must be a Singapore Citizen or Singapore Permanent Resident.",
    "Singapore Permanent Residents must have held their status for at least 2 years.",
    "The household income must not exceed $10,000 per month.",
    "Income shall be calculated based on the average of the last 12 months.",
]

HALLUCINATED_CONDITIONS = [
    "The applicant must own property in Singapore.",
    "Applicants must have lived in Singapore for at least 5 years.",
    "The application must be submitted before the end of the financial year.",
]


def run_guardrail_test() -> dict:
    results = {
        "known_conditions_verified": 0,
        "hallucinated_conditions_caught": 0,
        "test_cases": [],
    }

    for clause in KNOWN_CONDITIONS:
        verified, score = verify_source_clause(clause, SYNTHETIC_DOCUMENT)
        results["test_cases"].append(
            {
                "clause": clause[:60],
                "type": "known",
                "verified": verified,
                "score": score,
                "expected": True,
            }
        )
        if verified:
            results["known_conditions_verified"] += 1

    for clause in HALLUCINATED_CONDITIONS:
        verified, score = verify_source_clause(clause, SYNTHETIC_DOCUMENT)
        results["test_cases"].append(
            {
                "clause": clause[:60],
                "type": "hallucinated",
                "verified": verified,
                "score": score,
                "expected": False,
            }
        )
        if not verified:
            results["hallucinated_conditions_caught"] += 1

    print("\n=== Guardrail Validation ===")
    print(
        "Known conditions verified: "
        f"{results['known_conditions_verified']}/{len(KNOWN_CONDITIONS)}"
    )
    print(
        "Hallucinated conditions caught: "
        f"{results['hallucinated_conditions_caught']}/{len(HALLUCINATED_CONDITIONS)}"
    )
    for case in results["test_cases"]:
        status = "OK" if case["verified"] == case["expected"] else "FAIL"
        print(f"  {status} [{case['type']:12}] score={case['score']:.0f} | {case['clause']}")

    out_path = Path("eval/results/guardrail_validation.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


if __name__ == "__main__":
    run_guardrail_test()
