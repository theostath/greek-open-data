# Pythia — build plan & status

_Last updated: 2026-06-02. Source of truth for phase scope is `CLAUDE.md §8`; this file
tracks where we actually are and what's next._

## Status at a glance

| Phase | Scope | Status |
|---|---|---|
| 0 | Setup (uv, config, logging, tooling) | ✅ done, committed |
| 1 | API discovery (`client_probe` → `docs/api_findings.md`) | ✅ done, committed |
| 2 | Ingestion (harvest + normalize → SQLite) | ✅ done, committed |
| 3 | Retrieval (embeddings, hybrid search, golden eval) | ✅ done, committed (e5-small) |
| 3.1 | e5-large swap + incremental indexing | ✅ done, committed |
| 4 | Planning (NL → structured query) | ⬜ not started |
| 5 | Access (resilient data client + cache) | ⬜ not started |
| 6 | Synthesis (grounded answer + chart + footer) | ⬜ not started |
| 7 | Interface (FastAPI + HTMX) | ⬜ not started |
| 8 | Eval & hardening | ⬜ not started |

## Direction changes (decided 2026-06-02 — supersede CLAUDE.md §3)

> These override the original tech-stack decisions. Each needs `CLAUDE.md §3` updated and a
> short ADR in `docs/adr/`.

### LLM: local Qwen via Ollama (replaces Anthropic/Claude)
Found locally: **Ollama** with `qwen3.5:9b` (6.6 GB) and `qwen2.5-coder:7b`, serving an
OpenAI-compatible API at `http://localhost:11434/v1` (native `/api/chat` also available).
- [ ] Config: add `llm_base_url` (default `http://localhost:11434/v1`) and `llm_model`
      (default `qwen3.5:9b`); remove `anthropic_api_key` from the LLM path.
- [ ] Planning (Phase 4) + Synthesis (Phase 6): call the local Ollama endpoint via `httpx`
      (or the `openai` SDK pointed at localhost) — no API key, no network egress.
- [ ] Drop the Anthropic dependency/usage; update `.env.example` (remove `ANTHROPIC_API_KEY`).
- [ ] Update `CLAUDE.md §3` (LLM line) + write an ADR.
- [ ] Build-time check: ensure `ollama serve` is running and confirm the exact model tag.

### Frontend: Vercel app (replaces server-rendered Jinja2 + HTMX)
- [ ] Build the frontend as a Vercel-hosted app (e.g. Next.js/React) instead of Jinja2+HTMX.
- [ ] Rescope Phase 7: FastAPI becomes a **JSON API** the frontend calls (decoupled
      frontend/backend), not a server-rendered template app.
- [ ] Update `CLAUDE.md §3` (Frontend line) + write an ADR.
- [ ] ⚠️ **Resolve the hosting tension first:** Vercel is cloud-hosted, but the backend +
      Qwen (`localhost:11434`) + SQLite/Chroma indexes are **local-first**. Either the Vercel
      UI calls a locally-run backend (personal/dev use; backend not publicly deployed) or the
      backend must be hosted (which breaks the local-first MVP). Decide before building.

## What's done (committed)

- **Phase 0–2:** uv project, typed config, structured logging, `make check` green. CKAN
  catalog confirmed (`docs/api_findings.md`); **21,806 datasets / 106,678 resources**
  harvested into `data/catalog.sqlite` with `metadata_modified` provenance on every row.
- **Phase 3 (e5-small baseline):** hybrid `find_dataset()` = dense (Chroma, e5) + lexical
  (SQLite FTS5/BM25) fused with RRF (ADR-0001). `make index` / `make eval`. 26-question
  golden set (el/en/greeklish). **Baseline: MRR 0.48, R@5 0.58, R@10 0.65**
  (el 0.56 / en 0.52 / greeklish 0.30 MRR).
- 64+ tests, ruff + mypy (strict) green. TLS to Hugging Face via the OS trust store
  (`pythia/net.py`, needed for the corporate proxy CA).

