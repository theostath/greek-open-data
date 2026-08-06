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
| 4 | Planning (NL → structured query) | ✅ done, merged (eval gate run, 0 wrong matches) |
| 5 | Access (resilient data client + cache) | ✅ done (ADR-0006, verified live) |
| 6 | Synthesis (grounded answer + chart + footer) | ✅ done |
| 7 | Interface (FastAPI + Jinja2 + HTMX, one process) | ✅ done (ADR-0008, verified live) |
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

### ~~Frontend: Vercel app (replaces server-rendered Jinja2 + HTMX)~~
~~- [ ] Build the frontend as a Vercel-hosted app (e.g. Next.js/React) instead of Jinja2+HTMX.~~
~~- [ ] Rescope Phase 7: FastAPI becomes a **JSON API** the frontend calls.~~
~~- [ ] ⚠️ **Resolve the hosting tension first.**~~

**Reversed 2026-08-06 (ADR-0008):** the hosting tension is resolved *against* Vercel. The
blocker this entry left open was never resolvable while staying local-first — Qwen on
`localhost:11434`, a ~2.2 GB embedding model and two local SQLite databases cannot be reached
from a cloud-hosted page without either exposing the laptop or re-hosting the whole stack,
which contradicts Core Principle #3. Phase 7 ships as originally specified in `CLAUDE.md §3`:
**one FastAPI process serving Jinja2 + HTMX**, no build step, no second runtime. A JSON API
for third-party clients is deferred, not cancelled.

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

### Phase 4 — Planning (`src/pythia/planning/planner.py`) — 🟡 code+tests done, eval pending
- `make_plan()` → typed `QueryPlan` (dataset + CSV/JSON resource + validated intent params)
  via normalize → `find_dataset` → `select_resource` → one structured LLM call. Dataset
  **selection** and param **validation** are deterministic/tested; the LLM only proposes.
- **LLM = local Qwen via Ollama** behind an `LLMClient` Protocol + `FakeLLM`
  (`src/pythia/llm.py`, ADR-0004); Anthropic retained **only** for RAGAS. Prompts versioned
  under `planning/prompts/`.
- **Greeklish→Greek transliteration** + `en`-safe detection (`planning/normalize.py`,
  ADR-0005); fed into retrieval and the eval (`run_eval.py --normalize/--no-normalize`).
- Grounded-or-silent: LLM relevance gate (primary) + degraded score-floor fallback; fused
  RRF scores now surfaced on `Candidate` (small Phase-3 change). Opt-in, default-off LLM
  disambiguation (`planning_llm_disambiguate`).
- **Status (2026-07-28): eval gate RUN.** `make check` green (ruff + mypy strict + 113
  tests). The e5-large download was never actually blocked — it works once
  `pythia.net.use_system_trust_store()` is called first; the old `WinError 10054` note was a
  misdiagnosis (that error is a separate, intermittent HF HEAD flake — use
  `HF_HUB_OFFLINE=1` when models are cached).

**Eval matrix** (n=26, e5-large, tombstone-free index, reproducible):

| Arm | OVERALL MRR | R@1 | el | en | greeklish |
|---|---|---|---|---|---|
| norm OFF, rerank OFF | 0.515 | 0.42 | 0.595 | 0.571 | 0.319 |
| norm ON, rerank OFF | 0.544 | 0.46 | 0.595 | 0.571 | 0.429 |
| **norm OFF, rerank ON** | **0.652** | **0.62** | **0.729** | **0.714** | **0.457** |
| norm ON, rerank ON | 0.644 | 0.62 | 0.729 | 0.714 | 0.429 |

- **ADR-0005 → Accepted for the no-reranker config.** Greeklish +0.110 MRR, `el`/`en`
  bit-identical. Does *not* stack with the reranker (they fix the same weakness).
- **ADR-0002 → Accepted, default-off.** +0.137 MRR but **~28 s/query** on CPU.
- **ADR-0004 → Accepted** after the smoke run found the planner LLM path had never worked:
  `qwen3.5:9b` is a reasoning model and returned empty `content` over the OpenAI-compatible
  endpoint. Fixed via native `/api/chat` + `think:false` (76 s → 10 s).

### Phase 4 end-to-end result (golden set through `make_plan`, n=26)

| Outcome | Count |
|---|---|
| `MATCHED` on the **correct** dataset | **6** |
| `MATCHED` on the **wrong** dataset | **0** ← no silent-wrong-answer risk |
| `UNSUPPORTED` (correct dataset, no CSV/JSON) | 6 |
| `NO_MATCH` (retrieval missed) | 12 |

