"""Thin wrapper around OpenRouter API used by the extraction pipeline."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

import base64
import pathlib

from openai import OpenAI
from dotenv import load_dotenv

import app.config as _cfg

load_dotenv()

_client: Optional[OpenAI] = None

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass
class CompletionResult:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    model: str
    provider: str = "openrouter"


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY not set. Add it to your .env file. "
                "Get your key at https://openrouter.ai/keys"
            )
        _client = OpenAI(
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
        )
    return _client


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines).strip()
    return text


_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _file_content_block(file_path: str) -> dict:
    """Return an OpenRouter content block for a PDF or image file."""
    suffix = pathlib.Path(file_path).suffix.lower()
    raw = pathlib.Path(file_path).read_bytes()
    b64 = base64.b64encode(raw).decode("utf-8")
    if suffix == ".pdf":
        mime = "application/pdf"
    elif suffix in _IMAGE_MIME:
        mime = _IMAGE_MIME[suffix]
    else:
        raise ValueError(f"Unsupported file type: {suffix}")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def complete_with_file(
    system: str,
    user: str,
    file_path: str,
    model: Optional[str] = None,
    max_tokens: int = 8192,
    temperature: float = 0.0,
) -> CompletionResult:
    """Call a multimodal LLM with a PDF or image passed as inline base64 content.

    Supports: .pdf, .png, .jpg, .jpeg, .webp, .gif
    The model reads the file natively — no pre-extraction.
    """
    model_name = model or _cfg.get("MODEL_STAGE1", "google/gemini-3.5-flash")
    client = _get_client()

    start = time.time()
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user},
                    _file_content_block(file_path),
                ],
            },
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    latency_ms = (time.time() - start) * 1000

    usage = response.usage
    raw_text = (response.choices[0].message.content or "").strip()

    return CompletionResult(
        text=_strip_markdown_fences(raw_text),
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        latency_ms=latency_ms,
        model=model_name,
        provider="openrouter",
    )


def ocr_image(image_path: str, model: Optional[str] = None) -> str:
    """Extract all readable text from an infographic image using a multimodal LLM.

    Returns a flat text string suitable for the hallucination guardrail's
    fuzzy source-clause verification. Preserves numbers, labels, and categories.
    """
    model_name = model or _cfg.get("MODEL_STAGE1", "google/gemini-3.5-flash")
    result = complete_with_file(
        system=(
            "You are an OCR system. Extract all readable text from the image exactly "
            "as it appears. Preserve all numbers, labels, category names, and eligibility "
            "criteria text. Output only the extracted text with no commentary or formatting."
        ),
        user="Extract all text from this image.",
        file_path=image_path,
        model=model_name,
        max_tokens=4096,
        temperature=0.0,
    )
    return result.text


# Keep backwards-compatible aliases
def complete_with_pdf(system, user, pdf_path, model=None, max_tokens=8192, temperature=0.0):
    return complete_with_file(system, user, pdf_path, model, max_tokens, temperature)


def complete_with_pdf_and_text(
    system: str,
    user: str,
    pdf_path: str,
    model: Optional[str] = None,
    max_tokens: int = 8192,
    temperature: float = 0.0,
) -> CompletionResult:
    """Call the LLM with pdfplumber-extracted text AND the raw PDF attached.

    The extracted text is the primary context (structured, easy to reference).
    The PDF is attached so the model can verify layout, tables, and structure
    that text extraction may have flattened or lost.
    """
    model_name = model or _cfg.get("MODEL_STAGE1", "google/gemini-2.5-flash")
    client = _get_client()

    pdf_bytes = pathlib.Path(pdf_path).read_bytes()
    pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

    start = time.time()
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:application/pdf;base64,{pdf_b64}"},
                    },
                ],
            },
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    latency_ms = (time.time() - start) * 1000

    usage = response.usage
    raw_text = (response.choices[0].message.content or "").strip()

    return CompletionResult(
        text=_strip_markdown_fences(raw_text),
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        latency_ms=latency_ms,
        model=model_name,
        provider="openrouter",
    )


def complete(
    system: str,
    user: str,
    model: Optional[str] = None,
    max_tokens: int = 8192,
    temperature: float = 0.0,
) -> CompletionResult:
    """Call an LLM via OpenRouter and return a CompletionResult.

    Any OpenRouter model string is accepted — e.g.
      google/gemini-2.5-flash, google/gemini-2.5-pro,
      openai/gpt-4o, anthropic/claude-sonnet-4-6
    The default falls back to MODEL_PRIMARY in the environment.
    """
    model_name = model or _cfg.get("MODEL_STAGE1", "google/gemini-2.5-flash")
    client = _get_client()

    start = time.time()
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    latency_ms = (time.time() - start) * 1000

    usage = response.usage
    raw_text = (response.choices[0].message.content or "").strip()

    return CompletionResult(
        text=_strip_markdown_fences(raw_text),
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        latency_ms=latency_ms,
        model=model_name,
        provider="openrouter",
    )