## Phase 3.1 — done (e5-large + incremental indexing)

- **e5-large swap:** `config.embedding_model` = `intfloat/multilingual-e5-large`; tests pinned
  to e5-small (fast; keep the 384-dim assertions valid). **Re-eval (n=26): MRR 0.53, R@5 0.62,
  R@10 0.77** (el 0.55 / en 0.71 / greeklish 0.32) — up from e5-small (0.48 / 0.65 R@10).
- **Incremental indexing:** `build_chroma_index` re-embeds only datasets whose `embed_text`
  changed (sha1 signature in Chroma metadata) and drops removed ones; +3 tests. Verified on the
  live 21,806-vector index: after a one-time signature backfill, `make index` re-embeds **0**.

## What's next (later)

### Retrieval quality backlog (Phase 3 follow-ups)
- **Greeklish is the weak spot (0.30 MRR).** Highest-value fix: a **Greeklish→Greek
  transliteration** step before retrieval. (Write an ADR.)
- **Reranker (ADR-0002):** cross-encoder `pythia.retrieval.rerank` landed behind
  `rerank_enabled` (default off); `make eval` runs off-vs-on. Flip ADR to Accepted only if
  it beats hybrid-only on the golden set without regressing `el`. **Eval run still pending.**
- **Expand the golden set** beyond 26 once more datasets are exercised.

### Phase 4 — Planning (`src/pythia/planning/planner.py`)
- NL question → structured query: which dataset, which resource, parameters (date range,
  region, metric). Greek/Greeklish/accent normalization. LLM does language understanding
  only; selection stays inspectable/testable. **LLM = local Qwen via Ollama** (see Direction
  changes), model id in config — no Anthropic.

### Phase 5 — Access (`src/pythia/access/{data_client,cache}.py`)
- Per-resource fetch keyed off `datastore_active` (see `api_findings.md §3`):
  **DataStore** (`datastore_search`, `limit ≤ 32000`, page on `offset`) when active, else
  **file download** (`follow_redirects=True`, fetch `url` fresh — short-lived Azure SAS,
  decode UTF-8 + charset-normalizer fallback), else flag non-tabular as unsupported.
  Wrap in tenacity + SQLite response cache keyed by `resource_id` + `last_modified`.
  Do **not** use the legacy `query/{dataset}` endpoint (gone) or `datastore_search_sql`
  (disabled).

### Phase 6 — Synthesis (`src/pythia/synthesis/answer.py`)
- Grounded answer + Vega-Lite chart spec + freshness/provenance footer (source dataset,
  publisher, `last_updated`). Grounded-or-silent: never fabricate; "no dataset covers this"
  is a valid answer. **Synthesis LLM = local Qwen via Ollama** (see Direction changes).

### Phase 7 — Interface — **Vercel frontend + FastAPI JSON API** (see Direction changes)
- FastAPI exposes the query/answer endpoints as JSON; the chat UI is a Vercel-hosted app
  (Next.js/React) calling it. Resolve the local-first vs cloud-hosting tension first.

### Phase 8 — Eval & hardening
- Broaden eval, honesty checks, observability/structured logging review.

## Operational / infra TODOs

- **No git remote / no push yet** — all commits are local. Add a remote + push when ready.
- **`make` is not installed on this Windows box** — use the `uv run …` equivalents (the
  Makefile is correct wherever `make` exists). Optional: `winget install ezwinports.make`.
- **LF→CRLF** warnings on commit — optionally add a `.gitattributes` to pin LF.
- **Index cost:** full e5-large embed of 21.8k datasets is ~tens of minutes on CPU
  (one-time; incremental now avoids repeating it). A GPU would cut it ~10–50×.

## Key references
- `docs/api_findings.md` — CKAN endpoints, pagination, data-access, schema (source of truth).
- `docs/adr/` — 0001 hybrid retrieval (accepted), 0002 reranker (proposed/eval-gated),
  0003 eval framework (accepted).
