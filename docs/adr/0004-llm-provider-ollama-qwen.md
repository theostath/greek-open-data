# ADR 0004 — LLM provider: local Qwen via Ollama (planning + synthesis)

## Status

**Accepted** · smoke run 2026-07-28 (Proposed 2026-07-13) — adopted with a **transport
change**: Ollama's native `/api/chat`, not the OpenAI-compatible endpoint. See
"Smoke-run findings" below.

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
called through the **native `/api/chat`** endpoint via `httpx` + `tenacity`. The model id and
base URL live in `config.py` (`llm_model`, `llm_base_url`), never inline. No API key, no
network egress.

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

## Smoke-run findings (2026-07-28)

The first end-to-end run against live Ollama found the planner path had **never actually
worked** — every call fell into the degraded score-floor branch. Unit tests missed it because
they inject `FakeLLM`. Two causes:

1. **`qwen3.5:9b` is a reasoning model.** Over `/v1/chat/completions` Ollama streams
   chain-of-thought into a separate `reasoning` field and returns `content: ""`. All 512
   `max_tokens` were spent thinking (`completion_tokens: 512`, i.e. the cap), so there was
   never any JSON to parse. The OpenAI-compat `chat_template_kwargs.enable_thinking: false`
   flag did **not** suppress it (still 76 s, still empty).
2. **`llm_timeout_s = 30` was below the floor.** Real calls took 76–82 s.

**Fix:** call the native `/api/chat` with `think: false` and `format: "json"`. Measured
**76 s → 10.4 s** (~7×) and valid JSON on the first try. `llm_base_url` default becomes
`http://localhost:11434` (a legacy `.../v1` value is still normalized); `llm_timeout_s`
raised to 120 s as a cold-load ceiling. `OllamaClient` now raises a named error on empty
content rather than a confusing JSON parse error.

**Post-fix smoke:** 4 questions end-to-end in 59.6 s (~15 s each including retrieval),
`degraded=False` throughout, and the relevance gate correctly refused a bad match
("the dataset is about asphalt roads in Naxos and does not contain traffic accident data").

## Consequences

- **Latency budget:** this box has **no GPU** (e5-large indexing took ~98 min on CPU). A
  `qwen3.5:9b` JSON completion costs **~10 s warm** with thinking disabled. We cap output
  (`llm_max_tokens`), treat `llm_timeout_s=120` as a **ceiling not a target**, and retry
  only connection/5xx/model-loading (never generation timeouts). ~10 s/query is tolerable
  for a considered answer but is a real Phase 7 UX constraint; revisit with a smaller
  planning model if it bites.
- **Reasoning models need explicit handling.** Any future model swap must re-verify that
  `think: false` is honoured, or the same silent-degradation failure returns. The named
  empty-content error exists to make that loud.
- **Hosting tension (forward-looking):** binding planning + synthesis to `localhost:11434`
  hard-couples the backend to wherever Ollama runs. This forecloses a publicly-hosted
  backend unless revisited — it directly constrains the unresolved Vercel/local-first
  hosting decision deferred to **Phase 7** (`plan.md`). Not resolved here; flagged there.
- CLAUDE.md §3 (LLM line) and `.env.example` are updated; the Anthropic dependency stays
  only for the RAGAS dev-eval path.
