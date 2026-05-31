from pathlib import Path

import pytest

from app.pipeline.pdf_parser import (
    extract_chunks,
    extract_hints,
    filter_obligation_chunks,
    format_for_llm,
)

HDB_PDF = (
    "data/policies/flat-grand-loan-eligilibity-data/couples-and-families/"
    "CPF_housing_grants_for_families_buying_resale_flats_eligibility.pdf"
)


@pytest.mark.skipif(not Path(HDB_PDF).exists(), reason="PDF not available")
class TestPDFParser:
    def test_extracts_chunks(self):
        chunks = extract_chunks(HDB_PDF)
        assert len(chunks) > 5

    def test_filter_keeps_reasonable_fraction(self):
        chunks = extract_chunks(HDB_PDF)
        filtered = filter_obligation_chunks(chunks)
        assert 0.30 <= len(filtered) / len(chunks) <= 1.0

    def test_hints_find_known_thresholds(self):
        chunks = extract_chunks(HDB_PDF)
        hints = extract_hints(chunks)
        values = {hint["value"] for hint in hints.numeric_candidates}
        assert 21 in values or 35 in values or 14000.0 in values

    def test_format_includes_hints(self):
        chunks = extract_chunks(HDB_PDF)
        hints = extract_hints(chunks)
        formatted = format_for_llm(chunks, hints)
        assert "CANDIDATE" in formatted
