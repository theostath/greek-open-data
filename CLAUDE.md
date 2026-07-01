# CLAUDE.md — Pythia (working title)

> Natural-language query assistant over the Greek national open-data portal (data.gov.gr).
> Ask a question in Greek or English → get the right dataset, a grounded answer, and a chart.

This file is persistent context for Claude Code. Keep it accurate and concise; prune anything that goes stale. When a decision here conflicts with code, fix one of them and say which.

---

## 1. Mission & scope

**Mission:** Make Greece's ~9,500 open datasets usable by anyone who can type a question.
The portal's real weakness is *discoverability* — not data volume. This product attacks
discoverability with retrieval + grounded synthesis.

**In scope (MVP):**
- Map a natural-language question to the correct dataset(s).
- Fetch the relevant values via the official API.
- Return a grounded, cited answer plus a chart, with a freshness/provenance footer.
- Greek and English questions; Greek-language metadata throughout.

**Out of scope (for now):** user accounts, multi-tenant hosting, write-back to any
government system, cross-dataset statistical modeling/forecasting, mobile apps.

---

## 2. Core principles

1. **Grounded or silent.** Never fabricate figures. If retrieval finds no suitable
   dataset, say so plainly. "No dataset covers this" is a correct, valuable answer.
2. **Provenance is mandatory.** Every answer names the source dataset, its publisher, and
   its `last_updated`. Staleness is surfaced, never hidden.
3. **Local-first, reproducible.** Runs fully on a laptop. No managed services required for
   the MVP. SQLite + a local vector store + local embeddings.
4. **Deterministic where possible.** LLMs do language understanding and synthesis only.
   Dataset selection, parameter extraction, and data fetching must be inspectable and
   testable, not vibes.
5. **Eval-driven.** A golden-question set exists from Phase 3 onward. No retrieval or
   planning change merges without running it.
6. **Fail loud in dev, fail gracefully in prod.** The upstream API has no SLA — treat
   every external call as flaky.

---

## 3. Tech stack (decided — do not re-litigate without reason)

- **Language:** Python 3.11+
- **Package/env:** `uv`
- **Backend/API:** FastAPI + Uvicorn
- **Frontend (MVP):** server-rendered Jinja2 + HTMX (single coherent app, no SPA build step)
- **Metadata store:** SQLite (one file, committed schema, migrations as plain SQL)
- **Vector store:** Chroma (persistent, local). Swap to pgvector only if scale demands it.
- **Embeddings:** `intfloat/multilingual-e5-large` via `sentence-transformers` (strong Greek,
  runs locally, zero API cost). Hosted fallback: Voyage AI multilingual.
- **LLM (planning + synthesis):** Claude Sonnet (confirm the current API model string at
  build time — do NOT hardcode a guessed version). Keep the model id in config, not inline.
- **HTTP:** `httpx` (async), with `tenacity` for retries.
- **Charts:** emit Vega-Lite JSON specs; render client-side.
- **Testing:** `pytest`. **Lint/format:** `ruff`. **Types:** `mypy` on `src/`.

If you believe a swap is warranted, write a 3-line ADR in `docs/adr/` and proceed.

---

## 4. Repository layout

