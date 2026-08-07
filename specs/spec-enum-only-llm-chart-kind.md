# Plan: Enum-only LLM chart-kind selection (issue #21, Part B)

## Task Description

Let the language model influence **which kind of chart** an answer gets — `line`, `bar` or
`grouped_bar` — and nothing else. The model never emits a chart option, never sees a cell
value, and never affects a figure. `synthesis/chart.py` continues to build and validate every
option, so `validate_spec` remains the single place a chart is approved.

The model's choice is **advisory**. `chart.py` holds a deterministic veto and overrides the
model whenever the data shape cannot honestly support the requested kind. Overrides are
logged and counted.

- **Task type:** feature
- **Complexity:** complex — it supersedes part of ADR-0007, the project's central honesty
  contract.

## Objective

An answer to *"πώς εξελίχθηκαν οι αφίξεις;"* ("how did arrivals develop?") renders a line
chart, and *"ποια περιφέρεια έχει τις περισσότερες;"* ("which region has the most?") renders a
sorted bar chart — **from the same data shape**, because the difference is in the question, not
the table. No additional LLM call is made, the answer is no slower, and no chart can imply
continuity the data does not have.

## Problem Statement

### What is actually wrong today

`build_spec` picks the chart kind from this:

```python
temporal = facts.operation is Operation.NONE and (
    facts.series_field is not None or _looks_temporal(facts)
)
```

Two defects, and the first is the more serious:

1. **The authoritative signal exists and is not plumbed through.** `bind.py` computes
   `Binding.temporal` — the name of the column proven to be temporal — and `answer.py:107`
   passes `binding.dimension` into `build_spec` but **not** `binding.temporal`. So the chart
   builder falls back to `_looks_temporal()`, a string-prefix heuristic that guesses from the
   first four characters of a `dim` value. A dataset whose categories are years-as-labels
   ("2019", "2020") is treated as temporal; a genuine `referencedate` column formatted
   `01/2020` is not.
2. **`facts.series_field is not None` is treated as evidence of time.** A multi-series
   categorical breakdown (asylum applications by nationality *and* year of decision) is not
   temporal merely because it has more than one series.

**This is a bug that exists independently of any LLM work, and fixing it is Phase 1 below.**
It must land whether or not the model is ever consulted, because it is also what makes the
veto in Phase 2 trustworthy.

### What the model can genuinely contribute

Once the deterministic fix lands, the remaining question is not *"is this data temporal?"* —
that is answered. It is **"what is the reader asking for?"**, and that is linguistic:

| Question | Data shape | Best chart |
|---|---|---|
| "πώς εξελίχθηκαν οι αφίξεις;" (how did X develop) | temporal | **line** — the shape is the answer |
| "ποιος μήνας είχε τις περισσότερες;" (which month had most) | temporal | **bar**, ranked — a superlative, not a trend |
| "σύγκριση ανά περιφέρεια και έτος" (compare by region and year) | temporal + series | **grouped_bar** — comparison across categories |

A deterministic rule cannot distinguish rows 1 and 2: the data is identical and only the verb
differs. That is the entire and only justification for involving the model here. **If this
justification does not convince a reviewer, the correct outcome is to ship Phase 1 alone** —
it fixes the real defect and Phase 2 is then optional polish.

### Why the obvious implementation is wrong

The obvious approach — a second LLM call asking "which chart?" — costs ~10 s (ADR-0004
measures ~10 s per Qwen call on this CPU). An answer already takes 25–40 s end to end and
makes up to two calls. A third would add 25–40 % latency **for a presentation choice**, and
would add a second prompt, a second injection surface and a second failure mode.

**This spec adds no LLM call.** The chart kind rides on the narration call that already
happens.

## Solution Approach

**One extra JSON field on the existing narration response.** `narrate.md` already asks for
`{"answer": "…"}`; it will ask for `{"answer": "…", "chart": "line"}`, where `chart` is
optional and drawn from a closed three-value enum.

**Reorder `answer.py` so narration precedes chart construction.** Today the chart is built at
line 107 and narration runs at line 112. Both depend only on `facts`, `foot`, `binding` and
`limitation`, so the order is free. Swapping it lets the narration response inform the chart.

**A deterministic veto in `chart.py`, and one asymmetry that carries the honesty property:**

> The model may always make a chart **less** suggestive. It may never make one **more**
> suggestive.

Concretely: `LINE` implies continuity between points — a trend. Rendering categorical data as
a line asserts a relationship between adjacent categories that does not exist, which is
exactly *"never imply more precision than the data has"* (CLAUDE.md §5, PRODUCT.md). So:

