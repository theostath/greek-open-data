# CLAUDE.md — Pythia (working title)

> Natural-language query assistant over the Greek national open-data portal (data.gov.gr).
> Ask a question in Greek or English → get the right dataset, a grounded answer, and a chart.

This file is persistent context for Claude Code. Keep it accurate and concise; prune anything that goes stale. When a decision here conflicts with code, fix one of them and say which.

---

## 1. Mission & scope

**Mission:** Make Greece's ~21,900 open datasets usable by anyone who can type a question.
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
- **LLM (planning + synthesis):** local **Qwen via Ollama** (OpenAI-compatible API at
  `llm_base_url`; default `qwen3.5:9b`) — local-first, no API key, no egress (ADR-0004).
  Model id in config, not inline. Anthropic is retained **only** for RAGAS dev-eval
  (ADR-0003). Superseded the original Claude Sonnet choice per `plan.md` direction change.
- **HTTP:** `httpx` (async), with `tenacity` for retries.
- **Charts:** emit Vega-Lite JSON specs; render client-side.
- **Testing:** `pytest`. **Lint/format:** `ruff`. **Types:** `mypy --strict` over `src/` **and
  root `config.py`** — all three are configured in `pyproject.toml`, so run them bare
  (`uv run mypy`, no path argument; passing one skips `config.py`, where every setting lives).

If you believe a swap is warranted, write a 3-line ADR in `docs/adr/` and proceed.

---

## 4. Repository layout

```text
.
├── CLAUDE.md
├── README.md
├── plan.md                      # phase-by-phase build plan (the roadmap's long form)
├── pyproject.toml               # deps + ruff/mypy/pytest config (no setup.cfg, no tox)
├── Makefile
├── .env.example                 # DATA_GOV_GR_TOKEN=, ANTHROPIC_API_KEY=, etc.
├── config.py                    # typed settings (pydantic-settings), reads env — ROOT, not src/
├── .github/workflows/ci.yml     # ruff + mypy + pytest on py3.11 + py3.12 (§11)
├── .claude/skills/eval-gate/    # the §9/§11 retrieval eval ritual, incl. a preflight script
├── data/
│   ├── catalog.sqlite           # harvested metadata + datasets_fts (gitignored)
│   └── chroma/                  # vector index (gitignored)
├── specs/                       # per-phase implementation specs (Phases 4, 5, 6)
├── tests/                       # pytest suite; mirrors the module layout below
│   ├── synthesis_fixtures.py    # shared Phase 6 tables, built from live-probed resources
│   └── fixtures/                # captured API payloads
├── src/pythia/
│   ├── net.py                   # route TLS via the OS trust store (proxy CA)
│   ├── llm.py                   # LLM Protocol + Ollama /api/chat client + FakeLLM (ADR-0004)
│   ├── logging_setup.py         # structured logging + secret redaction
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
│   │   ├── rerank.py            # cross-encoder reorder, DEFAULT-OFF (ADR-0002)
│   │   └── search.py            # find_dataset(question) -> ranked candidates
│   ├── planning/                # Phase 4: NL -> structured query
│   │   ├── planner.py           # make_plan(): normalize -> retrieve -> select -> one LLM call
│   │   ├── normalize.py         # Greeklish->Greek + language detection (ADR-0005)
│   │   ├── select.py            # dataset + CSV/JSON resource choice (deterministic)
│   │   ├── models.py            # QueryPlan contract
│   │   └── prompts/             # extract_plan.md, disambiguate.md
│   ├── access/                  # Phase 5: resilient data fetch
│   │   ├── data_client.py       # orchestrator: guard -> cache -> transport -> sniff
│   │   ├── guard.py             # scheme/host/IP policy (pure)
│   │   ├── transport.py         # the only I/O; manual redirects, streaming caps
│   │   ├── detect.py            # magic bytes vs declared format (pure)
│   │   ├── sniff.py             # decode, dialect, banner rows, type inference (pure)
│   │   ├── catalog.py           # resource + provenance lookups
│   │   ├── models.py            # TableData honesty contract (ADR-0006)
│   │   ├── cache.py             # SQLite-backed response cache
│   │   └── cache_schema.sql     # committed cache schema
│   ├── synthesis/               # Phase 6: grounded answer + chart + footer (ADR-0007)
│   │   ├── answer.py            # orchestrator + refusal paths
│   │   ├── coerce.py            # Greek decimal comma, periods, sentinels (pure)
│   │   ├── bind.py              # column roles, series identity, params (pure)
│   │   ├── compute.py           # THE ONLY SOURCE OF NUMBERS (pure)
│   │   ├── chart.py             # deterministic Vega-Lite + validate_spec (pure)
│   │   ├── narrate.py           # placeholder prompt + deterministic template
│   │   ├── verify.py            # the claim guard (pure)
│   │   ├── footer.py            # provenance, coverage, staleness (pure)
│   │   ├── lexicon.py           # versioned Greek/English word lists
│   │   ├── models.py            # Answer/Fact/Binding contract
│   │   └── prompts/             # narrate.md — the ONLY synthesis prompt
│   ├── api/                     # Phase 7: the interface (ADR-0008)
│   │   ├── app.py               # FastAPI app, lifespan, routes, Origin check, CSP
│   │   ├── service.py           # Pipeline + RecoveryContext — the ONE orchestration path
│   │   ├── jobs.py              # bounded, TTL-evicting, thread-safe JobStore
│   │   └── view.py              # AnswerView: the publish whitelist (plan never ships)
│   └── eval/                    # Phase 3+: golden set + scoring
│       ├── golden_questions.yaml
│       └── run_eval.py
├── templates/                   # Jinja2 + HTMX (Phase 7)
│   └── partials/                # _ask, _progress, _result, _answer, _refusal, _footer,
│                                # _chart, _error, _expired
├── static/                      # app.css, app.js, vendor/ (htmx + vega, hash-pinned)
└── docs/
    ├── api_findings.md          # OUTPUT of Phase 1 — the source of truth for endpoints
    ├── api_probe_raw.md         # raw probe evidence behind api_findings.md
    ├── benchmarks/              # measured runs (e.g. embedding-index-build.md)
    └── adr/                     # 0001–0007; short architecture decision records
```