```
.
├── CLAUDE.md
├── pyproject.toml
├── Makefile
├── .env.example                 # DATA_GOV_GR_TOKEN=, ANTHROPIC_API_KEY=, etc.
├── config.py                    # typed settings (pydantic-settings), reads env
├── data/
│   ├── catalog.sqlite           # harvested metadata (gitignored)
│   └── chroma/                  # vector index (gitignored)
├── src/pythia/
│   ├── net.py                   # route TLS via the OS trust store (proxy CA)
│   ├── ingest/                  # Phase 1–2: API discovery + catalog harvest
│   │   ├── client_probe.py      # one-off endpoint discovery, writes findings to docs/
│   │   ├── harvest.py           # pulls all dataset metadata -> SQLite
│   │   ├── normalize.py         # schema normalization, Greek text cleanup
│   │   ├── models.py            # typed row models (ingest contract)
│   │   ├── db.py                # SQLite persistence (idempotent upserts)
│   │   └── schema.sql           # committed catalog schema
│   ├── retrieval/               # Phase 3: embed + hybrid search
│   │   ├── embed.py             # e5 embeddings + incremental Chroma index
│   │   ├── lexical.py           # FTS5 BM25 + RRF fusion
│   │   ├── index.py             # `make index`: build dense + lexical indexes
│   │   └── search.py            # find_dataset(question) -> ranked candidates
│   ├── planning/                # Phase 4: NL -> structured query
│   │   └── planner.py
│   ├── access/                  # Phase 5: resilient data fetch
│   │   ├── data_client.py       # token, retries, encoding, schema sniff
│   │   └── cache.py             # SQLite-backed response cache
│   ├── synthesis/               # Phase 6: answer + chart spec
│   │   └── answer.py
│   ├── api/                     # Phase 7: FastAPI routes
│   │   └── app.py
│   └── eval/                    # Phase 3+: golden set + scoring
│       ├── golden_questions.yaml
│       └── run_eval.py
├── templates/                   # Jinja2 + HTMX
├── static/
└── docs/
    ├── api_findings.md          # OUTPUT of Phase 1 — the source of truth for endpoints
    └── adr/                     # short architecture decision records
```

---

## 5. Data sources & gotchas (read before touching `access/` or `ingest/`)

The live portal blocks crawling and was **relaunched in May 2026**, so endpoint shapes must
be **verified in Phase 1**, not assumed. Record findings in `docs/api_findings.md`.

