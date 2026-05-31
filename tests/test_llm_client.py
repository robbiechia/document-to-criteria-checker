"""Tests for the OpenRouter LLM client wrapper."""

import os
import pytest
from unittest.mock import MagicMock, patch

from app.utils.llm_client import _strip_markdown_fences, complete, CompletionResult


def test_strip_markdown_fences_json():
    raw = "```json\n{\"key\": 1}\n```"
    assert _strip_markdown_fences(raw) == '{"key": 1}'


def test_strip_markdown_fences_plain():
    raw = "no fences here"
    assert _strip_markdown_fences(raw) == "no fences here"


def test_strip_markdown_fences_no_close():
    raw = "```\nsome text"
    assert _strip_markdown_fences(raw) == "some text"


def test_complete_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Reset cached client so it renegotiates on next call
    import app.utils.llm_client as mod
    mod._client = None
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        complete("system prompt", "user message")


def test_complete_returns_completion_result(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    import app.utils.llm_client as mod
    mod._client = None

    mock_response = MagicMock()
    mock_response.choices[0].message.content = "hello"
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 5

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("app.utils.llm_client._get_client", return_value=mock_client):
        result = complete("system", "user", model="google/gemini-2.5-flash")

    assert isinstance(result, CompletionResult)
    assert result.text == "hello"
    assert result.provider == "openrouter"
    assert result.model == "google/gemini-2.5-flash"
    assert result.input_tokens == 10
    assert result.output_tokens == 5
