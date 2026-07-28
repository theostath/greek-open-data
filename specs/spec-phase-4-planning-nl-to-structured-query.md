# Plan: Phase 4 — Planning (NL → structured query)

> Task type: **feature** · Complexity: **complex**
> Scope owner: `src/pythia/planning/` · Sits between Phase 3 (retrieval) and Phase 5 (access).
> Source of truth for phase scope: `CLAUDE.md §8` and `plan.md`.
>
> **Review status:** Reviewed by an independent 3-judge panel (logic/requirements; edge
> cases; security + architecture/ADR consistency). All three returned
> APPROVE-WITH-CHANGES; every blocker/major resolution is folded into this document. The
> consolidated review lives in the approved plan file. One conscious override is recorded
> (logging — see §"Logging & secrets").

## Task Description

Build Phase 4, the **planning** stage. It turns a natural-language question (Greek,
English, or Greeklish) into an inspectable, typed **`QueryPlan`**: which dataset answers
it, which resource to pull (with the URL Phase 5 needs), and the structured parameters
(date range, region, metric, aggregation) needed later to fetch and synthesize an answer.

The planner is the bridge between retrieval (`find_dataset`, Phase 3) and the resilient
data client (Phase 5). It must:

1. **Normalize the question** — Greeklish→Greek transliteration before retrieval, directly
   attacking the golden-set weak spot (greeklish MRR 0.30) — without regressing `el`/`en`.
2. **Select a dataset** deterministically from retrieval's ranked candidates.
3. **Select a resource** within that dataset using deterministic rules (CSV/JSON only for
   the MVP, DataStore-preferred).
4. **Extract structured intent parameters** via the local LLM, then **validate them
   deterministically**.
5. Be **grounded-or-silent**: when nothing genuinely covers the question, return a
   `no_match` plan rather than forcing a wrong dataset.

This spec also lands the two decisions Phase 4 depends on and that `plan.md` flags as
requiring ADRs: the **LLM provider swap to local Qwen via Ollama** (ADR-0004) and
**Greeklish→Greek transliteration** (ADR-0005).

## Objective

When this plan is complete:

- `make_plan(question, ...)` returns a typed, logged, testable `QueryPlan` for any Greek /
  English / Greeklish question, or a grounded `no_match` / `unsupported` plan.