- `LINE` is honoured **only** when `Binding.temporal` is set. Otherwise it is overridden.
- `BAR` and `GROUPED_BAR` are always honoured — a bar chart of temporal data is merely less
  elegant, never dishonest.
- `GROUPED_BAR` collapses to `BAR` when there is no series field, because there is nothing to
  group.

The veto is a pure function of the binding, so it is testable without a model.

**Degrade to today's behaviour on any doubt.** Absent field, unknown value, wrong type,
narration rejected by the guard, LLM unavailable → the deterministic kind is used and
`chart_source` records why.

## Exactly what changes in ADR-0007, and what does not

This is the section a reviewer should read first.

### Unchanged — the whole of the grounding contract

- **The model never sees the table.** It receives opaque `{FACT_n}` / `{LABEL_n}` tokens
  exactly as today. No cell value, no header, no row is added to the prompt.
- **The model never emits a quantity.** `verify.check_claims` runs unchanged, on the same
  placeholder text, before any substitution.
- **The model never influences a number.** Which rows aggregate, which column is a measure,
  what the figures are, and whether an answer is refused are all untouched.
- **The model never emits a chart option.** It emits at most one token from a closed set of
  three. `chart.py` builds every option; `validate_spec` approves every option.
- **The model cannot cause a chart to exist.** `build_spec` still returns `None` for a
  listing, a single figure, or an oversized series, regardless of what the model asked for.

### Changed — one presentation choice becomes model-influenced

- A **chart kind** may now be chosen by the model, from three values, subject to a
  deterministic veto that only ever makes the chart less suggestive.

### The blast radius, stated plainly

A model that is confused, adversarial, or successfully prompt-injected can achieve exactly
one thing: **a bar chart where a line chart would have been prettier, or vice versa within
what the data supports.** It cannot produce a wrong figure, a wrong label, a missing caveat, a
missing footer, or a chart implying a trend that is not there — the last because the veto is
deterministic and the model has no way to assert temporality.

Set against ADR-0007's own framing: the guard exists because *labels are cell values* and
*cell values are publisher-controlled*. A three-value enum carries no publisher content at all.

## Relevant Files

- `src/pythia/synthesis/chart.py` — `build_spec` gains `temporal_column` and `preferred`
  parameters and the veto. `_looks_temporal` is retained only as a fallback for callers that
  cannot supply a binding (the CLI probe path), and its use is narrowed.
- `src/pythia/synthesis/narrate.py` — `write()` returns the parsed chart hint alongside the
  answer; `build_placeholders` is untouched.
- `src/pythia/synthesis/prompts/narrate.md` — one new optional output field and one rule.
  **This is the only prompt change in the phase.**
- `src/pythia/synthesis/answer.py` — reorder narration before chart construction; thread
  `binding.temporal` and the hint into `build_spec`; carry `chart_source` for logging.
- `src/pythia/synthesis/models.py` — `ChartSpec` gains `source: ChartSource`, a new enum
  recording whether the kind was deterministic, model-chosen, or model-overridden.
- `src/pythia/synthesis/bind.py` — read only. `Binding.temporal` is the authoritative signal.
- `src/pythia/api/metrics.py`, `metrics_schema.sql` — record `chart_kind` and `chart_source`
  so the override rate is measurable rather than assumed.
- `src/pythia/api/view.py` — `AnswerView` may expose `chart_source` for the seams list; it
  must **not** expose anything else new.
- `docs/adr/0007-synthesis-grounding-contract.md` — amendment.
- `docs/adr/0009-echarts-renderer.md` — its "Not decided here" section is resolved.

### New Files

- `tests/test_synthesis_chart_kind.py` — the veto matrix, hint parsing, and degradation.

## Implementation Phases

### Phase 1: Deterministic foundation (independently valuable)

Plumb `Binding.temporal` into `build_spec` and make the kind decision correct without any
model involvement. Ship-able and reviewable on its own. **If Phase 2 is rejected, this stays.**

### Phase 2: The advisory hint

Prompt field, parsing, the veto, `ChartSource`, and the reorder in `answer.py`.

### Phase 3: Contract and observability

ADR-0007 amendment, ADR-0009 resolution, metrics columns, `/stats` override rate, docs.

## Step by Step Tasks

IMPORTANT: Execute every step in order, top to bottom.

### 1. Branch and issue hygiene

