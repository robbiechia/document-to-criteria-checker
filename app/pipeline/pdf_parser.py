"""Deterministic PDF processing for Stage 1 extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pdfplumber


@dataclass
class Chunk:
    text: str
    clause_id: str
    page_num: int


@dataclass
class HintSet:
    numeric_candidates: list[dict[str, Any]] = field(default_factory=list)
    enum_candidates: list[dict[str, Any]] = field(default_factory=list)
    date_candidates: list[dict[str, Any]] = field(default_factory=list)


OBLIGATION_PATTERN = re.compile(
    r"\b(must|shall|is required|will not be eligible|cannot|"
    r"is not allowed|is prohibited|is eligible|may not|"
    r"subject to|provided that|in accordance with|"
    r"at least|not exceeding|not more than|no more than|"
    r"within \d+|at least \d+|not less than|must not|"
    r"eligible for|not eligible|required to)\b",
    re.IGNORECASE,
)

CLAUSE_HEADING = re.compile(
    r"^(?P<id>(?:\d+(?:\.\d+)*|[a-z]\)|\([a-z]\)|[ivx]+\)|\([ivx]+\)))\s+",
    re.IGNORECASE,
)

NUMBERED_CLAUSE_START = re.compile(
    r"(?m)(?=^\s*(?:\d+(?:\.\d+)+|\([a-z]\)|[a-z]\)|\([ivx]+\)|[ivx]+\))\s+)"
)

YEAR_THRESHOLD = re.compile(
    r"(\d+)[- ]year(?:s)?\s+(?:minimum\s+)?(?:occupation\s+period|MOP|"
    r"wait[- ]out|period|tenure|status|lease)",
    re.IGNORECASE,
)
MONTH_THRESHOLD = re.compile(
    r"(\d+)\s+month(?:s)?\s+(?:from|before|after|within|of|period)",
    re.IGNORECASE,
)
AGE_THRESHOLD = re.compile(
    r"(?:at least|aged?|must be|age(?:d)?|above)\s+(\d+)\s+years?\s+old|"
    r"(\d+)\s+years?\s+old\s+or\s+above",
    re.IGNORECASE,
)
DOLLAR_THRESHOLD = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
CITIZENSHIP_TERMS = re.compile(
    r"\b(Singapore Citizen|SC|Singapore Permanent Resident|SPR|PR|"
    r"non-Singapore citizen|non-resident)\b",
    re.IGNORECASE,
)


def _normalise_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _chunk_page_text(text: str, page_num: int) -> list[Chunk]:
    text = _normalise_text(text)
    if not text:
        return []

    pieces = [p.strip() for p in NUMBERED_CLAUSE_START.split(text) if p.strip()]
    if len(pieces) <= 1:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) > 20]
        return [Chunk(text=p, clause_id=f"p{page_num}-prose-{i+1}", page_num=page_num) for i, p in enumerate(paras)]

    chunks: list[Chunk] = []
    for i, piece in enumerate(pieces, 1):
        if len(piece) <= 20:
            continue
        first_line = piece.splitlines()[0].strip()
        match = CLAUSE_HEADING.match(first_line)
        clause_id = match.group("id").strip() if match else f"p{page_num}-clause-{i}"
        chunks.append(Chunk(text=piece, clause_id=clause_id, page_num=page_num))
    return chunks


def extract_chunks(pdf_path: str) -> list[Chunk]:
    """Extract text from a PDF while preserving coarse page/clause context."""
    path = Path(pdf_path)
    if path.suffix.lower() == ".txt":
        return _chunk_page_text(path.read_text(encoding="utf-8"), 1)

    chunks: list[Chunk] = []
    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            text = page.extract_text(layout=False) or ""
            chunks.extend(_chunk_page_text(text, page_num))
    return [c for c in chunks if len(c.text.strip()) > 20]


def filter_obligation_chunks(chunks: list[Chunk]) -> list[Chunk]:
    """Keep obligation-bearing chunks, falling back if the filter is too aggressive."""
    if not chunks:
        return []
    filtered = [c for c in chunks if OBLIGATION_PATTERN.search(c.text)]
    if len(filtered) < len(chunks) * 0.30:
        return chunks
    return filtered


def _context(full_text: str, start: int, end: int, pad: int = 45) -> str:
    return full_text[max(0, start - pad) : min(len(full_text), end + pad)].strip()


def extract_hints(chunks: list[Chunk]) -> HintSet:
    """Extract candidate values to present to the LLM as hints, not facts."""
    hints = HintSet()
    full_text = "\n".join(c.text for c in chunks)

    for regex, unit, target in (
        (YEAR_THRESHOLD, "years", hints.date_candidates),
        (MONTH_THRESHOLD, "months", hints.date_candidates),
    ):
        for match in regex.finditer(full_text):
            target.append(
                {
                    "value": int(match.group(1)),
                    "unit": unit,
                    "context": _context(full_text, match.start(), match.end()),
                }
            )

    for match in AGE_THRESHOLD.finditer(full_text):
        value = match.group(1) or match.group(2)
        if value is None:
            continue
        hints.numeric_candidates.append(
            {
                "value": int(value),
                "unit": "age_years",
                "context": _context(full_text, match.start(), match.end()),
            }
        )

    for match in DOLLAR_THRESHOLD.finditer(full_text):
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        hints.numeric_candidates.append(
            {
                "value": value,
                "unit": "SGD",
                "context": _context(full_text, match.start(), match.end(), 60),
            }
        )

    counts: dict[str, int] = {}
    for match in CITIZENSHIP_TERMS.finditer(full_text):
        term = match.group(1).strip()
        counts[term] = counts.get(term, 0) + 1
    hints.enum_candidates = [
        {"term": term, "frequency": count}
        for term, count in sorted(counts.items(), key=lambda item: -item[1])
    ]

    for attr in ("numeric_candidates", "date_candidates"):
        seen: set[tuple[Any, str]] = set()
        deduped = []
        for item in getattr(hints, attr):
            key = (item["value"], item["unit"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        setattr(hints, attr, deduped)

    return hints


def format_for_llm(chunks: list[Chunk], hints: Optional[HintSet] = None) -> str:
    """Render chunks and optional hints as a single prompt section."""
    doc_text = "\n\n".join(
        f"[{chunk.clause_id} | p.{chunk.page_num}]\n{chunk.text}" for chunk in chunks
    )
    if hints is None:
        return doc_text

    hint_lines: list[str] = []
    if hints.date_candidates:
        hint_lines.append("\n=== CANDIDATE DURATION VALUES (verify against document) ===")
        for hint in hints.date_candidates[:12]:
            hint_lines.append(
                f"- {hint['value']} {hint['unit']}: \"{hint['context'][:120]}\""
            )
    if hints.numeric_candidates:
        hint_lines.append("\n=== CANDIDATE NUMERIC THRESHOLDS (verify against document) ===")
        for hint in hints.numeric_candidates[:12]:
            hint_lines.append(
                f"- {hint['value']} {hint['unit']}: \"{hint['context'][:120]}\""
            )
    if hints.enum_candidates:
        hint_lines.append("\n=== CANDIDATE ENUM/CATEGORY VALUES ===")
        for hint in hints.enum_candidates[:12]:
            hint_lines.append(f"- \"{hint['term']}\" (appears {hint['frequency']}x)")

    return doc_text + "\n" + "\n".join(hint_lines)
