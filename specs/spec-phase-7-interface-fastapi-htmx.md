# Plan: Phase 7 — Interface (FastAPI + Jinja2 + HTMX, single app)

> **Revision 2 (2026-08-06).** Revised after an independent four-judge panel review
> (logical gaps · edge cases · security & performance · architectural consistency). The
> findings and their resolutions are recorded in the [Panel review](#panel-review) section at
> the end; three were blockers against revision 1 and are fixed in the body below.

## Task Description

Build the web interface for Pythia: a single FastAPI application that serves both the HTTP
routes and server-rendered Jinja2 templates, progressively enhanced with HTMX. A user types a
question in Greek or English, the existing Phase 3–6 pipeline runs, and the page renders a
grounded answer, a Vega-Lite chart and a mandatory provenance footer — or a *recoverable*
refusal that shows what was searched and offers a next move.

This supersedes the 2026-06-02 direction change in `plan.md` ("Frontend: Vercel app (replaces
server-rendered Jinja2 + HTMX)"). That entry left an explicit blocker — *"Resolve the hosting
tension first"* — now resolved in favour of local-first, which is Core Principle #3 and the
reason Qwen, SQLite and Chroma all live on the laptop. `CLAUDE.md §3` already describes the
chosen architecture and needs no change; `plan.md` does, in three separate places.

- **Task type:** feature
- **Complexity:** complex

## Objective

`make dev` starts one process on `127.0.0.1:8000`. A journalist opens it, types *"πόσες
πυρκαγιές το 2023;"*, watches the pipeline's progress, and gets either a cited figure with a
chart and a footer naming dataset / publisher / freshness / row coverage, or a refusal that
names what was searched and lets them re-ask. The same `Answer` object drives the CLI and the
web app, and no HTML path can render a non-refusal without provenance.

## Problem Statement

Phases 0–6 produce a complete, typed pipeline reachable only through
`make answer QUESTION="..."`. That is unusable for the audience PRODUCT.md names — journalists
and researchers on deadline — and it hides the product's most common outcome. On the golden set
only 6/26 questions answer; 6 find the right dataset with no tabular resource and 12 find
nothing. A CLI makes each of those look like a failed command. The interface has to make a
correct refusal read as a legitimate result with a next step, or the product will be perceived
as broken exactly when it is being most honest.

Five constraints make this more than "add FastAPI":

1. **The pipeline is synchronous and slow.** A cold question loads an embedding model, calls
   Ollama (`llm_timeout_s = 120`), fetches an off-portal file (`access_deadline_s = 90`) and
   computes. A naive blocking POST ties up a worker for minutes and shows the user nothing.
2. **The honesty contract is enforced in Python dataclasses, not in templates.** `Answer`
   refuses to be constructed without provenance, but a Jinja2 template can happily omit the
   footer. The invariant has to be re-established at the render layer.
3. **`Answer.plan` must not reach the browser** (its own docstring says so), yet recoverable
   refusals need part of it. That requires a deliberate whitelist plus a caller-resolved
   recovery context — not a blanket omission.
4. **There are three refusal shapes, not two.** Beyond `NO_MATCH` and `UNSUPPORTED`,
   `answer_question` returns `REFUSED` on a **`MATCHED` plan** in three places
   (`answer.py:71` `TableTooLargeError`, `:77-84` requested period outside
   `binding.observed_range`, `:91-96` no rows match). CLAUDE.md §8 cites the live dataset that
   triggers the second — the ELSTAT index truncated at 2016-06 of a 2010-01→2026-01 span. Here
   retrieval and planning both *succeeded*; only the requested slice is absent. Rendering that
   dataset as a "near miss" would tell the user the opposite of the truth.
5. **A loopback bind is not an access control.** Any web page in any other browser tab can
   `POST` a cross-origin form to `http://127.0.0.1:8000/ask`. CORS does not prevent the request,
   only the reading of its response. A zero-auth endpoint that triggers LLM inference and
   outbound fetches needs an origin check regardless of what interface it binds to.

## Solution Approach

**One process, one shared pipeline object, HTML over the wire.** FastAPI owns both routes and
templates. HTMX handles the two interactions that need it — submitting a question and polling
for progress — with no build step, no second runtime, no CORS and no client state.

**Extract the orchestration the CLI already does into a reusable `Pipeline`.**
`answer.py::_run` currently takes `args: Any`, raises `SystemExit` and opens its own
connections. Lift that into `api/service.py` as a typed object holding process-lifetime
resources, then have the CLI call it too. ADR-0004 records the cost of not doing this: the
planner's LLM path never worked because only tests exercised it. A web path that duplicates the
CLI's orchestration will drift from it the same way.

**Resolve recovery data in the caller, never in the view.** `synthesis/` does no I/O by design,
which is precisely why `RefusalContext` is resolved by the caller and passed *in*. Phase 7 does
the same thing for the same reason: `Pipeline` holds the catalog connection, so `Pipeline`
builds a `RecoveryContext` (near-miss candidates with resolved URLs, and `offered_formats` from
`catalog.get_offered_formats`) alongside the `Answer`. **No Phase 6 dataclass is modified to
carry it.** This is the fix for revision 1's assumption that `to_view(answer)` could recover
`offered_formats` — it cannot; `RefusalContext` is consumed inside `_refuse_plan` and discarded.

**Answer asynchronously via a job, render synchronously via fragments.** `POST /ask` submits to
a bounded pool and returns a fragment that polls. Each poll renders the current stage. The
terminal fragment simply omits the polling attribute, which is how HTMX polling actually stops.

**Publish through a whitelist view model.** `api/view.py` maps `(Answer, RecoveryContext)` →
`AnswerView`, naming every field a template may read. Anything added to `QueryPlan` later is
invisible by default.

## Relevant Files

- `src/pythia/synthesis/answer.py` — `answer_question()` is the entry contract; `_run()` and
  `main()` hold the orchestration to extract. Lines 60–118 hold the three `MATCHED`-plan
  refusal paths; note that `_refuse()` (line 296) drops the `foot` already built at line 86.
- `src/pythia/synthesis/models.py` — `Answer`, `AnswerStatus`, `Footer`, `ChartSpec`,
  `FactTable`, `RefusalContext`, `output_language()`. The `__post_init__` invariants are the
  contract the templates must mirror. Note `Answer` permits a footer on a refusal; it only
  forbids facts and charts.
- `src/pythia/synthesis/footer.py` — **`format_number(value, language)` (line 116) is the
  canonical figure formatter**; its docstring exists because the guard's normalisation and the
  template text must not disagree. Line 49 is the canonical dataset-URL construction.
- `src/pythia/synthesis/chart.py` — `validate_spec` already blocks `url`/`signal`/`expr`/
  `transform`/`params`/`loader`. Phase 7 is the first time this spec reaches a JS interpreter.
- `src/pythia/retrieval/search.py` — `Candidate` (line 20): `id`, `name`, `title | None`,
  `last_updated`, `rank`, `score`. **No URL field**, and `title` is nullable.
- `src/pythia/planning/models.py` / `planner.py` — `QueryPlan`, `PlanStatus`; `make_plan()`
  performs retrieval *and* the LLM call inside one callback-free function.
- `src/pythia/access/data_client.py`, `access/catalog.py` — `fetch_for_plan`, `fetch_resource`,
  `get_resource`, `get_provenance`, `get_offered_formats`.
- `src/pythia/access/guard.py` — the existing SSRF policy; its "revisit if ever publicly
  hosted" note (line 67) is triggered by making the pipeline browser-reachable at all.
- `src/pythia/ingest/db.py` — `connect()` (line 73) **creates the file and parent dirs**, so a
  missing catalog cannot be detected by existence alone.
- `src/pythia/retrieval/embed.py` — `load_model()`; the ~2.2 GB load that happens once.
- `src/pythia/llm.py` — `load_llm()` and the `LLMClient` Protocol (what lets tests use a fake).
- `src/pythia/logging_setup.py` — `configure_logging`, `log_event`, `redact_secrets`.
- `config.py`, `Makefile`, `pyproject.toml`, `plan.md`, `CLAUDE.md`, `PRODUCT.md`.

### New Files

- `src/pythia/api/__init__.py`
- `src/pythia/api/app.py` — FastAPI app, lifespan, routes, security middleware.
- `src/pythia/api/service.py` — `Pipeline` and `RecoveryContext`.
- `src/pythia/api/jobs.py` — `JobStore`: bounded, TTL-evicting, thread-safe.
- `src/pythia/api/view.py` — `AnswerView` / `RefusalView` and the whitelist mapping.
- `templates/base.html`, `templates/index.html`, `templates/answer_page.html`,
  `templates/expired.html`
- `templates/partials/_progress.html`, `_answer.html`, `_refusal.html`, `_footer.html`,
  `_chart.html`, `_error.html`
- `static/app.css`, `static/app.js`, `static/vendor/` (pinned htmx + vega bundles)
- `tests/test_api_routes.py`, `test_api_jobs.py`, `test_api_view.py`, `test_api_templates.py`,
  `test_api_security.py`
- `docs/adr/0008-server-rendered-interface.md`

## Implementation Phases

### Phase 1: Foundation

Branch hygiene, dependencies, config, the `Pipeline` extraction and the job store — all
headless and testable before a template exists. The CLI is refactored onto `Pipeline` here so
the shared path is proven by `make answer` before the web app depends on it.

### Phase 2: Core Implementation

Routes, security middleware, view model, templates, the three refusal shapes, the chart.

### Phase 3: Integration & Polish

WCAG 2.2 AA pass, reduced motion, the empty state that teaches, error and degraded states,
vendored-asset integrity, and the ADR.

## Step by Step Tasks

IMPORTANT: Execute every step in order, top to bottom.

### 1. Land Phase 6 and branch correctly

- `feat/phase-6-synthesis` is currently **13 commits ahead of `develop` and unmerged** (PR #16
  open), and has already accumulated Phase-7-adjacent commits. Per `CLAUDE.md §11`, feature
  branches stem from `develop`; stacking Phase 7 on an unmerged sibling is how a PR becomes
  unreviewable.
- Merge PR #16 into `develop` (`--no-ff`). Note PR #15 (`fix/csv-banner-headers`) is **fully
  contained** in it — verified with `git merge-base --is-ancestor` — so it merges in one go and
  #15 should be closed as included, not merged separately.
- `git checkout develop && git pull && git checkout -b feat/phase-7-interface`
- Open a GitHub Issue using the template in the global CLAUDE.md, recording the branch name and
  this spec as the origin, **before** implementing. The PR targets `develop` with
  `Closes #<issue>`.

### 2. Add dependencies and configuration

- `uv add fastapi "uvicorn[standard]" jinja2 python-multipart`
  (`python-multipart` is required for HTML form posts; FastAPI raises at import of the first
  `Form(...)` parameter without it. `uvicorn[standard]` resolves `uvloop` only on non-Windows,
  which is expected and needs no marker.)
- Add to `config.py`, commented in the style of the existing blocks:
  - `api_host: str = "127.0.0.1"` — loopback default. Not a security boundary on its own (see
    step 6), but it keeps the port off the LAN.
  - `api_port: int = 8000`
  - `api_max_question_chars: int = 500` — counted in Python `str` code points, checked as
    `> max` after `.strip()`, bounding a field that reaches the LLM prompt.
  - `api_max_concurrent_jobs: int = 2` — CPU inference plus a 9B LLM on one laptop.
  - `api_max_pending_jobs: int = 4` — **separate backpressure from storage.** A
    `ThreadPoolExecutor` queue is unbounded by default, and TTL eviction only removes *finished*
    jobs, so without this a burst parks work behind a 2-worker pool for tens of minutes.
  - `api_job_ttl_s: int = 900` — how long a finished answer stays retrievable by URL.
  - `api_max_jobs: int = 64` — hard ceiling on the in-memory store.

### 3. Extract `Pipeline` and `RecoveryContext`

- Create `src/pythia/api/service.py`. `Pipeline` holds `Settings`, the loaded
  `SentenceTransformer`, an `LLMClient | None`, an `httpx.Client` and the two database paths.
- `Pipeline.create(settings)` mirrors `answer.main()`: `configure_logging()`,
  `use_system_trust_store()` (entrypoint only — it mutates `ssl` process-wide), model load,
  `load_llm`, `httpx.Client(follow_redirects=False, ...)`.
- **SQLite connections are per-call, not held on the instance.** `sqlite3` defaults to
  `check_same_thread=True` and handlers run in a worker thread, so a startup-created connection
  would raise on use. Open catalog and cache connections inside `answer()`, close them in a
  `finally`, and enable WAL on the cache database.
- `Pipeline.answer(question, resource_id=None, on_stage=None) -> AnswerBundle`, where
  `AnswerBundle` pairs the `Answer` with a `RecoveryContext`. Logic is exactly today's `_run`,
  with three changes: no `args` object, `ValueError` instead of `SystemExit`, and the stage
  callback.
- **`RecoveryContext` is built here, where the catalog connection lives:**
  - `near_misses`: up to five `plan.candidates`, each reduced to a display title
    (`candidate.title or candidate.name` — `title` is nullable) and a dataset URL.
  - Build the URL from `candidate.id`, not `candidate.name`. CLAUDE.md §8 records **two
    upstream slug collisions**, and a colliding slug would link a refusal to the wrong dataset —
    a provenance defect in a product whose premise is provenance. Verify once against the live
    portal that `/dataset/{uuid}` resolves; if it does not, fall back to the slug and add a test
    pinning the two known collisions.
  - `offered_formats`: `catalog.get_offered_formats(conn, plan.dataset.id)` when
    `plan.status is UNSUPPORTED`.
  - `matched_but_refused`: `plan.status is MATCHED and answer.status is REFUSED` — the third
    refusal shape, with `plan.dataset.title` and `last_updated` for display.
  - `planning_degraded`: `plan.degraded`, captured separately because
    `Answer.degraded = narration_degraded or plan.degraded` (`answer.py:115`) conflates a
    score-floor dataset match with templated prose, and the two need different wording.
- **Stages.** `make_plan()` performs retrieval *and* the LLM call inside one callback-free
  function, so a four-stage narration is not observable without changing Phase 4. Fire only
  what is true: `"queued"` → `"planning"` (covers retrieval and the LLM) → `"fetching"` →
  `"synthesising"`. Document in `_progress.html` that "planning" spans both. Adding an
  `on_stage` parameter to `make_plan()` is a legitimate follow-up but is **out of scope here**;
  do not claim granularity the code cannot produce.
- Guard inference with `threading.Semaphore(api_max_concurrent_jobs)` inside `answer()`.
- `Pipeline.close()` for the httpx client, called from lifespan shutdown.
- Refactor `answer.py::_run` to construct a `Pipeline` and delegate. `make answer` must produce
  byte-identical output afterwards — capture it before and diff.

### 4. Small Phase 6 amendment: keep provenance on a refusal that has it

- In `answer.py`, the `facts is None` branch (line 91) refuses *after* `foot` has already been
  built at line 86, then throws it away. `Answer`'s invariant permits a footer on a refusal — it
  forbids only facts and charts.
- Pass the footer into `_refuse` on that path (a `footer: Footer | None = None` parameter). A
  refusal reading "the right dataset, but no rows match your question" is substantially more
  useful with the dataset, publisher and freshness attached, and Principle #2 argues for
  surfacing provenance wherever it exists.
- This is a one-line behaviour change to Phase 6: give it **its own commit**, its own test, and
  a one-line amendment note in ADR-0007. Do not fold it into the interface commit.

### 5. Build the job store

- `src/pythia/api/jobs.py`: `JobStatus` (`queued | running | done | failed`) and a `Job`
  carrying id, question, status, stage, `AnswerBundle | None`, error string, timestamps, and the
  **process epoch** (see step 7).
- `JobStore` = dict + `threading.Lock`, with `submit()` (rejecting when pending jobs reach
  `api_max_pending_jobs` or the store reaches `api_max_jobs`), `get()`, and opportunistic TTL
  eviction of finished jobs.
- Dedupe: if an identical `(question, resource_id)` is already `queued` or `running`, return the
  existing job id rather than burning a second of only two slots. This also absorbs the
  double-click case.
- Execution on `ThreadPoolExecutor(max_workers=api_max_concurrent_jobs)`. Catch **every**
  exception in the worker and record it on the job; an escaped exception leaves a job queued
  forever and the UI polling until TTL.
- Store the error class and message only — never a traceback — and pass it through
  `redact_secrets` before it can reach a template.

### 6. Create the app, routes and security middleware

- `src/pythia/api/app.py` with an `asynccontextmanager` lifespan building `Pipeline` and
  `JobStore` onto `app.state`, and `get_pipeline()` / `get_jobs()` dependencies so tests can
  override them with fakes. Construct with `debug=False` explicitly.
- **Route handlers are `def`, not `async def`.** Starlette runs sync handlers in a threadpool;
  an `async def` handler doing blocking SQLite and inference work stalls the event loop for
  every other request.
- **Origin check on every mutating route.** Reject with 403 when an `Origin` header is present
  and is not `http://{api_host}:{api_port}`. Browsers reliably send `Origin` on cross-site form
  POSTs, and CORS does not stop the request from executing — only from being read. Without this,
  any page in any other tab can drive LLM inference and outbound fetches on this machine and
  fill the job queue.
- **Security headers on every response**, via middleware: `Content-Security-Policy:
  default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri
  'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. Every asset is
  vendored locally, so a strict CSP costs nothing and `frame-ancestors 'none'` closes
  clickjacking.
- Routes:
  - `GET /` — landing page.
  - `POST /ask` — form field `question`; `.strip()`, then reject empty or over
    `api_max_question_chars` with a rendered fragment (the client is a browser, not a JSON
    consumer); submit; return the polling fragment.
  - `GET /ask/{job_id}` — `_progress.html` while queued or running; the terminal fragment when
    done. 404 → an expired-result *fragment*.
  - `GET /a/{job_id}` — full-page render of a finished job. **Its own full-page 404 template**,
    not the fragment: this route is reached by direct navigation, so returning a bare partial
    yields a page with no `<html>`/`<head>`.
  - `GET /healthz` — JSON, **field by field, never `settings.model_dump()`** (which would
    publish `data_gov_gr_token` and `anthropic_api_key`). Report a dataset **row count**, not
    file existence: `ingest.db.connect()` creates a missing database, so an existence check
    reports green on a fresh checkout while every question silently returns `NO_MATCH`. Also
    report dense/lexical index counts and whether the LLM is reachable.
- Mount `static/` with `StaticFiles`; configure `Jinja2Templates` and assert autoescape is on.
- Register an exception handler so a render-time error (e.g. a future edit violating the footer
  invariant) produces `_error.html` rather than Starlette's default page.
- Log one structured event per request via `log_event`: question **length** (never the question
  text at INFO — it is user content), job id, stage timings, final status, cache hit/miss.

### 7. Handle the restart and expiry cases honestly

- `JobStore` is in-memory and `make dev` runs `--reload`, so any file save wipes every job —
  routine during development and demos.
- Stamp each job with a process-start epoch (a `uuid4` generated at startup) and embed it in the
  job id. A reference carrying a different epoch means the server restarted, which is a
  different message from "your result expired after 15 minutes". Say which one happened.
- Both messages must offer the original question back as a re-ask, so a lost result costs one
  click rather than retyping.

### 8. Build the templates

- `base.html` — one `<h1>` per page, skip link, `lang` set per rendered content, `dir="auto"` on
  user-content containers, vendored scripts, and **no external requests of any kind**:
  local-first means the page must work with the network cable out.
- `index.html` — question form and the empty state. The empty state must teach: show example
  questions drawn from the golden set, and **only ones that currently answer**. Suggesting a
  question the pipeline will refuse is a worse first run than an empty box. Query `/healthz` on
  load and warn if Ollama is unreachable *before* the user types.
- `partials/_progress.html` — current stage, `aria-live="polite"`,
  `hx-trigger="every 1s"`. Must render a distinct **"queued, waiting for a worker"** state:
  with two workers and jobs up to ~2 minutes, a third submission can sit with no stage at all,
  which is exactly the indefinite spinner the acceptance criteria forbid. Use skeleton content,
  not a centred spinner (product register). Because a single stage can legitimately last ~90 s,
  show elapsed time so a working fetch is distinguishable from a hang.
- `partials/_answer.html` — narration, facts, chart, caveats, and
  `{% include "_footer.html" %}` unconditionally, with an `{% if not footer %}` branch that
  raises rather than renders, mirroring the dataclass invariant instead of trusting it.
- `partials/_refusal.html` — **three branches**, not two:
  1. `NO_MATCH` — the normalized question (which also surfaces the Greeklish transliteration),
     near-miss datasets as links, and a pre-filled re-ask field.
  2. `UNSUPPORTED` — the dataset was found; name the formats it *does* offer and state that the
     MVP handles CSV/JSON only.
  3. `matched_but_refused` — **"this is the right dataset; the data doesn't cover what you
     asked."** Show the matched dataset and, when present, its footer (step 4). Never render
     these candidates under near-miss framing: retrieval and planning succeeded here, and
     saying otherwise tells the user the opposite of the truth.
- `partials/_footer.html` — dataset, publisher, `last_updated` with a staleness label, row
  coverage, resource format, fetched-at, dataset link. When `footer.complete` is false, say so
  in words, not with an icon alone.
- `partials/_chart.html` — a `<figure>` with the spec emitted via `|tojson` (Jinja2's
  `htmlsafe_json_dumps` escapes `<`, `>`, `&` and returns `Markup`, so a publisher-controlled
  label cannot break out of the `<script>` and it is not double-escaped). Include a text
  alternative for screen readers and render `ChartSpec.caveat` visibly when present.
- `partials/_error.html` — distinguish "the upstream publisher failed" (expected, per §5's
  no-SLA reality) from "Pythia failed"; they imply different next actions.

### 9. Vendor the client assets

- Pinned htmx and vega / vega-lite / vega-embed UMD builds into `static/vendor/`, committed. A
  CDN would break the local-first guarantee.
- Initialise Vega-Embed with `actions: {export: true, source: false, compiled: false,
  editor: false}` and disable hover transitions under `prefers-reduced-motion`.
- Record each file's SHA-256 in the ADR **and assert it in a test**, so silent drift fails
  `make check` rather than relying on review.

### 10. Wire up `make dev` and update every stale document

- `make dev` → `uv run uvicorn pythia.api.app:app --reload --host 127.0.0.1 --port 8000`.
- `plan.md` states the Vercel decision in **three** places, all of which must move together:
  the "Direction changes" bullet (lines 37–45), the status table row (line 18), and the
  "### Phase 7" narrative (lines 164–166). Use the file's existing dated strike-through
  convention (see the `~~Honesty bug...~~ **Fixed 2026-07-29:**` precedent) rather than deleting
  history.
- `CLAUDE.md`: §8 Phase 7 → `[x]` with the outcome summary; §4 gains the `api/` and
  `templates/partials/` subtrees; §7 if any command changes.

### 11. Write ADR-0008

- `docs/adr/0008-server-rendered-interface.md`, repo ADR structure, **`Status: Accepted ·
  2026-08-06`** — this ratifies a reversal of an abandoned `plan.md` TODO rather than proposing
  something new, matching how 0005/0006/0007 were accepted on their implementing commit.
- Context: the June direction change to Vercel + a JSON API and the hosting tension it left
  open — Qwen on `localhost:11434`, a 2.2 GB embedding model and two local SQLite databases
  cannot be reached from a cloud-hosted page without exposing the laptop or hosting the stack.
- Decision, rationale (local-first; no Node toolchain in a Python repo; the honesty contract is
  easier to hold when the view whitelist lives in the same process as the dataclass invariants),
  and consequences (no public deployment without a further decision; ~1 MB of vendored JS).
- Record explicitly that browser-reachability triggers the "revisit if ever publicly hosted"
  caveat in `access/guard.py:67`, and that the Origin check and CSP are the response.
- Note that this supersedes a `plan.md` TODO, **not** a numbered ADR — none of 0001–0007 ever
  covered the frontend.

### 12. Validate

- Run every command in Validation Commands and fix what it surfaces.

## Testing Strategy

**Route tests run with fakes, never the real pipeline.** Override `get_pipeline` with a stub
returning pre-built `Answer` objects (reuse `tests/synthesis_fixtures.py`, already built from
live-probed resources). No model load, no Ollama, no network — the suite must stay runnable
offline under `HF_HUB_OFFLINE=1`.

Cover:

- **The honesty invariant at the render layer.** `ANSWERED` and `PARTIAL` both render the
  footer's dataset, publisher and `last_updated`. A template omitting the footer must fail the
  suite, not merely review.
- **All three refusal shapes**, including a `matched_but_refused` fixture built from the ELSTAT
  date-range case, asserting the matched dataset is *not* labelled a near miss.
- **No internals leak.** Render a refusal whose plan carries scored candidates; assert scores,
  ranks and raw `QueryPlan` field names are absent from the HTML.
- **Escaping.** A dataset titled `<script>alert(1)</script>` and a Vega label containing
  `</script>` both render inert. Not hypothetical: 75% of fetchable resources are off-portal and
  titles come from CKAN.
- **Chart passthrough.** `AnswerView.chart` is the exact `ChartSpec.vega_lite` dict that
  `validate_spec` already approved — assert no re-serialisation path in `view.py` could
  reconstruct a spec and bypass that validation.
- **Security.** A cross-origin `Origin` header on `POST /ask` gets 403; CSP and
  `X-Content-Type-Options` present on every response; `/healthz` contains no `token` or `key`
  field.
- **Job store.** TTL eviction; the pending and total ceilings; dedupe of an identical in-flight
  question; a worker exception marks the job failed rather than leaving it queued; a stale-epoch
  id reports "restarted", not "expired".
- **Validation.** Empty, whitespace-only, punctuation-only and over-limit questions each render
  a message rather than a 422 JSON body.
- **`GET /a/{unknown}`** returns a full page, not a bare fragment.
- **Greek round-trip.** A Greek question and a Greek dataset title survive the full render as
  correct UTF-8. Encoding is this project's oldest recurring bug (§5).
- **Vendored asset hashes** match the ADR.

Edge cases worth explicit fixtures: a `PARTIAL` answer with both a chart and a truncation
caveat; `ChartSpec is None` (legitimate — the template must not render an empty figure);
`narration_rejected=True` (the guard fired, deterministic text shows); narration-degraded vs
planning-degraded rendering differently.

## Acceptance Criteria

- `make dev` serves the app on `127.0.0.1:8000` with no build step and no network access needed
  to render a page.
- A submitted question shows progress within one second — including a distinct queued state —
  and yields an answer, a refusal or a typed error. Never an indefinite spinner, never a
  traceback.
- Every `ANSWERED` and `PARTIAL` render carries dataset, publisher, `last_updated`, row coverage
  and a source link, enforced by a test.
- All three refusal shapes render correctly, and a `MATCHED`-plan refusal never presents its
  dataset as a near miss.
- `Answer.plan` is not serialized to the client; the whitelist has a test.
- A cross-origin `POST /ask` is rejected; CSP and nosniff headers present on every response.
- The CLI and the web app share one orchestration path — `make answer` output is unchanged.
- WCAG 2.2 AA: contrast verified for body and large text, every control labelled, full keyboard
  operation of ask → read → open source → re-ask, visible focus, reduced-motion alternatives.
- `make check` green with new tests for routes, jobs, view whitelist, templates and security.
- ADR-0008 written; `plan.md` updated in all three places; `CLAUDE.md` §4 and §8 updated.

## Validation Commands

- `uv run ruff check .` — lint.
- `uv run mypy` — strict over `src/` and `config.py`; the new `api/` package must pass.
- `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run pytest -q` — full suite, offline and
  deterministic (a bare `pytest` errors at random on Hugging Face HEAD flakes; see the Makefile).
- `uv run pytest tests/test_api_routes.py tests/test_api_jobs.py tests/test_api_view.py tests/test_api_templates.py tests/test_api_security.py -q`
  — the new surface alone while iterating.
- `uv run python -m pythia.synthesis.answer --question "πόσες πυρκαγιές το 2023;"` — proves the
  CLI still works after the `Pipeline` refactor; diff against output captured before step 3.
- `make dev`, then `curl -sS http://127.0.0.1:8000/healthz` — must report a non-zero dataset
  count, not merely "reachable".
- `curl -sS -X POST -H "Origin: http://evil.example" -d "question=x" http://127.0.0.1:8000/ask -o /dev/null -w '%{http_code}'`
  — must print 403.
- Manual: submit a question in a browser with the network disconnected after startup; page, htmx
  and Vega must all load from `static/vendor/`.

## Notes

**New dependencies:** `uv add fastapi "uvicorn[standard]" jinja2 python-multipart`, all runtime.

**No retrieval or planning code changes are in scope**, so `make eval` is not required for this
phase (see the `eval-gate` skill for when it is). Adding `on_stage` to `make_plan()` later would
not change ranking either, but the gate applies to anything that does.

**Deferred deliberately:** a JSON API for third-party clients, streaming narration, result
persistence beyond the in-memory TTL, multi-user concerns, and any deployment story. Each is a
Phase 8 decision or later; none blocks a usable single-user tool.

**The amber/staleness collision is resolved** (DESIGN.md, 2026-08-06). The accent stays amber
`oklch(0.700 0.130 60)` and staleness is encoded **without hue**: the sentence `footer.py`
already writes, plus a four-step indicator in neutral ink. Colorblind-safe, and it keeps the
accent meaning "actionable". Two consequences for this phase:

- The footer template renders freshness as text, never as a coloured chip alone.
- `synthesis/chart.py` sets `encoding.color` with **no `scheme`**, so specs currently fall back
  to Vega-Lite's default tableau10 — which would collide with the accent. A deliberate
  colorblind-safe categorical scheme must be set; `scale` is already on the `validate_spec`
  allowlist, so this needs no change to the guard. Treat it as part of step 8's chart work and
  keep it inside `chart.py`, so the spec stays validated at the single place that validates it.

**DESIGN.md is a seed**, written before any template exists. Re-run `/impeccable document`
against real templates once step 8 lands, to capture actual tokens and components.

**PRODUCT.md is the design brief:** product register, journalists as the primary user, refusals
as routes rather than dead ends, and two anti-references — a ChatGPT clone and a BI dashboard —
that rule out message bubbles and KPI tiles respectively.

## Panel review

Four independent judges reviewed revision 1 (logical gaps · edge cases · security & performance
· architectural consistency). Every claim below was verified against the code before being
accepted; the panel was not taken at face value.

**Blockers fixed.** (1) `to_view(answer)` could not produce `offered_formats` —
`RefusalContext` is consumed inside `_refuse_plan` and never stored on `Answer`; resolved by
building a `RecoveryContext` in `Pipeline`, where the catalog connection already is, rather than
amending a Phase 6 dataclass. (2) `Candidate` has no URL field and URL construction needs
`Settings`; `to_view` now takes settings, and URLs are built from `id` because of the two known
slug collisions. (3) A third refusal shape exists — a `MATCHED` plan that still refuses — and
revision 1 would have rendered the correctly-matched dataset as a near miss.

**Majors fixed.** `HX-Trigger` is a response header for firing client events and does *not* stop
polling (revision 1 was simply wrong); polling stops by omitting the attribute. `format_number`
in `footer.py` is the canonical formatter and must be reused, not reinvented. Stage narration
was over-promised: `make_plan` does retrieval and the LLM in one callback-free call, so the
stage list is now what the code can actually observe. `Answer.degraded` conflates planning and
narration degradation, so the two are surfaced separately. `/healthz` counted on file existence,
but `db.connect()` creates the file — it now reports a row count. Added: an Origin check (a
loopback bind does not stop a cross-origin form POST), CSP and nosniff headers, pending-job
backpressure independent of the storage ceiling, a queued progress state, in-flight dedupe, a
full-page 404 for `/a/{id}`, and restart-vs-expiry disambiguation.

**Process finding.** The spec would have been implemented on top of an unmerged, overloaded
`feat/phase-6-synthesis`; step 1 now lands Phase 6 first and cuts a clean branch off `develop`,
per `CLAUDE.md §11`.

**Beyond the panel.** Reading `answer.py:86` while verifying the third refusal shape surfaced
something no judge raised: the "no rows match" path builds a footer and then discards it, even
though `Answer` permits provenance on a refusal. Step 4 recovers it.

**Nothing was consciously overridden.** One suggestion was implemented differently than proposed
— a judge proposed adding `refusal_ctx` to `Answer`; resolving it in the caller keeps
`synthesis/` I/O-free, which is the architectural reason `RefusalContext` exists at all.
