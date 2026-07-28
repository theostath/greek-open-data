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
    """Wrap model content in an Ollama native /api/chat response envelope."""
    return {"message": {"role": "assistant", "content": content}}


def test_uses_native_chat_endpoint_with_thinking_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request targets /api/chat and disables thinking.

    Qwen3.5 is a reasoning model: left enabled, it spends the whole token budget on
    chain-of-thought and returns empty content, so every structured call fails.
    """
    seen: dict[str, Any] = {}

    def fake_post(self: Any, url: str, json: Any) -> httpx.Response:
        seen["url"] = url
        seen["payload"] = json
        return httpx.Response(200, json=_envelope('{"relevant": true}'))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    _client().complete_json([{"role": "user", "content": "q"}], max_tokens=16)

    assert seen["url"].endswith("/api/chat")
    assert "/v1" not in seen["url"]  # legacy base URL must be normalized away
    assert seen["payload"]["think"] is False
    assert seen["payload"]["format"] == "json"
    assert seen["payload"]["options"]["num_predict"] == 16


def test_empty_content_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty content (the reasoning-model failure mode) gets a named error, not a parse error."""
    def fake_post(self: Any, url: str, json: Any) -> httpx.Response:
        return httpx.Response(200, json=_envelope(""))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(LLMError, match="empty LLM content"):
        _client().complete_json([], max_tokens=16)


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
