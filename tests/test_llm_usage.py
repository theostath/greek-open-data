"""Token accounting for LLM calls (issue #22).

Ollama returns ``prompt_eval_count`` / ``eval_count`` in **every** ``/api/chat`` response and
the client previously read only ``message.content`` — the numbers were on the wire and thrown
away on every call.

Accumulation is **thread-local on purpose**. One ``OllamaClient`` is shared by the whole
process and up to ``api_max_concurrent_jobs`` worker threads call it at once, so a plain
``last_usage`` attribute would interleave two questions' tokens.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from config import Settings

from pythia.llm import FakeLLM, OllamaClient, Usage


def _client(**kw: Any) -> OllamaClient:
    cfg = Settings()
    return OllamaClient(
        base_url=cfg.llm_base_url, model=cfg.llm_model, timeout_s=1.0,
        temperature=0.0, **kw,
    )


def _reply(monkeypatch: pytest.MonkeyPatch, *, prompt: int, completion: int) -> None:
    """Stub the transport with an Ollama-shaped envelope carrying usage counts."""
    def fake_post(self: OllamaClient, payload: dict[str, Any]) -> str:
        self._record_usage({  # type: ignore[attr-defined]
            "prompt_eval_count": prompt, "eval_count": completion,
            "total_duration": 2_000_000_000,
        })
        return '{"ok": true}'
    monkeypatch.setattr(OllamaClient, "_post", fake_post)


def test_usage_starts_empty() -> None:
    assert _client().drain_usage() == Usage()


def test_usage_accumulates_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single question makes two LLM calls — planner then narrator — and both count."""
    _reply(monkeypatch, prompt=100, completion=20)
    client = _client()

    client.complete_json([{"role": "user", "content": "a"}], max_tokens=8)
    client.complete_json([{"role": "user", "content": "b"}], max_tokens=8)

    usage = client.drain_usage()
    assert usage.prompt_tokens == 200
    assert usage.completion_tokens == 40
    assert usage.calls == 2


def test_draining_resets_so_the_next_question_starts_clean(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metrics are per question; carrying a previous one's tokens would inflate every row."""
    _reply(monkeypatch, prompt=10, completion=5)
    client = _client()
    client.complete_json([], max_tokens=8)

    client.drain_usage()

    assert client.drain_usage() == Usage()


def test_usage_does_not_leak_between_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent questions share the client; their token counts must not interleave."""
    _reply(monkeypatch, prompt=7, completion=3)
    client = _client()
    seen: dict[str, Usage] = {}
    started = threading.Barrier(2)

    def work(name: str, calls: int) -> None:
        started.wait(timeout=5)
        for _ in range(calls):
            client.complete_json([], max_tokens=8)
        seen[name] = client.drain_usage()

    threads = [threading.Thread(target=work, args=("a", 1)),
               threading.Thread(target=work, args=("b", 3))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert seen["a"].calls == 1 and seen["a"].prompt_tokens == 7
    assert seen["b"].calls == 3 and seen["b"].prompt_tokens == 21


def test_a_response_without_usage_fields_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older Ollama, or a different backend, simply reports zero rather than crashing."""
    def fake_post(self: OllamaClient, payload: dict[str, Any]) -> str:
        self._record_usage({})  # type: ignore[attr-defined]
        return '{"ok": true}'
    monkeypatch.setattr(OllamaClient, "_post", fake_post)
    client = _client()

    client.complete_json([], max_tokens=8)

    usage = client.drain_usage()
    assert usage.calls == 1
    assert usage.prompt_tokens == 0 and usage.completion_tokens == 0


def test_the_fake_reports_zero_tokens_but_counts_calls() -> None:
    """Keeps the offline suite deterministic while still exercising the metrics path."""
    fake = FakeLLM({"ok": True})

    fake.complete_json([], max_tokens=8)
    fake.complete_json([], max_tokens=8)

    usage = fake.drain_usage()
    assert usage.calls == 2
    assert usage.prompt_tokens == 0 and usage.completion_tokens == 0


def test_usage_totals_add() -> None:
    """``Usage`` is summed across call sites, so addition has to be defined."""
    assert Usage(10, 2, 1, 100.0) + Usage(5, 1, 1, 50.0) == Usage(15, 3, 2, 150.0)