**Grounded-or-silent holds perfectly: the planner never once matched confidently on the
wrong dataset.** Retrieval placed the correct dataset first for 12/26 (46%, consistent
with R@1 0.42–0.46); exactly half of those are then blocked by the dataset having no
CSV/JSON resource. So the ceiling is retrieval quality and resource format — **not the
planner**. Phase 4 is closed on that basis.

### Carried forward (not Phase 4 blockers)

1. ~~**Honesty bug (ordering):** `select_resource` ran before the LLM relevance gate.~~
   **Fixed 2026-07-29:** relevance is decided first, so `UNSUPPORTED` now means "relevant
   but no CSV/JSON" and *"what is the capital of France?"* returns `NO_MATCH`.
2. **Golden set is too small (Phase 8).** At n=26 one question ≈ 0.04 MRR — larger than
   several effects being compared. Per-language slices are n=7–12.
3. **Retrieval R@1 is the binding constraint (Phase 3 follow-up).** 12/26 questions never
   surface the right dataset. The reranker fixes much of this (R@1 0.42 → 0.62) but costs
   ~28 s/query; finding the quality/latency knee across `rerank_pool` sizes is the
   cheapest open lever.
4. **Half the correctly-retrieved datasets have no tabular resource (Phase 5).** Expect
   `UNSUPPORTED` to stay common until non-CSV/JSON handling is considered.

### Chroma tombstone finding

A full re-embed that **upserts over** an existing collection leaves one HNSW tombstone per
row (live 21,806 vs `max seq_id` 42,798). That made ANN results **nondeterministic across
processes** — the same eval returned MRR 0.483–0.526. `build_chroma_index` now drops and
recreates the collection when every row changed (regression-tested). The live index was
repaired by copying vectors into a fresh collection (**no re-embedding**); the old graph is
retained as `datasets_tombstoned` and can be deleted once the new one is trusted.

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

### Phase 7 — Interface — **one FastAPI process serving Jinja2 + HTMX** (ADR-0008)
~~Vercel frontend + FastAPI JSON API.~~ **Reversed 2026-08-06:** local-first won; see the
Direction-changes note above. `make dev` serves the app on `127.0.0.1:8000`. `Pipeline`
(`api/service.py`) is the one orchestration path shared by the CLI and the web app;
`api/view.py` is a publish whitelist so `Answer.plan` never reaches the browser. Three refusal
shapes render distinctly — a `MATCHED`-plan refusal is never framed as a near miss. Assets are
vendored and hash-pinned; an Origin check and CSP cover the browser-reachability that
`access/guard.py:67` warned about.

### Queued between Phase 7 and Phase 8 — decide before building
- **Guided exploration by publisher, place and theme (issue #18).** Geography lives in
  `org_title`, not `spatial_text` (which is 90% "Ελλάδα") and not in free text (Ιωάννινα → 0
  mentions while its municipality publishes plenty). Deterministic SQL, filtered to the 24.4%
  of datasets with CSV/JSON, handing off via `resource_id` — which bypasses retrieval, the
  measured ceiling.
- **LLM chart tooling / Highcharts (no issue yet).** Licensing (proprietary vs BSD Vega) and,
  more importantly, whether letting the model emit chart specs breaks ADR-0007's "the LLM never
  touches the numbers". Needs `/spec` + judge panel + an ADR.

### Phase 8 — Eval & hardening
- Broaden eval, honesty checks, observability/structured logging review.
- **Retrieval remains the highest-value lever** (R@1 0.46 — only 12/26 golden questions put the
  right dataset first). Issue #13 (expand the golden set beyond n=26) is the prerequisite for
  trusting any improvement: at n=26 one question is worth ~0.04 MRR overall and ~0.14 in the
  greeklish slice.

## Operational / infra TODOs

- **Branching model (from 2026-07-29):** `develop` is the default branch and the
  integration point; feature branches stem from `develop` and merge back there. `main` holds
  released state. Remote: `https://github.com/theostath/greek-open-data`.
- **`make` is not installed on this Windows box** — use the `uv run …` equivalents (the
  Makefile is correct wherever `make` exists). Optional: `winget install ezwinports.make`.
- **LF→CRLF** warnings on commit — optionally add a `.gitattributes` to pin LF.
- **Index cost:** full e5-large embed of 21.8k datasets is ~tens of minutes on CPU
  (one-time; incremental now avoids repeating it). A GPU would cut it ~10–50×.

## Key references
- `docs/api_findings.md` — CKAN endpoints, pagination, data-access, schema (source of truth).
- `docs/adr/` — 0001 hybrid retrieval (accepted), 0002 reranker (proposed/eval-gated),
  0003 eval framework (accepted).