Everything marked **NOT YET CREATED** is Phase 7 scaffolding described here for intent only —
do not assume those files exist.

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

Keep this list in sync with the Makefile.

```text
make setup        # uv sync (pre-commit hooks: not wired yet, Phase 8)
make probe        # run ingest/client_probe.py, write docs/api_probe_raw.md
make harvest      # pull catalog metadata into data/catalog.sqlite
make index        # build/refresh the Chroma + FTS5 indexes (incremental)
make eval         # run the golden-question RETRIEVAL eval, print metrics
make fetch RESOURCE_ID=<id>   # Phase 5: fetch one resource -> typed table
make cache-purge  # drop access-cache rows past the TTL ceiling
make answer QUESTION="..."    # Phase 6: grounded answer + chart + footer
                              # (add RESOURCE_ID=<id> to bypass retrieval)
make dev          # uvicorn with reload — stub until Phase 7
make check        # ruff + mypy + pytest — the gate in §9
```

Every target is a one-line `uv run` wrapper; run those directly when `make` is unavailable
(see the Makefile). **Run pytest from the repo root** — `pyproject` sets `pythonpath = ["."]`,
which is what lets tests import both `pythia.*` and `tests.synthesis_fixtures`.

```bash
uv run pytest tests/test_synthesis_verify.py -q             # one file
uv run pytest tests/test_synthesis_verify.py::test_name -q  # one test
uv run pytest -q -k "synthesis and not honesty"             # by keyword
```

**Always run the suite offline** — `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run pytest -q`,
which is what `make check` and CI both do. `test_embed.py` and `test_search.py` load real
e5-small weights, and without those vars the loader fires a live HEAD request to
huggingface.co per module; those requests fail intermittently and error 7–10 tests at random.
The weights are cached, so offline fetches nothing and the run is both deterministic and
faster (~27 s vs ~40 s). A bare `uv run pytest` is the single most likely reason you see
red on unmodified code.

The suite is 335 tests. Prefer a targeted file while iterating, and run `make check` before
committing.

---

## 8. Roadmap & current status

Status legend: [ ] not started · [~] in progress · [x] done