- Branch `feat/llm-chart-kind` from `develop` (after PR #25 merges — this builds on ECharts).
- #21 already exists and covers both parts; reference it rather than opening a new issue.

### 2. Plumb the authoritative temporal signal (Phase 1)

- Add `temporal_column: str | None = None` to `build_spec`.
- Replace the `temporal` computation with: temporal **iff** `temporal_column` is set, falling
  back to `_looks_temporal(facts)` **only** when `temporal_column is None` (the CLI probe path
  has no binding).
- Delete `facts.series_field is not None` from the temporal test. A multi-series categorical
  breakdown is not a time series.
- In `answer.py`, pass `temporal_column=binding.temporal`.
- **Write the tests first.** A categorical table with year-like labels must now be a bar; a
  `referencedate` column formatted `01/2020` must now be a line. Both are currently wrong.

### 3. Add `ChartSource` and thread it (Phase 2)

- In `models.py`:
  ```python
  class ChartSource(StrEnum):
      DETERMINISTIC = "deterministic"   # no hint offered
      MODEL = "model"                   # hint accepted as given
      OVERRIDDEN = "overridden"         # hint offered and vetoed
  ```
- `ChartSpec` gains `source: ChartSource = ChartSource.DETERMINISTIC`.

### 4. Extend the narration prompt (Phase 2)

- Add to `narrate.md`'s output object: `"chart"`, optional, one of `line` | `bar` |
  `grouped_bar`.
- Add one rule, worded to match the existing absolute-rules register:
  > **Choose the chart from the question's intent, never from the figures.** Use `line` only
  > when the question asks how something changed over time. Use `bar` when it asks which is
  > largest, smallest, or how categories compare. If unsure, omit the field entirely.
- Do **not** describe the data shape to the model — it does not need to know, and the veto
  handles the case where it guesses wrong.

### 5. Parse the hint defensively (Phase 2)

- `narrate.write` returns `tuple[str, dict[str, str], ChartKind | None]`.
- Parse: not a string → `None`; not in the enum → `None`; correct value → the `ChartKind`.
  **Never raise** — a malformed hint degrades, it does not fail an answer.
- Case-fold and strip before matching. Reject anything else, including near-misses like
  `"linechart"`.

### 6. Implement the veto (Phase 2)

- `build_spec` gains `preferred: ChartKind | None = None`.
- Resolution order:
  1. Compute `deterministic` exactly as Phase 1 leaves it.
  2. If `preferred is None` → use `deterministic`, `source=DETERMINISTIC`.
  3. If `preferred is LINE` and `temporal_column is None` → use `deterministic`,
     `source=OVERRIDDEN`.
  4. If `preferred is GROUPED_BAR` and `facts.series_field is None` → use `BAR`,
     `source=OVERRIDDEN`.
  5. Otherwise → use `preferred`, `source=MODEL`.
- Log an `INFO` `synthesis.chart_override` event on every override, carrying the requested and
  final kinds. **Never the question text.**

### 7. Reorder `answer.py` (Phase 2)

- Move the `_narrate(...)` call above the `chart = ...` block.
- Thread the returned hint into `build_spec`.
- **`_narrate` currently returns `(text, degraded, rejected)`** — extend to carry the hint.
  When the guard rejects the narration the hint is still usable: it is an enum, not prose, and
  the veto is what makes it safe. Record that decision in a comment; a reviewer will ask.
- Verify no behaviour depends on the old ordering: `foot`, `caveats`, `limitation` and
  `status` are all computed before either call today.

### 8. Record it in metrics (Phase 3)

- Add `chart_kind TEXT` and `chart_source TEXT` to `metrics_schema.sql` and `AnswerMetric`.
- Surface the override rate on `/stats` under "Honesty guards" — a high override rate means
  the prompt is misleading the model, which is worth seeing.

### 9. Amend the contracts (Phase 3)

- ADR-0007: an amendment stating precisely the "Changed / Unchanged" split above. It must say
  in one sentence what a compromised model can now do.
- ADR-0009: resolve its "Not decided here" section.
- `CLAUDE.md §8`: note the model's narrow new role, so it is not rediscovered as a surprise.

### 10. Validate

- Run every command in Validation Commands.

## Testing Strategy

Offline and deterministic throughout — `FakeLLM` supplies hints, so no test needs Ollama.

**The veto matrix is the core.** One test per cell, because this table *is* the honesty
property:

| `preferred` | `temporal_column` | `series_field` | Result | `source` |
|---|---|---|---|---|
| `None` | set | — | `LINE` | `DETERMINISTIC` |
| `None` | unset | — | `BAR` | `DETERMINISTIC` |
| `LINE` | set | — | `LINE` | `MODEL` |
| **`LINE`** | **unset** | — | **`BAR`** | **`OVERRIDDEN`** |
| `BAR` | set | — | `BAR` | `MODEL` |
| `GROUPED_BAR` | — | set | `GROUPED_BAR` | `MODEL` |
| `GROUPED_BAR` | — | unset | `BAR` | `OVERRIDDEN` |

The bold row is the one that matters: **a model can never turn categorical data into a line.**

Also cover:

- **Phase 1 regressions**, which are currently-wrong behaviour: year-like category labels are
  a bar; a real temporal column with a non-ISO format is a line.
- **Hint parsing:** `None`, `""`, `"LINE"` (case), `" line "` (whitespace), `"linechart"`,
  `"pie"`, `42`, `["line"]`, a dict. All degrade to `None`; none raise.
- **Injection:** a hostile cell value cannot reach the `chart` field, because the model never
  sees cell values — assert the prompt still contains no payload after this change, reusing
  `tests/test_synthesis_injection.py`'s fixtures.
- **No extra LLM call:** assert `len(FakeLLM.calls)` is unchanged from today for a full
  answer. This is the spec's central performance claim and must be pinned, not assumed.
- **Degradation:** LLM unavailable, `LLMError`, malformed JSON, narration rejected by the
  guard → a chart is still produced, `source=DETERMINISTIC`.
- **`validate_spec` still runs** on every produced option regardless of source.
- **Metrics:** an overridden chart records `chart_source="overridden"`.

## Acceptance Criteria

- Chart kind is decided from `Binding.temporal`, not a string heuristic, and the two
  currently-wrong cases in step 2 are fixed and tested.
- The model can supply at most a three-value enum; anything else degrades silently.
- **A `LINE` chart is impossible without a temporal column**, whatever the model returns —
  enforced by test, not by prompt.
- **No additional LLM call**: total calls per answer are unchanged, asserted by test.
- An answer still renders when the model omits the field, returns junk, fails, or has its
  narration rejected.
- `validate_spec` remains the only approval path; no option reaches a browser unvalidated.
- ADR-0007 carries an amendment stating exactly what changed and what a compromised model can
  now achieve; ADR-0009's open question is resolved.
- Override rate is visible on `/stats`.
- `make check` green.

## Validation Commands

- `uv run ruff check .` — lint.
- `uv run mypy` — strict over `src/` and `config.py`.
- `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run pytest -q` — full suite, offline.
- `uv run pytest tests/test_synthesis_chart_kind.py tests/test_synthesis_chart.py tests/test_synthesis_injection.py -q`
  — the affected surface while iterating.
- `uv run python -m pythia.synthesis.answer --question "πώς εξελίχθηκαν οι εμβολιασμοί;" --resource-id d0c5e369-461d-4110-8930-f4b656c119d5`
  — a real temporal question; expect a line chart and `chart_source` in the log event.
- `uv run python -m pythia.synthesis.answer --question "ποια περιφέρεια έχει τους περισσότερους;" --resource-id d0c5e369-461d-4110-8930-f4b656c119d5`
  — same resource, superlative question; expect a bar chart. **These two commands over one
  dataset are the whole feature**; if they produce the same chart, it did not work.
- `curl -s http://127.0.0.1:8000/stats | grep -i override` — the rate is rendered.

## Notes

**No new dependencies.**

**`make eval` is not required** — no retrieval or planning code changes.

**Phase 1 is separable and should be reviewed on its own merits.** It is a bug fix; if the
panel rejects Phase 2, Phase 1 still lands.

**The strongest argument against this spec**, which a reviewer should weigh: chart kind is
cosmetic, the veto means the model can only pick between charts we would already have been
willing to draw, and *therefore the feature buys little while spending some of ADR-0007's
credibility*. The counter-argument is that the win is real for one specific, common case — a
superlative question over temporal data, which is currently always drawn as a line and reads
as a trend claim the user did not ask for — and that the enum's blast radius is provably one
cosmetic step. **The panel should decide which of those weighs more; this spec does not assume
the answer.**

**A deliberately rejected alternative:** telling the model the data shape (`"the dimension is
temporal"`) so it can choose better. Rejected because it makes the model's contribution
collapse into the deterministic rule — if it is told the answer, it adds nothing — while
widening the prompt. The model is asked about the *question*, which is the only thing it knows
that `bind.py` does not.
