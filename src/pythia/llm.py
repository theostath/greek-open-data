"""Shared LLM transport: local Qwen via Ollama's native ``/api/chat`` API (ADR-0004).

Neutral home so both ``planning/`` and ``synthesis/`` can call an LLM without importing
one from the other. ``LLMClient`` is a minimal ``Protocol`` (mirroring
``retrieval.rerank.Scorer``) so unit tests inject ``FakeLLM`` and never hit the network.
Callers own prompt construction and response validation; this module only moves JSON.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from config import Settings, get_settings
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

Message = dict[str, str]


class LLMError(Exception):
    """Raised when the LLM call fails or returns unparseable JSON."""


class _RetryableStatus(Exception):
    """Internal marker for a 5xx / model-loading response worth retrying."""


@dataclass(frozen=True)
class Usage:
    """Tokens and wall-clock consumed by LLM calls, summable across call sites."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    total_ms: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        """Add two usages; one question spans a planner call and a narrator call."""
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            calls=self.calls + other.calls,
            total_ms=self.total_ms + other.total_ms,
        )


class LLMClient(Protocol):
    """Minimal chat interface: send messages, get back a parsed JSON object."""

    def complete_json(self, messages: list[Message], *, max_tokens: int) -> dict[str, Any]:
        """Return the model's response parsed as a JSON object (raises ``LLMError``)."""
        ...

    def drain_usage(self) -> Usage:
        """Return usage accumulated **on this thread** since the last drain, and reset."""
        ...


class OllamaClient:
    """``LLMClient`` backed by Ollama's native ``/api/chat`` endpoint.

    Uses the native API rather than the OpenAI-compatible one specifically to send
    ``think: false``. Qwen3.5 is a reasoning model: over ``/v1/chat/completions`` it
    streams chain-of-thought into a separate ``reasoning`` field, exhausts the token
    budget, and returns an **empty** ``content`` — so every structured call fails. The
    OpenAI-compat ``chat_template_kwargs.enable_thinking`` flag does not suppress it.
    Disabling thinking natively both fixes the parse and cuts latency ~7x (measured
    76s -> 10s on CPU).

    Retries only connection errors and 5xx / model-loading responses; a generation
    ``ReadTimeout`` is *not* retried (it would just burn another full timeout on a slow
    CPU) — it propagates so the caller degrades promptly.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_s: float,
        temperature: float,
        attempts: int = 3,
    ) -> None:
        """Configure the client; nothing is sent until ``complete_json`` is called."""
        # Thread-local, not an instance attribute: one client is shared by the whole process
        # and up to ``api_max_concurrent_jobs`` worker threads call it at once, so a plain
        # attribute would interleave two questions' token counts.
        self._usage = threading.local()
        # Tolerate a legacy ``.../v1`` base URL so existing .env files keep working.
        self._url = base_url.rstrip("/").removesuffix("/v1") + "/api/chat"
        self._model = model
        self._timeout_s = timeout_s
        self._temperature = temperature
        self._attempts = attempts

    def complete_json(self, messages: list[Message], *, max_tokens: int) -> dict[str, Any]:
        """POST the chat request and return the response content parsed as JSON."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "think": False,
            "options": {"temperature": self._temperature, "num_predict": max_tokens},
        }
        content = self._request(payload)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"non-JSON LLM response: {exc}") from exc
        if not isinstance(parsed, dict):
            raise LLMError("LLM response was not a JSON object")
        return parsed

    def _request(self, payload: dict[str, Any]) -> str:
        """Send the request with bounded retries; return the message content string."""
        retryer = Retrying(
            retry=retry_if_exception_type((httpx.ConnectError, _RetryableStatus)),
            stop=stop_after_attempt(self._attempts),
            wait=wait_exponential(multiplier=0.5, max=4.0),
            reraise=True,
        )
        try:
            for attempt in retryer:
                with attempt:
                    return self._post(payload)
        except (httpx.HTTPError, _RetryableStatus) as exc:
            raise LLMError(str(exc)) from exc
        raise LLMError("LLM request exhausted retries")  # pragma: no cover

    def _post(self, payload: dict[str, Any]) -> str:
        """Perform one POST; raise ``_RetryableStatus`` on 5xx, else parse the content."""
        with httpx.Client(timeout=self._timeout_s) as client:
            resp = client.post(self._url, json=payload)
        if resp.status_code >= 500:
            raise _RetryableStatus(f"{resp.status_code} from LLM")
        if resp.status_code >= 400:
            resp.raise_for_status()  # 4xx -> HTTPStatusError (not retried)
        data = resp.json()
        self._record_usage(data)
        try:
            content: str = data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMError(f"unexpected LLM envelope: {exc}") from exc
        if not content.strip():
            # Named explicitly: the reasoning-model failure mode this client exists to
            # avoid. A bare json.loads("") would surface as a confusing parse error.
            raise LLMError(
                "empty LLM content (model emitted reasoning instead of an answer; "
                "check that 'think: false' is honoured by this model)"
            )
        return content


    def _record_usage(self, data: dict[str, Any]) -> None:
        """Accumulate the counts Ollama already sends, tolerating their absence.

        A backend that omits them (an older Ollama, or something else speaking this shape)
        reports zero rather than raising: metrics are observability, and must never be able to
        fail an answer.
        """
        nanoseconds = data.get("total_duration") or 0
        current: Usage = getattr(self._usage, "value", Usage())
        self._usage.value = current + Usage(
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
            calls=1,
            total_ms=float(nanoseconds) / 1_000_000.0,
        )

    def drain_usage(self) -> Usage:
        """Return this thread's accumulated usage and reset it."""
        current: Usage = getattr(self._usage, "value", Usage())
        self._usage.value = Usage()
        return current


class FakeLLM:
    """Deterministic ``LLMClient`` test double: returns a fixed dict or raises."""

    def __init__(
        self, response: dict[str, Any] | None = None, *, error: Exception | None = None
    ) -> None:
        """Configure the canned ``response`` (or an ``error`` to raise on every call)."""
        self._response = response if response is not None else {}
        self._error = error
        self._usage = Usage()
        self.calls: list[list[Message]] = []

    def complete_json(self, messages: list[Message], *, max_tokens: int) -> dict[str, Any]:
        """Record the call and return the canned response (or raise the canned error)."""
        self.calls.append(messages)
        self._usage += Usage(calls=1)
        if self._error is not None:
            raise self._error
        return self._response

    def drain_usage(self) -> Usage:
        """Report calls but zero tokens, so the offline suite stays deterministic."""
        current, self._usage = self._usage, Usage()
        return current


def load_llm(settings: Settings | None = None) -> OllamaClient:
    """Build the configured Ollama client (defaults from ``config``)."""
    cfg = settings or get_settings()
    return OllamaClient(
        base_url=cfg.llm_base_url,
        model=cfg.llm_model,
        timeout_s=cfg.llm_timeout_s,
        temperature=cfg.llm_temperature,
    )
