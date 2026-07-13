"""Tests for the shared LLM transport (Ollama client + fake), Phase 4."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from pythia.llm import FakeLLM, LLMError, OllamaClient


def _client(attempts: int = 3) -> OllamaClient:
    """Build an OllamaClient pointed at a dummy URL (network is monkeypatched)."""
    return OllamaClient(
        base_url="http://localhost:11434/v1", model="test", timeout_s=1.0,
        temperature=0.0, attempts=attempts,
    )


def _envelope(content: str) -> dict[str, Any]:
    """Wrap model content in an OpenAI-compatible chat response envelope."""
    return {"choices": [{"message": {"content": content}}]}


def test_fake_llm_returns_canned_response() -> None:
    """FakeLLM echoes its configured response and records the call."""
    fake = FakeLLM({"relevant": True})
    result = fake.complete_json([{"role": "user", "content": "hi"}], max_tokens=8)
    assert result == {"relevant": True}
    assert len(fake.calls) == 1


def test_fake_llm_raises_configured_error() -> None:
    """FakeLLM(error=...) raises on every call."""
    fake = FakeLLM(error=LLMError("boom"))
    with pytest.raises(LLMError):
        fake.complete_json([], max_tokens=8)


def test_complete_json_parses_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid JSON content string is parsed into a dict."""
    def fake_post(self: Any, url: str, json: Any) -> httpx.Response:
        return httpx.Response(200, json=_envelope('{"relevant": true, "params": {}}'))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    result = _client().complete_json([{"role": "user", "content": "q"}], max_tokens=16)
    assert result == {"relevant": True, "params": {}}


def test_complete_json_rejects_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-JSON content raises LLMError."""
    def fake_post(self: Any, url: str, json: Any) -> httpx.Response:
        return httpx.Response(200, json=_envelope("not json at all"))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(LLMError):
        _client().complete_json([], max_tokens=16)


def test_complete_json_rejects_non_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """A JSON array (not an object) raises LLMError."""
    def fake_post(self: Any, url: str, json: Any) -> httpx.Response:
        return httpx.Response(200, json=_envelope("[1, 2]"))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(LLMError):
        _client().complete_json([], max_tokens=16)


def test_server_error_retries_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persistent 5xx is retried up to `attempts` times, then surfaces as LLMError."""
    calls = {"n": 0}

    def fake_post(self: Any, url: str, json: Any) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(LLMError):
        _client(attempts=3).complete_json([], max_tokens=16)
    assert calls["n"] == 3


def test_read_timeout_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A generation ReadTimeout fails fast (no retry) and surfaces as LLMError."""
    calls = {"n": 0}

    def fake_post(self: Any, url: str, json: Any) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(LLMError):
        _client(attempts=3).complete_json([], max_tokens=16)
    assert calls["n"] == 1
