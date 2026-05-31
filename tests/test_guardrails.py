from app.pipeline.guardrails import verify_source_clause


def test_exact_match_verifies():
    ok, score = verify_source_clause(
        "buyer must be at least 21 years old",
        "The buyer must be at least 21 years old at the time of application",
    )
    assert ok, f"Expected match, got score {score}"


def test_hallucinated_clause_fails():
    ok, score = verify_source_clause(
        "buyer must own three cats to qualify",
        "The buyer must be at least 21 years old at the time of application",
    )
    assert not ok, f"Expected hallucination to fail, got score {score}"


def test_minor_formatting_still_matches():
    ok, score = verify_source_clause(
        "buyer must be at least 21 years old",
        "The buyer must be at least 21 years old.",
    )
    assert ok, f"Minor formatting difference should still match, got score {score}"


def test_empty_clause_fails():
    ok, _score = verify_source_clause("", "any document text")
    assert not ok
