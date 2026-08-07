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