- [x] **Phase 0 — Setup:** repo, `uv`, config, `.env.example`, Makefile, logging, `make check` green.
- [x] **Phase 1 — API discovery:** see `docs/api_findings.md` (curated) + `docs/api_probe_raw.md`
      (probe evidence). Catalog is **CKAN 2.11.3** (Action API `/api/3/action/*`, **21,930**
      datasets, anonymous reads, DCAT-AP metadata; `package_search` caps at `rows=15000`). Legacy
      `/api/v1/query/{id}` data API is **GONE (404)**. Data is per-resource: **DataStore**
      (`datastore_search`, ~1% of resources, `limit≤32000`) or **file download** (302 → short-lived
      Azure Blob; the common case). `datastore_search_sql` disabled.
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
- [x] **Phase 4 — Planning:** `make_plan()` (`planning/planner.py`) → typed `QueryPlan`
      (dataset + CSV/JSON resource + validated intent params) via normalize → retrieve →
      select → one LLM call. LLM = local **Qwen/Ollama** behind a `Protocol`+fake
      (`pythia/llm.py`, ADR-0004; Anthropic now RAGAS-only). Greeklish→Greek transliteration
      + `en`-safe language detection (`planning/normalize.py`, ADR-0005). Grounded-or-silent
      via an LLM relevance gate + a degraded score-floor fallback. **117 tests green.**
      **Eval gate RUN 2026-07-28** (n=26, e5-large): baseline MRR **0.515**; +normalization
      **0.544** (greeklish 0.319→0.429, `el`/`en` unchanged → ADR-0005 **accepted for the
      no-reranker config**); +reranker **0.652** but **~28 s/query** on CPU → ADR-0002
      **accepted, default-off**. ADR-0004 **accepted** after the smoke run exposed that the
      planner LLM path never worked (reasoning model returned empty `content`); fixed by
      switching to Ollama's native `/api/chat` with `think:false` (76 s → 10 s).
      Relevance is now gated **before** resource selection, so `unsupported` means
      "relevant but no CSV/JSON" and off-topic questions correctly return `no_match`.
      **End-to-end on the golden set (n=26): 6 MATCHED on the correct dataset, 0 matched
      on a wrong one, 6 UNSUPPORTED (right dataset, no CSV/JSON), 12 NO_MATCH.** Zero
      wrong matches — grounded-or-silent holds. The ceiling is retrieval R@1 (12/26 put
      the right dataset first) and resource format, **not** the planner: both are
      Phase 3 / Phase 5 concerns.
- [x] **Phase 5 — Access:** `fetch_resource()`/`fetch_for_plan()` (`access/data_client.py`)
      → typed `TableData` via DataStore (`sort=_id asc`, paged) or file download. Layered
      pure modules: `guard` (scheme/host/IP policy, manual ≤3 redirects — **75% of CSV/JSON
      resources are off-portal**, and `localhost:11434` runs Ollama), `detect` (magic bytes;
      stops an HTML 404 parsing as a table), `sniff` (Greek-restricted codecs, dialect,
      type inference — no silent coercion). SQLite cache keyed on
      `(resource_id, key_field, key_value)` with a TTL ceiling; incomplete bodies never
      cached. **Honesty contract:** `TableData.complete` has no default and is validated
      against `incomplete_reason`. Carries `publisher` for the Phase 6 footer. ADR-0006.
      **215 tests green**; verified live on portal CSV (cp1253 + `;` auto-detected),
      DataStore (24,390/24,390 rows) and an off-portal municipal endpoint.
      Run with `make fetch RESOURCE_ID=<id>`.
- [x] **Phase 6 — Synthesis:** `answer_question()` (`synthesis/answer.py`) → typed `Answer`
      (`answered | partial | refused`) with a Vega-Lite spec and a mandatory provenance footer.
      **The LLM never emits a quantity and never sees the table** — it gets opaque placeholders
      and the real strings are substituted back after `verify.check_claims`, which gates
      numerals, number-words, trend/superlative language, wrong-label figures and markup
      (ADR-0007). Only `MEASURE` columns aggregate; `LATEST` handles running totals.
      Designed against **four live-probed resources**: an embedded `ΣΥΝΟΛΟ` row makes a naive
      asylum total exactly 2× (147,374 vs 73,687); the ELSTAT index is ~715 interleaved series
      truncated at **2016-06** of a 2010-01→2026-01 span; `OBS_VALUE='86,6'` is typed `text`;
      `BASE_PER`/`Arithmese`/`areaid` are `number` but not measures. **335 tests green**,
      including a guard-recall eval (14 adversarial narrations, all rejected) that runs inside
      `make check`. Run with `make answer QUESTION="..."`.
