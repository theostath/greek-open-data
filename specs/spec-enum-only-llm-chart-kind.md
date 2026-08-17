# Plan: Enum-only LLM chart-kind selection (issue #21, Part B)

> **Revision 2 (2026-08-07).** Revision 1 was reviewed by an independent four-judge panel
> (logical gaps · edge cases · security & performance · architectural consistency). **All four
> returned BLOCK**, and three converged on the same defect. The findings and their resolutions
> are in the [Panel review](#panel-review) at the end. Revision 1's central claim — *"the model
> may make a chart less suggestive, never more"* — **was false in both directions** against the
> real `chart.py`, and both failures were reproduced by running the code.
>
> **Phase 1 has shipped** (PR #27, merged). It stands on its own and is unaffected by this.

## Task Description

Let the language model influence **which kind of chart** an answer gets, and nothing else. The
model never emits a chart option, never sees a cell value, and never affects a figure.
`synthesis/chart.py` continues to build and validate every option, so `validate_spec` remains
the single place a chart is approved.

The model's choice is **advisory**, and may only ever select a chart the deterministic rule
would already have been willing to draw, or a strictly less suggestive one.

- **Task type:** feature
- **Complexity:** complex — it supersedes part of ADR-0007, the project's central honesty
  contract.

## Objective

A superlative question over a single-series time table — *"ποιος μήνας είχε τις
περισσότερες;"* — renders a **bar chart in chronological order** instead of a line, because a
line asserts a trend the reader did not ask about. The same table with *"πώς εξελίχθηκαν;"*
still renders a line. No additional LLM call, and no chart can assert more than its data
supports.

## Problem Statement

### The one case this addresses

After Phase 1, chart kind is decided correctly from data shape. What shape cannot decide is
what the reader asked:

| Question | Data | Honest chart |
|---|---|---|
| "πώς εξελίχθηκαν οι αφίξεις;" | single-series, `Operation.NONE`, temporal | **line** |
| "ποιος μήνας είχε τις περισσότερες;" | *identical* | **bar**, chronological |

Only the verb differs. **This is the entire scope.** Revision 1 claimed a broader remit; the
panel showed most of it was unreachable (see below).

### Why the scope is narrower than it looks — three reachability facts

The panel established these by running the code; they bound the feature and must be stated
before anyone designs against a wider surface.

1. **`grouped_bar` can never be requested.** `series_field` is set only by `compute._series`
   when `len(points) > 1`, and that branch leaves `facts.facts == []`. `narrate.write` returns
   at `narrate.py:128` on `not facts.facts` — **before** the LLM call. So every multi-series
   table makes no narration call at all, and the hint cannot exist. `grouped_bar` is therefore
   **dropped from the prompt enum** (it stays in `ChartKind`, which `chart.py` still uses).
2. **`Operation` is the variable that decides whether `dim` is a date**, not
   `Binding.temporal`. `compute._latest` is *gated on* `binding.temporal` yet emits **region
   labels** as `dim` (`compute.py:327`), and `_counted` does the same. Verified:

   ```
   aggregated SUM + temporal_column='date' -> deterministic kind = bar
   ```
3. **`BAR` is not a safe fallback.** `_ordered_categories` sorts by descending value when the
   table is complete (`chart.py:256`). Verified on a monthly series:

   ```
   forced-BAR x-axis order: ['2020-02', '2020-03', '2020-04', '2020-01']
   ```

   January renders after April, under an axis named for the temporal column. The axis silently
   encodes **rank**, not time — and `verify.py:181` would refuse that same superlative in prose.

## Solution Approach

**One optional enum field on the existing narration response.** `narrate.md` will ask for
`{"answer": "…", "chart": "line"}`, `chart` optional, drawn from **two** values: `line` | `bar`.

**Reorder `answer.py` so narration precedes chart construction.** Both depend only on `facts`,
`foot`, `binding` and `limitation`, all computed before either call today.

### The veto, restated as a theorem rather than a claim

Revision 1 spread the veto across four conditionals keyed on `temporal_column`. That is what
broke it. Replace the whole thing with an ordering:

```python
SUGGESTIVENESS = {ChartKind.BAR: 0, ChartKind.GROUPED_BAR: 1, ChartKind.LINE: 2}

deterministic = _kind_for(facts, temporal_column)      # unchanged, Phase 1
if preferred is None:
    final, source = deterministic, ChartSource.DETERMINISTIC
elif SUGGESTIVENESS[preferred] > SUGGESTIVENESS[deterministic]:
    final, source = deterministic, ChartSource.OVERRIDDEN   # promotion refused
elif preferred is deterministic:
    final, source = deterministic, ChartSource.CONFIRMED    # hint agreed; no change
else:
    final, source = preferred, ChartSource.MODEL            # genuine demotion
```

Three properties follow **by construction**, not by restatement:

- **A promotion is impossible.** The model can never obtain a kind the deterministic rule would
  not have produced. Every `Operation`, present and future, is covered, because the rule is
  expressed over the *outcome* rather than over the inputs that happen to produce it today.
- **It is total.** Every `(preferred, deterministic)` pair has an outcome.
- **`OVERRIDDEN` means something.** It is recorded only when the final kind differs from the
  request, so the override rate is a real signal rather than noise.

### Demotion must not itself be dishonest

`LINE → BAR` is only safe if the bars stay in time order. So, paired with the veto:

> When the plotted dimension is the temporal column, `_ordered_categories` must preserve
> **chronological** order regardless of `complete`.

Without this, the feature's own headline case ships the defect it exists to remove. This is a
change to Phase 1 code and needs its own test.

### Fail closed on a rejected narration

If `verify.check_claims` rejects the prose, **discard the hint**. Revision 1 argued the enum was
still safe because the veto protected it; the panel showed the correlation is not incidental —
the guard's likeliest rejections are unlicensed *trend* and *superlative* language
(`verify.py:106`), the same semantic class as choosing a line or a ranked bar. Honouring the
hint there draws in pixels the claim the guard just refused in prose.

## Exactly what changes in ADR-0007, and what does not

### Unchanged — every clause except Decision §8's first sentence

- The model never sees the table; it receives opaque `{FACT_n}` / `{LABEL_n}` tokens.
- The model never emits a quantity; `verify.check_claims` runs unchanged.
- The model never influences which rows aggregate, what the figures are, or whether an answer
  is refused.
- The model never emits a chart option; `validate_spec` approves every option.
- The model cannot cause a chart to exist.

### Changed — precisely one sentence

ADR-0007 Decision §8 opens *"Shape decides the chart, never the model."* After this, shape
decides the **ceiling**; the model may choose a less suggestive chart beneath it.

### Blast radius, corrected

Revision 1 claimed a compromised model could achieve "a bar where a line would have been
prettier". That was wrong twice over — it could have promoted an aggregate to a trend line, and
"bar" was not harmless. With the veto and the ordering fix above, the true radius is:

> **A bar chart, in chronological order, where a line chart would have been drawn.**

It cannot promote, cannot reorder an axis, cannot reach a wrong figure, and cannot suppress a
caveat. One further honest note the panel raised: switching to a bar turns on `xAxis.data`,
which carries publisher-controlled category strings that the line path never emits. They remain
inert (`validate_spec` allows only scalars) but they are a rendering surface that did not exist,
and the ADR must say so rather than claim "no new surface".

## Relevant Files

- `src/pythia/synthesis/chart.py` — `build_spec` gains `preferred`; `_ordered_categories` gains
  chronological ordering for temporal dimensions. **No logging here** — the module is marked
  pure in CLAUDE.md §4.
- `src/pythia/synthesis/narrate.py` — `write()` returns the parsed hint; the parser lives here.
- `src/pythia/synthesis/prompts/narrate.md` — one optional field, one rule, two enum values.
- `src/pythia/synthesis/answer.py` — reorder; thread `binding.temporal` and the hint; **emit
  `chart_source` on the existing `synthesis.done` event** (`answer.py:317`), following the
  `verify.py` precedent of returning a result and letting the orchestrator log.
- `src/pythia/synthesis/models.py` — `ChartSource`, and `ChartSpec.source` with **no default**
  (ADR-0006 precedent: honesty-bearing fields do not get defaults).
- `src/pythia/api/metrics.py`, `metrics_schema.sql` — `chart_kind`, `chart_source`, **plus an
  explicit `ALTER TABLE` migration**: the schema is `CREATE TABLE IF NOT EXISTS`, so adding
  columns to the file alone leaves existing databases unchanged and every subsequent write
  fails silently (`record()` swallows errors by design).
- `docs/adr/0010-llm-chart-kind.md` — **new ADR**, per repo precedent: every decision change
  got a numbered ADR; bare amendments were reserved for fixes that *tightened* a contract.
- `docs/adr/0007-…` — back-reference amendment. `docs/adr/0009-…` — its "Not decided here"
  section resolved, and its line 8 claim ("never chooses a chart") annotated.

### New Files

- `tests/test_synthesis_chart_hint.py` — the veto matrix and parsing.

## Step by Step Tasks

### 1. Chronological ordering for temporal bars (Phase 1 correction, independently valuable)

- Thread `temporal_column` into `_series` / `_ordered_categories`.
- When the dimension is the temporal column, sort chronologically regardless of `complete`.
- **Test first:** a complete monthly series drawn as bars stays in month order.
- This lands even if the rest is rejected: it fixes a real defect reachable today whenever
  `_kind_for` picks `BAR` on a temporal table.

### 2. `ChartSource`, with no default

```python
class ChartSource(StrEnum):
    DETERMINISTIC = "deterministic"  # no hint offered
    CONFIRMED = "confirmed"          # hint offered, matched the deterministic kind
    MODEL = "model"                  # hint accepted, chart is less suggestive
    OVERRIDDEN = "overridden"        # hint refused as a promotion
```

`CONFIRMED` exists so the override rate has a denominator and `MODEL` counts only real changes.

### 3. Prompt: two values, and field order

- Output becomes `{"answer": "…", "chart": "line"}` — **`answer` first**, so a truncated
  response loses the hint rather than the whole object (`num_predict` is 400).
- The rule, marked explicitly as the first discretionary one:
  > **Optional.** Choose the chart that fits **the answer you just wrote**. Use `line` if you
  > described a change over time; use `bar` if you described which is largest, smallest, or how
  > things compare. If unsure, omit the field.
- Raise `synthesis_max_narration_tokens` 400 → 448 to offset the added field.

### 4. Parse defensively

Normalise `casefold()`, strip `" .«»\"'\n"`, collapse separators, then match an alias set.
Cover, at minimum: `None`, `""`, `"LINE"`, `" line "`, `"line."`, `"γραμμή"`, `"ράβδοι"`,
`"bar chart"`, `"pie"`, `42`, `True`, `["line"]`, `{"kind": "line"}`. **Never raise.** Also
handle the key itself being `chart_type` / `chartKind` / `type`.

A valid `chart` with a missing or blank `answer` still degrades wholesale — `narrate.write`
returns `None` at `narrate.py:147` and that behaviour is deliberate, not an oversight to
"helpfully" salvage.

### 5. Implement the veto and reorder `answer.py`

- As the code block above. Resolve `(kind, source)` **before** the early `return None` paths in
  `build_spec`, so the override rate is not biased by charts that were never drawn; carry a
  `SUPPRESSED` case for those.
- Drop the hint when `check_claims` rejects.

### 6. Metrics, with a migration

- `ALTER TABLE answers ADD COLUMN chart_kind TEXT` / `chart_source TEXT`, guarded so it is
  idempotent. Add a test that an **existing** database gains the columns.
- `/stats`: report `MODEL / (MODEL + CONFIRMED + OVERRIDDEN)` and state the denominator.

### 7. ADR-0010, and a live eval

- New ADR; back-reference in 0007; annotate 0009.
- **Before ratifying**, run the two headline questions against a real single-series temporal
  resource and record whether `qwen3.5:9b` actually distinguishes them. ADR-0004 records this
  repo losing a phase to a path only `FakeLLM` exercised; the whole justification here is that
  the model can tell a trend question from a superlative one, and that is **currently
  unevidenced**.

## Testing Strategy

The matrix now has an `operation` axis, which is what revision 1 lacked:

| `preferred` | `operation` | temporal col | deterministic | final | `source` |
|---|---|---|---|---|---|
| `None` | NONE | set | LINE | LINE | DETERMINISTIC |
| `LINE` | NONE | set | LINE | LINE | CONFIRMED |
| `BAR` | NONE | set | LINE | **BAR, chronological** | MODEL |
| **`LINE`** | **LATEST** | **set** | **BAR** | **BAR** | **OVERRIDDEN** |
| **`LINE`** | **SUM** | **set** | **BAR** | **BAR** | **OVERRIDDEN** |
| **`LINE`** | **COUNT** | **set** | **BAR** | **BAR** | **OVERRIDDEN** |
| `LINE` | NONE | unset | BAR | BAR | OVERRIDDEN |
| `BAR` | SUM | — | BAR | BAR | CONFIRMED |

The four bold rows are the panel's blocker. **`LATEST` is the one most likely to ship broken**:
the LLM *is* called (one Fact exists), `Binding.temporal` *is* set, and `dim` is a region name.

Also required: chronological order on a demoted temporal bar; hint dropped on guard rejection;
no extra LLM call (assert `len(FakeLLM.calls)` unchanged); degradation on `LLMError` **and** on
a bare `ValueError` (`llm.py:147` calls `resp.json()` outside any try); `validate_spec` still
runs on every produced option.

## Acceptance Criteria

- **A promotion is impossible for every `Operation`**, asserted per row of the matrix above.
- A demoted temporal bar chart is in chronological order.
- The hint is discarded when the claim guard fires.
- No additional LLM call, asserted.
- `chart_source` distinguishes CONFIRMED from MODEL, and `/stats` states its denominator.
- An existing `metrics.sqlite` gains the new columns.
- ADR-0010 written; 0007 and 0009 updated.
- A recorded live run showing the model does or does not distinguish the two question shapes.
- `make check` green.

## Validation Commands

- `uv run ruff check .` · `uv run mypy` · `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run pytest -q`
- `uv run pytest tests/test_synthesis_chart_hint.py tests/test_synthesis_chart_kind.py -q`
- Two questions over a **single-series temporal** resource, asserting on `chart_source` in the
  `synthesis.done` event rather than eyeballing the rendered shape — revision 1's validation
  could not discriminate, because `compute.summarise` never reads `plan.params` and both
  questions produce an identical `FactTable`.

## Notes

**No new dependencies.** `make eval` not required.

**Step 1 is separable and should land regardless of the verdict on the rest.**

**The strongest argument against this spec**, sharpened by the panel: the reachable surface is
one enum value on one branch (`Operation.NONE`, single series), the win is one question shape,
and it costs an amendment to the project's central honesty contract plus a live-eval obligation.
Ship Phase 1 and step 1 alone and the honesty defect is already fixed for free. **The panel's
architecture judge concluded the feature belongs "narrowly, yes — but not on this design"; this
revision is that design, and the decision to proceed is still open.**

## Panel review

Four independent judges reviewed revision 1. **All four returned BLOCK.** Every finding below
was verified against the running code before being accepted.

**The unanimous blocker.** The veto keyed on `temporal_column` instead of the deterministic
outcome. `binding.temporal` being set does *not* mean the plotted dimension is a date —
`_latest` and `_counted` are both gated on it while emitting category labels. Three judges
found this independently; two reproduced it. Fixed by expressing the veto over suggestiveness
ordering, which makes it total and future-proof against new `Operation` values.

**The finding I had not considered.** `BAR` is not a safe fallback: `_ordered_categories` sorts
by value when complete, so a demoted temporal series renders January after April on an axis
named for the date column — a superlative `verify.py` would refuse in prose. Revision 1
asserted the opposite in as many words.

**Reachability.** `grouped_bar` is unrequestable (multi-series tables never call the LLM), so
the enum is two values. Revision 1's motivating example for it was unreachable.

**Corrections to revision 1's own account.** Its claim that the model is told nothing about data
shape was wrong — `narrate.py:133` already sends `Operation: {value}`. Its headline validation
could not discriminate, because `compute.summarise` accepts `params` and never reads it. It
listed an already-existing test file as new.

**Also fixed:** hint dropped on guard rejection (the correlation is not incidental); logging
moved out of the pure `chart.py`; `ChartSource` given no default and a `CONFIRMED` value;
`ALTER TABLE` migration added; ADR-0010 required rather than a bare amendment; parsing widened
to what a JSON-mode small model actually returns.

**Raised and deliberately not adopted here:** two judges noted a series-name truncation
collision in `compute.py:273` that silently drops observations under grouped bars. It is real
but **pre-existing and independent of this feature** — `grouped_bar` is unreachable through the
model. Filed as its own concern rather than folded in.
