"""Tests for deterministic Vega-Lite generation and the spec validator."""

from __future__ import annotations

import pytest
from tests.synthesis_fixtures import asylum_table, index_table, table

from pythia.synthesis.bind import bind_columns
from pythia.synthesis.chart import UnsafeSpecError, build_spec, validate_spec, vega_type
from pythia.synthesis.compute import summarise
from pythia.synthesis.models import ChartKind, CoercedKind


def spec_for(data, **kwargs):  # type: ignore[no-untyped-def]
    """Bind, summarise and build a chart in one step."""
    binding = bind_columns(data)
    facts = summarise(data, binding, language="el")
    assert facts is not None
    return build_spec(facts, title="τίτλος", complete=data.complete, **kwargs)


def test_categorical_data_becomes_a_bar_chart() -> None:
    """One label column and one measure is a bar chart, sorted by value when complete."""
    chart = spec_for(asylum_table())
    assert chart is not None
    assert chart.kind is ChartKind.BAR
    assert chart.vega_lite["encoding"]["x"]["sort"] == "-y"


def test_time_series_becomes_a_line_chart() -> None:
    """A period axis with a measure is a line."""
    chart = spec_for(index_table(complete=True))
    assert chart is not None
    assert chart.kind is ChartKind.LINE
    assert chart.vega_lite["encoding"]["x"]["type"] == "temporal"


def test_measure_axis_is_quantitative_not_nominal() -> None:
    """Column.type says 'text' for 86,6 because Greek uses a decimal comma.

    Encoding that as nominal makes Vega sort the axis lexically — '100' < '86,6' — and the
    chart's magnitudes come out scrambled.
    """
    chart = spec_for(index_table(complete=True))
    assert chart is not None
    assert chart.vega_lite["encoding"]["y"]["type"] == "quantitative"
    assert vega_type(CoercedKind.DECIMAL) == "quantitative"


def test_incomplete_table_is_not_ranked_by_value() -> None:
    """A visual ranking is a superlative claim about rows we never fetched."""
    rows = [{"dim": "a", "value": 2}, {"dim": "b", "value": 1}]
    from pythia.synthesis.models import FactTable, Operation
    facts = FactTable(facts=[], series=rows, operation=Operation.SUM, row_basis=2)
    partial = build_spec(facts, title="t", complete=False)
    assert partial is not None
    assert partial.vega_lite["encoding"]["x"]["sort"] == "x"


def test_caveat_is_a_literal_subtitle_never_an_expression() -> None:
    """A signal or calculate here would be an expression-injection vector."""
    chart = spec_for(asylum_table(), caveat="μερικά δεδομένα")
    assert chart is not None
    assert chart.vega_lite["title"]["subtitle"] == "μερικά δεδομένα"


def test_untrusted_names_never_become_field_references() -> None:
    """A column called 'Δείκτης [2021=100]' is read by Vega as a nested accessor.

    Fixed synthetic data keys keep the chart working, and keep a hostile name inert.
    """
    data = table([("Δείκτης [2021=100]", "text"), ("Πλήθος", "integer")],
                 [{"Δείκτης [2021=100]": "Α", "Πλήθος": "3"},
                  {"Δείκτης [2021=100]": "Β", "Πλήθος": "5"}])
    chart = spec_for(data)
    assert chart is not None
    assert chart.vega_lite["encoding"]["x"]["field"] == "dim"
    assert all(set(point) <= {"dim", "value", "series"} for point in
               chart.vega_lite["data"]["values"])


@pytest.mark.parametrize(
    "hostile",
    [
        {"data": {"url": "https://attacker.example/x.json"}},
        {"transform": [{"calculate": "1", "as": "x"}]},
        {"title": {"text": {"signal": "alert(1)"}}},
        {"params": [{"name": "p", "expr": "1"}]},
        {"datasets": {"a": []}},
    ],
)
def test_validate_spec_rejects_executable_keys(hostile: dict[str, object]) -> None:
    """'We do not emit expressions' has to be checked, not merely intended."""
    with pytest.raises(UnsafeSpecError):
        validate_spec(hostile)


def test_validate_spec_accepts_what_we_generate() -> None:
    """The validator must not reject the shapes the builder actually emits."""
    chart = spec_for(asylum_table())
    assert chart is not None
    validate_spec(chart.vega_lite)


def test_single_figure_produces_no_chart() -> None:
    """One bar is decoration. None is a legitimate outcome and must not be faked."""
    data = table([("Νομός", "text"), ("Πλήθος", "integer")], [{"Νομός": "ΑΤΤΙΚΗΣ", "Πλήθος": "1"}])
    binding = bind_columns(data)
    facts = summarise(data, binding, language="el")
    assert facts is not None
    assert build_spec(facts, title="t") is None