- [x] **Phase 7 — Interface:** one FastAPI process serving Jinja2 + HTMX (**ADR-0008**, which
      reverses `plan.md`'s June Vercel direction change — local-first won). `make dev` →
      `127.0.0.1:8000`. **`Pipeline` (`api/service.py`) is the single orchestration path**,
      shared by the CLI and the web app; ADR-0004 is the argument for that. Questions run on a
      bounded pool (`api_max_concurrent_jobs=2`) and the browser polls; the terminal fragment
      stops polling by omitting `hx-trigger`. `api/view.py` is a **publish whitelist** — a
      field added to `QueryPlan` later is invisible by default, and `Answer.plan` never
      reaches the browser. **Three refusal shapes, not two:** a `MATCHED` plan that still
      refuses is never framed as a near miss, guarded in *both* `build_recovery_context` and
      `to_view`. Provenance renders inside the answer, before the chart, so the quotable unit
      never scrolls away from its citation. Job ids carry a process epoch, so a lost result
      says "restarted" rather than the untrue "expired". Security: an **Origin check** (a
      loopback bind is not an access control) plus CSP/nosniff/no-referrer; assets vendored
      and **hash-asserted in the suite**. **436 tests green.** Verified live: `/healthz` reads
      21,806/21,806/21,806 with the LLM reachable, cross-origin POST → 403, a real Greek
      question end-to-end in 40 s.
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

---

## 11. Git workflow — Gitflow (adopted 2026-07-29)

Remote: <https://github.com/theostath/greek-open-data> · default branch: **`develop`**

**The branching model itself lives in the global `~/.claude/CLAUDE.md` ("Git Branching
Model — Gitflow") and is not duplicated here.** It defines the five branch types
(`main`, `develop`, `feat|fix|docs|chore/*`, `release/*`, `hotfix/*`), which branch
merges where, tagging, and the `--no-ff` / branch-deletion rules. Read it first.

Repo-specific notes only:

### Per-change checklist here

1. `git checkout develop && git pull`
2. `git checkout -b feat/<slug>` (never off `main` — only `hotfix/*` does that)
3. Open a **GitHub Issue** using the structure in the global CLAUDE.md; record the
   branch name and origin in it before implementing.
4. Small, focused **Conventional Commits**. No AI attribution trailers.
5. `make check` green + tests for new logic. Any retrieval/planning change **must** also
   report `make eval` numbers in the PR/commit — see §9.
6. PR **targets `develop`** with `Closes #<issue>`.
7. After merge, delete the branch locally and on the remote.

### Eval-gated changes and releases

Because §9 ties "done" to eval metrics, a `release/*` branch is the right place to
re-run `make eval` on the release candidate and record the numbers in the tag message —
retrieval quality is the product here, so a release without current metrics is untagged
work. Note the eval is only meaningful on a **tombstone-free** Chroma collection (§8,
Phase 4 notes); a rebuilt-by-upsert index makes the numbers non-reproducible.

### CI

`.github/workflows/ci.yml` runs `ruff` + `mypy` + `pytest` on **pushes to `main` and
`develop`, and PRs targeting either** — the global guideline names `main` only, which under
Gitflow would miss every day-to-day PR. Two repo-specific details:

- **Matrix on Python 3.11 and 3.12.** `pyproject` declares `requires-python >=3.11` but
  local development only ever runs 3.12, so 3.11 would otherwise be an unverified claim.
- **Tests run with `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`**, after a cached
  pre-download of e5-small (only `test_embed.py` and `test_search.py` need real weights).
  Hugging Face HEAD requests are intermittently flaky, and offline runs are verified to
  produce identical results.
- **Actions are SHA-pinned and the token scope is `contents: read`.** A mutable major tag can
  be repointed at new code by its owner; the job reads the tree and needs nothing more. Note
  `uv run mypy` follows `[tool.mypy] files`, so the CI type gate covers `src/` and
  `config.py` but **not** `tests/`.
