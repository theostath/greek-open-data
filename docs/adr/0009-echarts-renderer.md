# ADR 0009: Apache ECharts as the chart renderer

## Status

Accepted · 2026-08-07

Supersedes the Vega-Lite choice recorded in `CLAUDE.md §3` and amends ADR-0007's Phase 7
rendering contract. Does **not** change ADR-0007's grounding contract: the LLM still never
touches the numbers and never chooses a chart.

## Context

Two motivations, one of which turned out to rest on a mistake.

**The wanted change** was a richer chart vocabulary than Vega-Lite, with **Highcharts** named
specifically, and eventually some LLM involvement in chart selection.

**The mistake** was mine and is worth recording so it is not repeated. Highcharts is
**CC BY-NC 3.0** — NonCommercial — and its updated EULA narrows free use further: personal and
educational use only, with internal business use requiring a paid licence. I initially recorded
in issue #21 that adopting Apache-2.0 (#20) had "settled" the licensing. **That was backwards.**
Apache-2.0 grants everyone who receives this project a perpetual, royalty-free right to use it
commercially; Highcharts' terms forbid exactly that. Vendoring Highcharts here would have meant
sublicensing a freedom we have no right to grant. Buying a commercial licence would not fix it
either — that covers our use, not everyone who clones the repo.

So the licence that made the project *properly* open source is precisely what ruled Highcharts
out.

## Decision

**Apache ECharts 5.5.1 (`common` build) replaces Vega-Lite as the chart renderer.**

- **Apache-2.0**, the same licence as this project, with commercial use and redistribution
  explicitly granted. No conflict to manage.
- `synthesis/chart.py` builds every option object deterministically, exactly as before.
  `validate_spec` still approves it. **Chart choice remains a function of data shape.**
- `ChartSpec.vega_lite` is renamed to `ChartSpec.option` — the renderer's own term. A
  neutral-sounding name would claim a portability this dict does not have.

## Rationale

- **Licence compatibility is the deciding constraint**, not features. ECharts is the only
  strong candidate under the project's own licence.
- **Decal patterns.** ECharts' accessibility module fills series with textures alongside
  colour. DESIGN.md forbids encoding meaning in hue alone; Vega-Lite had no equivalent, so
  colourblind safety rested entirely on palette choice. Verified active in the rendered output.
- **Ordering moves into Python.** Vega-Lite sorted bars with `sort: "-y"`. ECharts has no
  equivalent flag, so ordering is now computed in `_ordered_categories` — which is better:
  the ranking is inspectable and testable rather than delegated to a renderer, and the rule
  "never rank a truncated table" is enforced where it can be read.
- **Bundle size is comparable, not smaller.** 662 KB replaces 812 KB of Vega. An earlier claim
  of "~186 KB" was wrong — that is a gzipped or custom-build figure, and the full dist is
  1.0 MB. The `common` build was chosen over `simple` (468 KB, but drops grid features the
  axes need) and over `full` (chart types this product has no use for).

## Consequences

- **The guard had to be rewritten, and its threat model changed.** Vega-Lite's risk was
  expression-bearing keys (`signal`, `expr`, `calculate`). ECharts' is **JavaScript functions**
  — `formatter`, `renderItem`, `valueFormatter` and friends. Those cannot survive JSON
  transport, and the option ships inside `<script type="application/json">` and is parsed
  rather than evaluated, so the guarantee holds structurally. `validate_spec` refuses them by
  name anyway, so it does not depend on the transport staying JSON forever.
- **The allowlist is derived from what the builder emits**, so an unrecognised ECharts option
  is rejected until someone considers it. `radar`, `dataset`, `graphic` and `toolbox` are all
  refused today.
- **Data validation is stricter.** Series data must be scalars or `[x, y]` scalar pairs;
  objects are refused outright. This is where publisher-controlled values land.
- **Line charts use a real time axis** with `[x, y]` pairs, so gaps stay gaps. A category axis
  would space irregular dates evenly, and `smooth` is explicitly `false` — interpolating
  between reported points would imply precision the data does not have.
- **Animation is off in the option**, not just suppressed client-side, so a reduced-motion
  user is never animated at even if the client script fails to load.
- **~700 KB of vendored JavaScript** (htmx 2.0.4, echarts 5.5.1 common), digests asserted in
  `tests/test_api_assets.py`.

## Verification

The exact vendored bundle was rendered headlessly (ECharts SSR, SVG renderer) against a real
option produced by the live pipeline — the asylum-by-nationality answer, 26 Greek categories:

| Check | Result |
|---|---|
| SVG produced | 10,821 bytes |
| Greek category labels | present (`ΑΦΓΑΝΙΣΤΑΝ`) |
| Title | present |
| Decal pattern definitions | 1 (accessibility active) |

## Not decided here

**Whether the LLM may choose a chart kind** (issue #21 Part B). That supersedes part of
ADR-0007 and needs `/spec` plus an independent judge panel first, per the global CLAUDE.md.
The scoped intent is an **enum-only** tool — the model picks from `ChartKind` and nothing else,
while `chart.py` still builds and validates the option — but it is not implemented and not
ratified.