**Known anchors (verify, don't trust blindly):**
- Legacy data API pattern: `https://data.gov.gr/api/v1/query/{dataset}` with a **Bearer
  token** (register on the portal) and `date_from` / `date_to` params.
- Real dataset ids seen in the wild: `mcp_traffic_accidents`, `mcp_forest_fires`,
  `mdg_emvolio`, `internet_traffic`, `sailing_traffic`, `public-administration-evaluation`,
  `ekt-expenses`.
- The new catalog is likely CKAN-style (terms used: datasets, resources, organisations).
  If so, metadata lives behind `/api/3/action/package_search` & `package_show`. **Confirm.**
- Reference implementation worth reading (not depending on): the `pydatagovgr` client.

**Gotchas that WILL bite:**
- **Encoding:** assume UTF-8 but expect Latin-1/Windows-1253 stragglers and broken Greek
  diacritics. Normalize on ingest; never store mojibake.
- **Greek + Greeklish + English** all appear in questions and metadata. Normalize accents
  and handle transliteration in query planning.
- **No stable cross-dataset keys.** Geographic codes shifted across the
  Kapodistrias→Kallikratis reforms. Do NOT silently join datasets on region names.
- **Granularity is uneven** (national vs prefecture vs municipal). Never imply finer
  precision than the dataset provides.
- **Update cadence varies wildly**, and some datasets are effectively abandoned. Every
  dataset row must carry `last_updated`; surface it in answers.
- **No SLA:** timeouts, 5xx, rate limits, partial payloads. Retry with backoff, cache
  aggressively, and degrade gracefully.
- **PDF/denormalized resources** exist alongside clean tabular ones. MVP handles tabular
  (CSV/JSON) only; flag the rest as "not yet supported."

---

## 6. Coding conventions

- Type-hint everything in `src/`. `mypy` must pass.
- Pure functions for transforms; side effects (HTTP, DB) isolated in `access/` and
  `ingest/`. LLM calls live only in `planning/` and `synthesis/`.
- No secrets in code or logs. Read from env via `config.py`. Token must never be logged.
- Every external call goes through the retry/cache layer — no raw `httpx` in business logic.
- Prompts live in versioned files under each module (e.g. `planning/prompts/`), not inlined
  as long string literals.
- Log structured events (question → chosen dataset → params → cache hit/miss → latency).
- Docstrings short and in English. Code comments only where intent isn't obvious.

---

## 7. Commands

(Create these in Phase 0; keep this list in sync with the Makefile.)

```
make setup        # uv sync + install hooks
make probe        # run ingest/client_probe.py, write docs/api_findings.md
make harvest      # pull catalog metadata into data/catalog.sqlite
make index        # build/refresh the Chroma vector index
make eval         # run the golden-question eval, print retrieval metrics
make dev          # uvicorn with reload
make check        # ruff + mypy + pytest
```

---

## 8. Roadmap & current status

Status legend: [ ] not started · [~] in progress · [x] done

- [x] **Phase 0 — Setup:** repo, `uv`, config, `.env.example`, Makefile, logging, `make check` green.
- [x] **Phase 1 — API discovery:** see `docs/api_findings.md` (curated) + `docs/api_probe_raw.md`
      (probe evidence). Catalog is **CKAN 2.11.3** (Action API `/api/3/action/*`, **21,930**
      datasets, anonymous reads, DCAT-AP metadata; `package_search` caps at `rows=15000`). Legacy
      `/api/v1/query/{id}` data API is **GONE (404)**. Data is per-resource: **DataStore**
      (`datastore_search`, ~1% of resources, `limit≤32000`) or **file download** (302 → short-lived
      Azure Blob; the common case). `datastore_search_sql` disabled. **Note:** mission §1 says
      ~9,500 datasets; live count is ~21,930.
- [x] **Phase 2 — Ingestion:** `harvest.py` walks `package_search` → `normalize.py` → SQLite
      (`db.py`, schema in `ingest/schema.sql`). Harvested **21,806 datasets / 106,678 resources**
      (124 non-dataset/inactive skipped); `last_updated`=`metadata_modified` on every row. Note:
      `name` slug is **not unique** upstream (2 collisions) and deep pagination needs an explicit
      `sort`. Run with `make harvest`.
- [x] **Phase 3 — Retrieval:** hybrid `find_dataset()` = dense (Chroma, e5) + lexical (SQLite
      FTS5/BM25) fused with RRF (ADR-0001); `make index` builds both, `make eval` scores a
      26-question golden set. **Baseline (e5-small, n=26): MRR 0.48, R@5 0.58, R@10 0.65**
      (el 0.56 / en 0.52 / greeklish 0.30 MRR). Greeklish is the weak spot — next levers:
      e5-large swap, reranker (ADR-0002), Greeklish→Greek transliteration. TLS to Hugging Face
      goes through the OS trust store (`pythia/net.py`).
- [ ] **Phase 4 — Planning:** NL → structured query; Greek/Greeklish normalization.
- [ ] **Phase 5 — Access:** resilient data client + SQLite cache + schema sniffing.
- [ ] **Phase 6 — Synthesis:** grounded answer + Vega-Lite spec + freshness footer.
- [ ] **Phase 7 — Interface:** FastAPI + HTMX chat.
- [ ] **Phase 8 — Eval & hardening:** retrieval metrics, honesty checks, observability.

**Always work the lowest unchecked phase unless told otherwise.** Update this section when a
phase completes.

---

## 9. Definition of done (quality bar)

A change is done when: `make check` passes; new logic has tests; any retrieval/planning
change has been run through `make eval` with metrics reported in the PR/commit message; no
secrets leaked; and any new external-API assumption is reflected in `docs/api_findings.md`.

---

## 10. Open questions (resolve as you learn; don't block on them silently)

- Is the relaunched catalog CKAN, and does it expose full per-resource schema?
- Does the data API still use the legacy token + `query/{dataset}` pattern post-relaunch?
- Which datasets are tabular-and-fresh enough to be the demo set for Phase 6?
- ~~Embedding strategy: title+description only, or include column/field names?~~ **Resolved
  (Phase 3):** embed `title + notes + tags` plus their English translations (the `embed_text`
  column); per-resource field names deferred — revisit if eval shows a gap.
