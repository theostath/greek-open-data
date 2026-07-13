# ADR 0004 — LLM provider: local Qwen via Ollama (planning + synthesis)

## Status

Proposed · 2026-07-13 (adopt once Phase 4 lands and the smoke run confirms viable latency)

## Context

CLAUDE.md §3 originally named Claude Sonnet as the planning/synthesis LLM. `plan.md`
("Direction changes", 2026-06-02) supersedes that: an **Ollama** runtime is available locally
(`qwen3.5:9b`, `qwen2.5-coder:7b`) serving an OpenAI-compatible API at
`http://localhost:11434/v1`. Core Principle #3 is *local-first, reproducible, no managed
services*; a hosted LLM contradicts it and adds cost + network egress + an API key on the
hot path. Phase 4 (Planning) is the first module to actually call an LLM, so the decision is
forced now.

## Decision

Use **local Qwen via Ollama** for all LLM calls in `planning/` and `synthesis/`. The shared
transport lives in `src/pythia/llm.py` (`LLMClient` Protocol + `OllamaClient` + `FakeLLM`),
called through the OpenAI-compatible `/v1/chat/completions` endpoint via `httpx` + `tenacity`.
The model id and base URL live in `config.py` (`llm_model`, `llm_base_url`), never inline.
No API key, no network egress.

**Disposition of ADR-0003 (RAGAS):** ADR-0003 still uses an LLM judge (RAGAS) that makes
paid API calls **during dev eval runs only**. That path is out of scope here and unchanged:
`anthropic_api_key` **stays in `config.py` solely for RAGAS** and is documented as such in
`.env.example`. It is never read on the planning/synthesis path. Repointing RAGAS at the
local model is a possible follow-up but is not required by this ADR.

## Rationale

- Honours local-first/reproducible (Principle #3); zero per-query cost; no secret on the
  serve path (Principle #6, no secrets).
- Ollama's OpenAI-compatible surface means no new runtime dependency — the existing `httpx`
  + `tenacity` stack suffices.
- A `Protocol` + `FakeLLM` keeps unit tests offline and deterministic, mirroring the
  established `retrieval/rerank.py` `Scorer` pattern.

## Consequences

- **Latency budget:** this box has **no GPU** (e5-large indexing took tens of minutes on
  CPU). One `qwen3.5:9b` JSON completion on CPU can run seconds to tens of seconds. We cap
  output (`llm_max_tokens`), treat `llm_timeout_s=30` as a **ceiling not a target**, retry
  only connection/5xx/model-loading (never generation timeouts), and record p50/p95 latency
  in the Phase 4 smoke run. If interactive latency (Phase 7) proves unacceptable, revisit
  with a smaller/quantized planning model.
- **Hosting tension (forward-looking):** binding planning + synthesis to `localhost:11434`
  hard-couples the backend to wherever Ollama runs. This forecloses a publicly-hosted
  backend unless revisited — it directly constrains the unresolved Vercel/local-first
  hosting decision deferred to **Phase 7** (`plan.md`). Not resolved here; flagged there.
- CLAUDE.md §3 (LLM line) and `.env.example` are updated; the Anthropic dependency stays
  only for the RAGAS dev-eval path.
