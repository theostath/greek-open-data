# Pythia

> Natural-language query assistant over the Greek national open-data portal
> ([data.gov.gr](https://data.gov.gr)). Ask a question in **Greek or English** → get the
> right dataset, a **grounded, cited answer**, and a chart — with a freshness/provenance footer.

**Status:** early development. Phase 0 (setup) and Phase 1 (API discovery) complete; Phase 2
(ingestion) is next. See the [roadmap](#roadmap).

---

## Why

Greece publishes ~22,000 open datasets, but the portal's real weakness is **discoverability**,
not data volume. Pythia attacks that with retrieval + grounded synthesis: you type a question,
it finds the dataset, fetches the values, and answers — or tells you plainly that no dataset
covers your question. It never fabricates figures.

## How it works

```
question ──▶ retrieval ──▶ planning ──▶ access ──▶ synthesis ──▶ answer + chart
            (find dataset) (NL→query)  (fetch data) (grounded)   (+ provenance footer)
```

LLMs do only language understanding and synthesis. Dataset selection, parameter extraction,
and data fetching are deterministic and testable — not vibes. Every answer names its source
dataset, publisher, and `last_updated`.

## Tech stack

| Layer | Choice |
| --- | --- |
| Language / env | Python 3.11+, [`uv`](https://docs.astral.sh/uv/) |
| Backend | FastAPI + Uvicorn |
| Frontend | Server-rendered Jinja2 + HTMX (no SPA build step) |
| Metadata store | SQLite (committed schema, plain-SQL migrations) |
| Vector store | Chroma (persistent, local) |
| Embeddings | `intfloat/multilingual-e5-large` (local, zero cost); Voyage AI fallback |
| Retrieval | Dense + BM25 (SQLite FTS5) hybrid, RRF fusion, optional reranker — see [ADRs](docs/adr/) |
| LLM | Claude Sonnet (model id in config, not hardcoded) |
| HTTP | httpx (async) + tenacity retries |
| Charts | Vega-Lite JSON specs, rendered client-side |
| Quality | pytest · ruff · mypy |

Local-first and reproducible: the MVP runs entirely on a laptop with no managed services.

## Repository layout

```
config.py              # typed settings (pydantic-settings)
src/pythia/
  ingest/              # API discovery + catalog harvest
  retrieval/           # embed + search metadata
  planning/            # NL -> structured query
  access/              # resilient data fetch + cache
  synthesis/           # grounded answer + chart spec
  api/                 # FastAPI routes
  eval/                # golden set + scoring
docs/
  api_findings.md      # curated API source of truth
  api_probe_raw.md     # auto-generated probe evidence
  adr/                 # architecture decision records
```

## Quickstart

**Prerequisites:** Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). `make` is optional
(every target has a one-line `uv run` equivalent below).

```bash
# 1. Install dependencies into a local .venv
uv sync

# 2. Configure environment
cp .env.example .env        # then edit .env
#   DATA_GOV_GR_TOKEN  — portal token (catalog reads are anonymous; not required yet)
#   ANTHROPIC_API_KEY  — RAGAS dev-eval ONLY (ADR-0003); never read on the serve path

# Planning/synthesis use a LOCAL model — no API key. Requires Ollama running with
# the configured model (config.llm_model, default qwen3.5:9b):
#   ollama pull qwen3.5:9b

# 3. Verify the toolchain is green
uv run ruff check . && uv run mypy && uv run pytest -q

# 4. Probe the live API (read-only) and refresh the evidence file
uv run python -m pythia.ingest.client_probe   # -> docs/api_probe_raw.md
```

> **Behind a corporate proxy?** This project sets `native-tls = true` in `pyproject.toml` so
> `uv` uses the OS trust store. The API probe likewise builds its TLS context from the system
> store.

## Commands

| Task | `make` | Direct |
| --- | --- | --- |
| Install deps | `make setup` | `uv sync` |
| API probe | `make probe` | `uv run python -m pythia.ingest.client_probe` |
| Lint + types + tests | `make check` | `uv run ruff check . && uv run mypy && uv run pytest -q` |
| Harvest catalog | `make harvest` | `uv run python -m pythia.ingest.harvest` |
| Build indexes | `make index` | `uv run python -m pythia.retrieval.index` |
| Retrieval eval | `make eval` | `uv run python -m pythia.eval.run_eval` |
| Dev server | `make dev` | *(Phase 7)* |

> `make index` is **incremental** — it re-embeds only datasets whose text changed (and drops
> removed ones), so refreshing the catalog doesn't re-embed everything. When *every* dataset
> changed it rebuilds the collection from scratch, so the HNSW graph stays tombstone-free.
> A full CPU rebuild of all 21,806 datasets takes ~98 min — see
> [`docs/benchmarks/embedding-index-build.md`](docs/benchmarks/embedding-index-build.md).
>
> Retrieval baseline (e5-large, 26 golden questions): **MRR 0.515, R@1 0.42, R@10 0.69**.
> With Greeklish normalization: **MRR 0.544** (greeklish slice 0.319 → 0.429).
> With the opt-in cross-encoder reranker (`RERANK_ENABLED=true`): **MRR 0.652, R@1 0.62** —
> but ~28 s/query on CPU, which is why it ships **off** (ADR-0002).

## Data source

The catalog is **CKAN 2.11.3**; metadata is served by the standard Action API at
`https://data.gov.gr/api/3/action/…` (anonymous reads). Data is fetched per-resource via the
CKAN DataStore or a direct file download. The legacy `/api/v1/query/{dataset}` API was removed
in the May 2026 relaunch. Full details — endpoints, pagination, schema, gotchas — live in
[`docs/api_findings.md`](docs/api_findings.md).

## Where the data & embeddings live

All of these are **local build artifacts** (gitignored) — regenerate them with
`make harvest` then `make index`; nothing here is committed.

| Artifact | Location | In git? |
| --- | --- | --- |
| Catalog metadata (`datasets`, `resources`) | `data/catalog.sqlite` | gitignored |
| Lexical index (FTS5 / BM25) | `datasets_fts` table inside `data/catalog.sqlite` | gitignored |
| Dense embedding **vectors** (Chroma) | `data/chroma/` (`chroma.sqlite3` + collection dir) | gitignored |
| Embedding **model weights** (e5-large, ~2.2 GB) | `~/.cache/huggingface/hub/` — outside the repo | n/a |

## Roadmap

- [x] **Phase 0** — Setup: repo, tooling, config, logging.
- [x] **Phase 1** — API discovery: catalog + data endpoints documented.
- [x] **Phase 2** — Ingestion: harvest + normalize metadata into SQLite.
- [x] **Phase 3** — Retrieval: embeddings, hybrid search (dense + BM25, RRF), golden-set eval.
- [x] **Phase 4** — Planning: NL → structured query (`make_plan` → typed `QueryPlan`),
      Greeklish→Greek normalization, local Qwen via Ollama, grounded-or-silent refusal.
- [ ] **Phase 5** — Access: resilient data client + cache.
- [ ] **Phase 6** — Synthesis: grounded answer + chart + freshness footer.
- [ ] **Phase 7** — Interface: FastAPI + HTMX chat.
- [ ] **Phase 8** — Eval & hardening.

## Conventions

Project context, principles, and the working agreement live in
[`CLAUDE.md`](CLAUDE.md). Code is fully type-hinted (`mypy` strict); commits follow
[Conventional Commits](https://www.conventionalcommits.org/).