- Dataset **selection** and parameter **validation** are deterministic and unit-tested; the
  LLM does language understanding and parameter *proposal* only (per Core Principle #4).
- Feeding the **normalized** question into `find_dataset` is measured through `make eval`
  and reported (per Core Principle #5), with the greeklish lift quantified and **no `el` or
  `en` regression**.
- The LLM path runs against **local Ollama/Qwen** (no Anthropic, no network egress), with
  the model id in config, backed by ADR-0004; transliteration is backed by ADR-0005.
- `make check` (ruff + mypy strict + pytest) is green.

## Problem Statement

Retrieval alone returns *candidate datasets*; it does not tell the system **which resource
to fetch** or **with what parameters**. A question like *"Πόσες απελάσεις έγιναν ανά
υπηκοότητα το 2024;"* implies a date filter (2024), a grouping (by nationality), and a
metric (count) — none of which retrieval extracts. Downstream Access (Phase 5) and
Synthesis (Phase 6) need this structure to fetch the right rows and answer honestly.

Three constraints make this non-trivial:

- **Determinism (Principle #4):** selection and parameter extraction "must be inspectable
  and testable, not vibes." The LLM may *understand language and propose* structured JSON,
  but every consequential decision (which dataset, which resource, whether a date is valid)
  is deterministic code that we log and test.
- **Grounded-or-silent (Principle #1):** "No dataset covers this" is a correct answer.
  The planner must be able to decline.
- **Greeklish is the weak retrieval spot** (MRR 0.30 vs el 0.55 / en 0.71). The planner's
  normalization step is the natural, highest-value place to fix it — and any change to what
  we feed `find_dataset` is a **retrieval change that must pass `make eval`** without
  regressing `el`/`en`.

There is also a hard limit to respect: **the catalog has no per-resource column schema**
(DataStore typing exists for only ~1% of resources; the rest are file downloads sniffed at
fetch time — see `docs/api_findings.md §3`). Therefore the planner extracts parameters as
**normalized intent** (date range, region text, metric keywords, aggregation/grouping
hints), **not** column-bound query fragments. Binding intent to real columns is deferred to
Phase 5/6 after schema sniffing.

## Solution Approach

A single public entry point, `make_plan`, orchestrating deterministic stages around one
LLM call (a second, opt-in call only when disambiguation is enabled):

```
question
  │
  ▼  planning/normalize.py            (pure, deterministic)
detect_language (margin-based) → transliterate Greeklish→Greek (greeklish path only)
  │  normalized_question  (blank/too-short → NO_MATCH, no LLM call)
  ▼  retrieval/search.find_dataset    (Phase 3; now returns SCORED candidates)
ranked candidates (Candidate[] with .score)
  │  (empty → NO_MATCH, confidence 0.0)
  ▼  [ if planning_llm_disambiguate ON: LLM disambiguation call picks dataset FIRST ]
  ▼  planning/select.py               (deterministic rules over `resources`, CSV/JSON only)
chosen dataset + resource (id, format, url, access_path)   (none → UNSUPPORTED)
  │
  ▼  planning/planner.py → llm.py     (ONE structured LLM chat call, temp 0, JSON, max_tokens capped)
{ relevant: bool, reason: str, params: {...} }
  │  parse/schema/relevant-missing/connection failure → degraded=True (score-floor fallback)
  │  relevant=false → NO_MATCH ; else validate params deterministically
  ▼
QueryPlan  (status ∈ matched | no_match | unsupported; degraded flag; confidence; candidates)
```

Design commitments, each tracing to a repo principle or precedent:

- **Grounding = LLM relevance gate (primary) + score floor (degraded fallback).** The
  relevance gate reads the candidate's metadata and returns `relevant: bool`; it is the
  primary grounded-or-silent mechanism. Because a rank-only signal is degenerate (top is
  always rank 0), a **small, eval-gated Phase 3 change** surfaces the fused score:
  `rrf_fuse`/`find_dataset` return the score and `Candidate` gains `score: float`.
  `confidence` = min-max normalization of the top score over the returned pool (documented
  formula; `0.0` when retrieval is empty). `planning_score_threshold` is used **only** in
  degraded mode (LLM unavailable), calibrated on the 26-question golden set and validated
  with the reranker both on and off.
- **LLM behind a `Protocol` + fake, in a neutral shared module.** Transport lives in
  `src/pythia/llm.py` (`LLMClient` Protocol, `OllamaClient`, `FakeLLM`, `load_llm`) so
  Phase 6 synthesis reuses it without importing upward from `planning/`. The *calls* stay in
  `planning/` and `synthesis/` per `CLAUDE.md §6`. This mirrors the established
  `retrieval/rerank.py` `Scorer` pattern; unit tests inject `FakeLLM` — no live LLM.
- **Dependency injection like `find_dataset`** — `make_plan` receives `conn`, `model`,
  `chroma_path`, `llm`, `reranker`, `reference_date`. No per-call model reload.
- **One LLM call, structured chat output** — the call returns relevance + reason + params
  together (JSON mode, `temperature=0`, `max_tokens` capped). Sent as **chat messages**
  (`system` = the versioned prompt, `user` = question + candidate context) — never
  `.format()`-interpolating untrusted question text into the instruction (prompt-injection
  mitigation; see §Security).
- **Deterministic dataset selection by default** — take retrieval's **top-1**, then apply
  the LLM relevance gate (a yes/no grounding check, *not* a ranked re-selection). An opt-in,
  **eval-gated** LLM disambiguation over the top-N shortlist (`planning_llm_disambiguate`,
  default **off**) runs **first** when enabled (so `select_resource` acts on the finalized
  dataset); it is a **distinct** call, and a `-1` ("none relevant") result → `NO_MATCH`.
  This follows the reranker (ADR-0002) opt-in precedent — measure before adopting.
- **Grounded-or-silent** — `NO_MATCH` when: retrieval is empty; the LLM relevance gate (or
  disambiguation) rejects; or (degraded only) the top score is below the floor.
  `UNSUPPORTED` when the dataset matches but has no **CSV/JSON** resource.
- **Fail loud in dev, gracefully in prod (Principle #6)** — if Ollama is unreachable **or
  returns malformed/schema-invalid JSON**, deterministic stages still produce a plan;
  relevance/params are skipped, `degraded=True` is set and logged, grounding falls back to
  the score floor, and `params` is empty.
- **No cross-dataset region joins** — region is captured as free-text intent only; the plan
  never resolves region names to codes (Kapodistrias→Kallikratis caveat, `CLAUDE.md §5`).

### Data contract

```python
# src/pythia/planning/models.py
from dataclasses import dataclass, field
from enum import Enum
from pythia.retrieval.search import Candidate  # now carries .score

AGGREGATIONS = frozenset({"count", "sum", "avg", "min", "max"})


class PlanStatus(str, Enum):
    """Terminal outcome of planning for one question."""
    MATCHED = "matched"          # dataset + CSV/JSON resource + validated params
    NO_MATCH = "no_match"        # grounded refusal: nothing covers the question
    UNSUPPORTED = "unsupported"  # dataset matched but only non-CSV/JSON resources exist


@dataclass(frozen=True)
class QueryParams:
    """Normalized *intent* parameters — not yet bound to real columns."""
    date_from: str | None = None      # ISO-8601 date; guaranteed date_from <= date_to
    date_to: str | None = None
    region: str | None = None         # spatial text as-said; never code-joined
    metrics: list[str] = field(default_factory=list)  # metric keywords (intent)
    aggregation: str | None = None    # one of AGGREGATIONS or None
    group_by: str | None = None       # e.g. "nationality" | "year" | "region"
    limit: int | None = None          # positive int, clamped to settings max, or None


@dataclass(frozen=True)
class QueryPlan:
    """The inspectable output of Phase 4."""
    question: str                     # original, verbatim
    normalized_question: str          # transliterated (greeklish) else original
    language: str                     # detection label: "el" | "en" | "greeklish" (NOT an ISO code)
    status: PlanStatus
    dataset: Candidate | None         # chosen dataset (carries provenance + score)
    resource_id: str | None           # catalog resource key (both access paths)
    resource_format: str | None       # "CSV" | "JSON"
    resource_url: str | None          # download endpoint; Phase 5 resolves the SAS fresh
    access_path: str | None           # "datastore" | "download" | None
    params: QueryParams
    confidence: float                 # 0..1, min-max of top score over pool; 0.0 if empty
    reason: str                       # human-readable why (logged; supports honesty)
    degraded: bool                    # True if the LLM step was skipped/failed/malformed
    candidates: list[Candidate]       # full ranked shortlist, for inspectability
```

`confidence` is derived from **retrieval** (min-max normalized top score over the returned
pool), never from the LLM — keeping the number inspectable. The relevance gate is a boolean
logged in `reason`, not a fabricated probability. `metrics`/`group_by`/`region` are
free-text **intent** and are deliberately *not* enum-validated (only `aggregation`, dates,
and `limit` are); this asymmetry is intentional because there is no column schema to
validate against.

### Public API

```python
# src/pythia/planning/planner.py
def make_plan(
    question: str,
    *,
    conn: sqlite3.Connection,
    model: SentenceTransformer,
    chroma_path: str,
    llm: LLMClient,
    reranker: Scorer | None = None,
    reference_date: date | None = None,   # None disables relative-date resolution
    settings: Settings | None = None,
) -> QueryPlan:
    """Turn a NL question into a typed, grounded QueryPlan."""
```

## Relevant Files

Use these files to complete the task:

- `src/pythia/retrieval/search.py` — `find_dataset` / `Candidate`; **small change**: add
  `score` to `Candidate` and thread the fused score through. Reference for the injection
  style to mirror.
- `src/pythia/retrieval/lexical.py` — `rrf_fuse`; **small change**: surface the fused score
  instead of discarding it (return `[(id, score)]` or parallel scores).
- `src/pythia/retrieval/rerank.py` — the `Scorer` **Protocol + fake** pattern to replicate
  for `LLMClient`; also the eval-gated opt-in precedent (ADR-0002).
- `src/pythia/ingest/models.py` — `ResourceRow`; the fields (`is_tabular`, `format`,
  `datastore_active`, `url`, `state`, `position`, `size`) that drive resource selection.
- `src/pythia/ingest/db.py` — `connect(...)`.
- `docs/api_findings.md §3–§4` — access paths (DataStore vs download), the MVP "CSV/JSON
  only" boundary, and the "no per-resource column schema" constraint.
- `config.py` — add `llm_*` and `planning_*` settings; reconcile Anthropic (RAGAS only).
- `src/pythia/logging_setup.py` — `log_event(...)`; the structured planning trace.
- `src/pythia/eval/run_eval.py` + `golden_questions.yaml` — the retrieval gate the
  normalization change must pass; the extension point for a small planning eval.
- `docs/adr/0003-eval-framework.md` — RAGAS uses a paid LLM; ADR-0004 must state its
  disposition.
- `plan.md` / `CLAUDE.md §3, §8` — direction changes (Ollama, Vercel) and phase status.

### New Files

- `src/pythia/llm.py` — `LLMClient` Protocol, `OllamaClient`, `FakeLLM`, `load_llm`,
  response schema + parsing/validation. **Neutral shared home** (not under `planning/`).
- `src/pythia/planning/__init__.py`
- `src/pythia/planning/models.py` — `QueryPlan`, `QueryParams`, `PlanStatus`, `AGGREGATIONS`.
- `src/pythia/planning/normalize.py` — `detect_language`, `transliterate_greeklish`,
  `normalize_question` (all pure).
- `src/pythia/planning/select.py` — `select_resource(conn, dataset_id) -> ResourceRow | None`.
- `src/pythia/planning/planner.py` — `make_plan` + deterministic validators.
- `src/pythia/planning/prompts/extract_plan.md` — versioned system prompt (per `CLAUDE.md §6`).
- `docs/adr/0004-llm-provider-ollama-qwen.md`, `docs/adr/0005-greeklish-transliteration.md`.
- `tests/planning/test_normalize.py`, `test_select.py`, `test_planner.py`, `test_llm.py`.
- *(optional, small)* `src/pythia/eval/planning_golden.yaml` + a param-extraction scorer.

## Implementation Phases

### Phase 1: Foundation
- ADR-0004 (Ollama/Qwen; states ADR-0003/RAGAS disposition + the localhost-hosting
  consequence) and ADR-0005 (transliteration; eval-gated), both **Proposed**.
- **Phase-3 score surfacing:** `rrf_fuse` returns fused scores; `Candidate.score` added;
  run `make eval` to confirm no ranking change (scores are additive metadata).
- `config.py`: add `llm_base_url` (`http://localhost:11434/v1`), `llm_model` (`qwen3.5:9b`),
  `llm_timeout_s: float = 30.0`, `llm_temperature: float = 0.0`, `llm_max_tokens`,
  `planning_score_threshold` (**defaulted**, calibrated), `planning_llm_disambiguate` (`False`),
  `planning_limit_max`. Anthropic stays only for RAGAS; update `.env.example`, `CLAUDE.md §3`.
- `planning/models.py` and the pure `planning/normalize.py` with unit tests.

### Phase 2: Core Implementation
- `src/pythia/llm.py`: `LLMClient` Protocol + `OllamaClient` (httpx chat + tenacity, JSON
  mode, `temperature=0`, `max_tokens`) + `FakeLLM`; response validation.
- `planning/select.py`: deterministic CSV/JSON resource selection.
- `planning/planner.py`: `make_plan` wiring normalize → retrieve → (disambiguate?) → select
  → LLM → validate, with structured logging and the degraded/grounded fallbacks.
- The versioned prompt in `planning/prompts/extract_plan.md`.

### Phase 3: Integration & Polish
- Wire the **normalized** question into eval; run `make eval` off-vs-on and report the
  greeklish lift plus `el`/`en` non-regression. Gate transliteration per the measured
  result and record it in ADR-0005.
- Optional small planning-eval fixture (manual/opt-in; **not** in CI).
- Update `CLAUDE.md §8` (tick Phase 4) and `plan.md`; flip ADR-0004/0005 to Accepted with
  evidence. `make check` green.

## Step by Step Tasks

IMPORTANT: Execute every step in order, top to bottom.

### 1. Record the two architectural decisions
- `docs/adr/0004-llm-provider-ollama-qwen.md` (Proposed): decision = local Qwen via Ollama
  OpenAI-compatible API, model id in config, no egress, no Anthropic **in the
  planning/synthesis path**; explicitly state ADR-0003's disposition (keep
  `anthropic_api_key` **solely** for RAGAS, or repoint RAGAS at the local model with a
  follow-up); consequences = local-only + a **latency budget** note (9B on CPU, no GPU) and
  the note that localhost Ollama forecloses a publicly-hosted backend unless Phase 7
  revisits it.
- `docs/adr/0005-greeklish-transliteration.md` (Proposed): decision = query-side
  Greeklish→Greek transliteration before retrieval, **greeklish path only**; gate adoption on
  `make eval` (greeklish lift, **no `el`/`en` regression**), matching the ADR-0002 eval gate.

### 2. Surface retrieval scores (small Phase-3 change)
- `rrf_fuse`: return fused scores alongside ids (e.g. `list[tuple[str, float]]`); update
  `find_dataset` and `_hydrate` to carry `score` into `Candidate`.
- `Candidate`: add `score: float`. Update existing retrieval tests.
- Run `make eval` to confirm order is unchanged (scores are additive metadata).

### 3. Extend configuration
- Add the `llm_*` and `planning_*` settings above (all defaulted; `planning_score_threshold`
  calibrated from the golden set in Step 9).
- `.env.example`: add `LLM_BASE_URL` / `LLM_MODEL`; keep `ANTHROPIC_API_KEY` with a comment
  that it is **RAGAS-only** (per ADR-0004). Update `CLAUDE.md §3` LLM line.

### 4. Define the data contract
- Create `planning/models.py` (`PlanStatus`, `QueryParams`, `QueryPlan`, `AGGREGATIONS`),
  full type hints + short English docstrings, `frozen=True` (repo idiom).

### 5. Implement question normalization (pure)
- `detect_language(q) -> "el" | "en" | "greeklish"`: Greek script present → `el`. For ASCII
  text, use a **scoring margin**, not any-cue presence: score Greeklish-only cluster density
  (`mp, nt, gk, ou, ei, ai, ps, ks`, `-is`/`-os` endings, etc.) against common-English
  signal; **default to `en` on ambiguity** (never transliterate English). Documented,
  testable heuristic.
- `transliterate_greeklish(q) -> str`: longest-match-first digraph table
  (`th→θ, ps→ψ, ch/x→χ, ks→ξ, ou→ου, ai→αι, ei→ει, ...`) then single chars; handle
  **final sigma** (word-final `s`→`ς`). Pinned order: **transliterate first**, accent
  folding (if any) after. Document that it is lossy/best-effort.
- `normalize_question(q) -> tuple[str, str]`: returns `(normalized_text, language)`.
  Transliterate **only** when language is `greeklish`; leave `el`/`en` untouched (avoids the
  query/corpus accent-fold mismatch — the index is unfolded). Blank / too-short /
  punctuation-only input returns a sentinel the planner maps to `NO_MATCH`.

### 6. Implement deterministic resource selection
- `select_resource(conn, dataset_id) -> ResourceRow | None`: query `resources` where
  `dataset_id = ?` (key strictly on dataset **`id`** — slug is non-unique) and `state`
  active; **eligibility = `format` in (CSV, JSON)** for the MVP (XLS/XLSX → treated as
  unsupported; widening is a Phase-5 follow-up); order by `datastore_active DESC`, then
  format priority (CSV > JSON), then `position ASC`, then `size` **NULLS LAST ASC**, then
  `id ASC` (deterministic terminal tiebreak). Return the top row or `None`.
- Map `datastore_active` → `access_path` (`"datastore"` else `"download"`); carry `url`.

### 7. Implement the LLM client (Protocol + Ollama + fake) in `src/pythia/llm.py`
- `class LLMClient(Protocol): def complete_json(self, messages: list[dict], *, max_tokens: int) -> dict`.
- `OllamaClient`: `httpx.Client` to `{llm_base_url}/chat/completions`, `temperature=0`, JSON
  response format, `max_tokens`, wrapped in `tenacity` that retries **only** connection
  errors / 5xx / model-loading (502) — **not** generation timeouts (fail straight to the
  caller so the planner degrades without a ~90 s stall).
- `load_llm(settings) -> LLMClient`; provide `FakeLLM(returns=...)` for tests.
- Validate the response deterministically: `relevant` must be bool (else treat as
  malformed); coerce `params` to `QueryParams` — dates via `date.fromisoformat` (drop
  invalid to `None`), enforce `date_from <= date_to` (else null the pair + log), `aggregation`
  in `AGGREGATIONS` or `None`, `limit` positive int **clamped to `planning_limit_max`** or
  `None`, `metrics` a list of non-empty strings; **unknown keys → drop-and-continue**.

### 8. Author the versioned prompt
- `planning/prompts/extract_plan.md`: the **system** message — return **only** JSON with
  keys `relevant` (bool), `reason` (short string), `params` (the QueryParams shape); forbid
  inventing figures; forbid resolving region names to codes; honor the injected
  `reference_date` for relative dates. The **user** message (built in the planner) carries
  the question + the candidate's `title`/`title_en`/`notes`/`tags`. Never `.format()` raw
  question text into the system prompt.

### 9. Implement the orchestrator `make_plan`
- Normalize; blank/too-short → `NO_MATCH` (no LLM call).
- `find_dataset(normalized, ...)` (pass `reranker`). Empty → `NO_MATCH`, `confidence=0.0`.
- Compute `confidence` = min-max of the top `score` over the returned pool.
- If `planning_llm_disambiguate` is on: **first** call the LLM to pick the best of the top-N
  (index or `-1`); `-1` → `NO_MATCH`; else finalize that dataset. (Default off → top-1.)
- `select_resource(chosen)`; `None` → `UNSUPPORTED` (dataset set, resource `None`).
- Build chat messages; call `llm.complete_json`. Success + `relevant=false` → `NO_MATCH`;
  success + valid → `MATCHED` with validated `params` (empty params is valid MATCHED). Any
  **connection failure / timeout / malformed JSON / schema failure / missing `relevant`** →
  `degraded=True`, skip relevance/params, grounding falls back to the score floor
  (`planning_score_threshold`), `params` empty; `log_event(..., WARNING, "planning.llm_degraded")`.
- Distinguish a **hydrate miss** (candidate id absent from the catalog) — log an integrity
  WARNING; do **not** silently report it as `UNSUPPORTED`.
- Emit one structured `log_event("planning.done", ...)`: question, normalized, language,
  dataset id, resource id, status, degraded, confidence, latency_ms.

### 10. Wire normalization into eval and measure
- In `run_eval.py`, normalize each question before `find_dataset`, behind a
  `--normalize/--no-normalize` toggle (mirrors the reranker off/on split).
- Run `make eval` both ways; capture MRR + R@k overall and per language. **Report greeklish
  lift + `el`/`en` non-regression** in the commit/PR (Definition of Done, `CLAUDE.md §9`).
  Calibrate `planning_score_threshold` from these results, validated reranker on **and** off.

### 11. Tests
- `test_normalize.py`: table-driven — greeklish digraph mapping incl. final sigma; language
  detection across el/en/greeklish golden samples; **explicit English "must-not-
  transliterate" cases** (q13–q19); blank/short/punctuation-only.
- `test_select.py`: in-memory SQLite — prefers DataStore, CSV>JSON priority, **XLSX-only →
  `None` (unsupported)**, NULLS-LAST size, `id` terminal tiebreak, keys on `id`.
- `test_llm.py`: `FakeLLM` fixed JSON; validation drops bad dates/aggregations/limits,
  enforces `date_from<=date_to`, clamps `limit`, drops unknown keys; malformed JSON path.
- `test_planner.py`: with `FakeLLM` + stubbed retrieval: matched happy path; `no_match` on
  empty retrieval, blank input, relevance=false, disambiguate `-1`; `unsupported` on
  CSV/JSON-absent; `degraded=True` on LLM raise **and** on malformed JSON; provenance
  (`dataset.last_updated`) carried; plans are `frozen`.

### 12. Optional lightweight planning eval
- Annotate ~8–10 golden questions with expected `params` in `planning_golden.yaml`; add a
  scorer for exact/partial param match. **Manual/opt-in — must not be wired into CI**
  (needs live Ollama; CI runs ruff + pytest only).

### 13. Finalize
- Update `CLAUDE.md §8` (tick Phase 4) and `plan.md`; flip ADR-0004/0005 to Accepted (or
  record the measured decision for 0005). Run the full validation suite; ensure green.

## Testing Strategy

- **Pure units (no I/O, no LLM):** normalization (incl. the English-not-transliterated guard
  and final sigma) and parameter validation are the highest-value tests — table-driven,
  covering the three languages and malformed LLM output.
- **Deterministic selection:** resource selection against in-memory SQLite fixtures
  exercises every ordering rule, the XLSX-only→unsupported boundary, and the empty case.
- **Orchestration with a fake LLM:** `make_plan` end-to-end with `FakeLLM` and stubbed
  retrieval so no models load — every terminal status + both degraded triggers.
- **Retrieval regression gate:** `make eval` run for the normalization change; greeklish lift
  and `el`/`en` non-regression reported. Merge gate per Principle #5.
- **Edge cases explicitly covered:** empty/blank/short input; empty retrieval; hydrate miss;
  relevance/disambiguation rejection; CSV/JSON-absent dataset; Ollama unreachable and
  malformed JSON (both degraded); relative dates via injected `reference_date`;
  `date_from>date_to`; oversized `limit`; a Greeklish question transliterating to the correct
  Greek dataset; an English question **not** transliterated; region captured as text and
  never code-joined.
- **Determinism:** `temperature=0`, injected `reference_date`, and a fully deterministic
  `select_resource` ordering make tests reproducible; unit tests never hit the network.

## Security

- **Prompt injection.** The question is untrusted and flows into the LLM. Blast radius is
  small by design — dataset selection is deterministic top-1 (disambiguation off by
  default), and params are validated intent never bound to columns — but the planner still
  sends the question as a **`user` chat message**, never `.format()`-interpolated into the
  `system` instruction, and relies on deterministic validation of the JSON response. A forced
  `relevant=true` is bounded by the degraded score floor; a forced `false` only self-denies.
- **Secrets.** No token is needed on the read path (`api_findings.md §5`); Ollama is local
  with no key. `ANTHROPIC_API_KEY` (RAGAS-only) is never read in the planning path.

## Logging & secrets

- INFO stays clean: model, latency, status, and prompt **length/hash** — never the token.
- The full prompt may be logged at **DEBUG** for local dev (Ollama is local; the prompt
  carries no secret). *This is a conscious relaxation of the first spec's "never log full
  prompts"; the absolute prohibition is reserved for secrets/token, matching `CLAUDE.md §6`.*

## Acceptance Criteria

- `make_plan` returns the correct `status` for: a normal Greek/English/Greeklish question
  (`matched`), an out-of-scope question (`no_match`), and a matched-but-CSV/JSON-absent
  dataset (`unsupported`).
- Dataset selection and all parameter validation are deterministic and unit-tested; the LLM
  is invoked **once per matched plan** (zero when `degraded` or on any pre-LLM terminal
  status; **plus one** disambiguation call only when the opt-in flag is on), behind a
  `Protocol` with a fake used in tests.
- The normalization change has been run through `make eval`; **greeklish MRR improves and
  `el`/`en` do not regress** (or transliteration is flag-gated per the recorded ADR-0005
  decision), with numbers in the PR/commit.
- No secrets are logged; the LLM path uses local Ollama (no Anthropic, no egress); the model
  id lives in `config.py`; `ANTHROPIC_API_KEY` is documented as RAGAS-only.
- ADR-0004 and ADR-0005 exist and are referenced; `CLAUDE.md §8` and `plan.md` reflect
  Phase 4 done.
- `make check` (ruff + mypy strict + pytest) passes with new tests for every new module.

## Validation Commands

`make` is not installed on this Windows box — use the `uv run` equivalents (both listed):

- `uv run ruff check .` — lint.
- `uv run ruff format --check .` — format check.
- `uv run mypy src/` — strict types on `planning/` + `llm.py`.
- `uv run pytest -q` — full suite incl. new `tests/planning/*` (offline: `FakeLLM`, stubbed
  retrieval, in-memory SQLite; no models load).
- `uv run python -m pythia.eval.run_eval` (`make eval`) — run normalization **off vs on**;
  record MRR + R@k per language; verify greeklish lift + `el`/`en` non-regression; calibrate
  the degraded-mode score threshold reranker on **and** off.
- Smoke (needs `ollama serve` + the pulled model): `make_plan` on one Greek and one Greeklish
  golden question; confirm `status`, `resource_id`/`resource_url`, validated `params`; log
  **p50/p95 LLM latency** for the Phase-7 budget.

## Notes

- **Dependencies:** the Ollama endpoint is OpenAI-compatible; call it with the existing
  `httpx` + `tenacity` — no new runtime dependency. If the `openai` SDK is preferred, add it
  with `uv add openai` and justify in ADR-0004. Do **not** add a heavyweight date parser; the
  LLM emits ISO dates against the injected `reference_date`, validated with `date.fromisoformat`.
- **Build-time check (per plan.md):** confirm `ollama serve` is running and the exact model
  tag before wiring; the tag lives in config, not inline.
- **Parameters are intent, not columns.** No per-resource column schema exists
  (`api_findings.md §3`); binding to real columns happens in Phase 5/6 after schema sniffing.
- **No cross-dataset joins on region names** (Kapodistrias→Kallikratis, `CLAUDE.md §5`).
- **GitHub issue:** global CLAUDE.md wants a tracking issue per feature branch, but `plan.md`
  notes **no git remote yet** — create the Phase 4 issue once a remote exists; until then
  this spec is the tracking artifact.
- **Judge-panel gate (global CLAUDE.md):** passed — independent 3-judge review folded in
  above; the one conscious override (DEBUG-level prompt logging) is documented in §Logging.
