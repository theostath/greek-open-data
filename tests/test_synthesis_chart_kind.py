"""Chart-kind selection from the authoritative temporal signal (issue #21, Phase 1).

`bind.py` computes `Binding.temporal` — the column *proven* temporal — but `build_spec` never
received it and fell back to `_looks_temporal()`, a prefix heuristic on the first four
characters of a dimension value. Both tests below fail before this phase: one draws a line
chart across categories that merely look like years, the other draws bars over a real date
column whose format the heuristic does not recognise.

Drawing categories as a line asserts continuity between them. That is the specific dishonesty
this fixes, not a cosmetic preference.
"""

from __future__ import annotations

from pythia.synthesis.chart import build_spec
from pythia.synthesis.models import ChartKind, FactTable, Operation


def _facts(series: list[dict[str, object]], **kw: object) -> FactTable:
    return FactTable(
        facts=[], series=series, operation=kw.pop("operation", Operation.NONE),  # type: ignore[arg-type]
        row_basis=len(series), **kw,  # type: ignore[arg-type]
    )


def test_year_like_labels_are_not_a_time_series() -> None:
    """"2019"/"2020" as *categories* is a bar chart; the heuristic called it a line."""
    facts = _facts([{"dim": "2019", "value": 10}, {"dim": "2020", "value": 12}])

    chart = build_spec(facts, title="t", temporal_column=None)

    assert chart is not None
    assert chart.kind is ChartKind.BAR


def test_a_real_temporal_column_is_a_line_whatever_its_format() -> None:
    """A proven temporal column formatted 01/2020 was previously drawn as bars."""
    facts = _facts([{"dim": "01/2020", "value": 10}, {"dim": "02/2020", "value": 12}])

    chart = build_spec(facts, title="t", temporal_column="referencedate")

    assert chart is not None
    assert chart.kind is ChartKind.LINE


def test_multiple_series_alone_does_not_imply_time() -> None:
    """A categorical breakdown with a series field is not a time series."""
    facts = _facts(
        [{"dim": "Αττική", "value": 10, "series": "2019"},
         {"dim": "Κρήτη", "value": 12, "series": "2019"}],
        series_field="year",
    )

    chart = build_spec(facts, title="t", temporal_column=None)

    assert chart is not None
    assert chart.kind is ChartKind.GROUPED_BAR


def test_a_temporal_column_with_series_is_still_a_line() -> None:
    """Multi-series over real time is a multi-line chart, not grouped bars."""
    facts = _facts(
        [{"dim": "2019-01", "value": 10, "series": "Αττική"},
         {"dim": "2019-02", "value": 12, "series": "Αττική"}],
        series_field="region",
    )

    chart = build_spec(facts, title="t", temporal_column="month")

    assert chart is not None
    assert chart.kind is ChartKind.LINE


def test_the_heuristic_still_serves_callers_without_a_binding() -> None:
    """The CLI probe path has no Binding, so ISO-looking dims may still be inferred."""
    facts = _facts([{"dim": "2019-01-01", "value": 10}, {"dim": "2019-02-01", "value": 12}])

    chart = build_spec(facts, title="t")  # temporal_column omitted entirely

    assert chart is not None
    assert chart.kind is ChartKind.LINE


def test_an_aggregated_table_is_never_a_line_even_over_time() -> None:
    """A SUM collapses the time axis; plotting the result as a trend would invent one."""
    facts = _facts(
        [{"dim": "2019-01", "value": 10}, {"dim": "2019-02", "value": 12}],
        operation=Operation.SUM,
    )

    chart = build_spec(facts, title="t", temporal_column="month")

    assert chart is not None
    assert chart.kind is ChartKind.BAR


def test_a_temporal_dimension_drawn_as_bars_stays_chronological() -> None:
    """Ranking a time axis by magnitude turns chronology into a superlative claim.

    Not reachable in production today: `compute._series` is the only branch emitting date
    `dim` values and it is gated on `binding.temporal`, so `_kind_for` always returns LINE
    for it and the bar-ordering path is never entered with dates. This is hardening — the
    trap is disarmed before anything (a demotion, a new Operation) can spring it.

    `verify.py` refuses an unlicensed superlative in prose; a value-sorted date axis asserts
    exactly that superlative in pixels, which is the disagreement this prevents.
    """
    from pythia.synthesis.chart import _ordered_categories

    points = [{"dim": "2020-01", "value": 10.0}, {"dim": "2020-02", "value": 90.0},
              {"dim": "2020-03", "value": 50.0}, {"dim": "2020-04", "value": 20.0}]

    ordered = _ordered_categories(points, complete=True, temporal=True)

    assert ordered == ["2020-01", "2020-02", "2020-03", "2020-04"]


def test_a_categorical_dimension_is_still_ranked_by_value() -> None:
    """The ranking is the point for categories: it answers "which is largest?" visually."""
    from pythia.synthesis.chart import _ordered_categories

    points = [{"dim": "Κρήτη", "value": 10.0}, {"dim": "Αττική", "value": 90.0}]

    assert _ordered_categories(points, complete=True, temporal=False) == ["Αττική", "Κρήτη"]


def test_an_incomplete_categorical_table_keeps_published_order() -> None:
    """Unchanged behaviour: ranking rows we never fetched is a claim about absent data."""
    from pythia.synthesis.chart import _ordered_categories

    points = [{"dim": "Κρήτη", "value": 10.0}, {"dim": "Αττική", "value": 90.0}]

    assert _ordered_categories(points, complete=False, temporal=False) == ["Κρήτη", "Αττική"]
