"""Tests for deterministic Apache ECharts option generation and the option validator."""

from __future__ import annotations

import pytest
from tests.synthesis_fixtures import asylum_table, index_table, table

from pythia.synthesis.bind import bind_columns
from pythia.synthesis.chart import UnsafeSpecError, axis_type, build_spec, validate_spec
from pythia.synthesis.compute import summarise
from pythia.synthesis.models import ChartKind, CoercedKind


def spec_for(data, **kwargs):  # type: ignore[no-untyped-def]
    """Bind, summarise and build a chart in one step."""
    binding = bind_columns(data)
    facts = summarise(data, binding, language="el")
    assert facts is not None
    return build_spec(facts, title="τίτλος", complete=data.complete, **kwargs)


def test_categorical_data_becomes_a_bar_chart() -> None:
    """One label column and one measure is a bar chart."""
    chart = spec_for(asylum_table())
    assert chart is not None
    assert chart.kind is ChartKind.BAR
    assert chart.option["series"][0]["type"] == "bar"


def test_a_complete_table_is_ranked_by_value() -> None:
    """Ordering is decided in Python, so the ranking is inspectable rather than a renderer flag."""
    rows = [{"dim": "a", "value": 1}, {"dim": "b", "value": 9}, {"dim": "c", "value": 5}]
    from pythia.synthesis.models import FactTable, Operation
    facts = FactTable(facts=[], series=rows, operation=Operation.SUM, row_basis=3)

    chart = build_spec(facts, title="t", complete=True)

    assert chart is not None
    assert chart.option["xAxis"]["data"] == ["b", "c", "a"]
    assert chart.option["series"][0]["data"] == [9, 5, 1]


def test_an_incomplete_table_keeps_published_order() -> None:
    """A visual ranking is a superlative claim about rows we never fetched."""
    rows = [{"dim": "a", "value": 2}, {"dim": "b", "value": 1}]
    from pythia.synthesis.models import FactTable, Operation
    facts = FactTable(facts=[], series=rows, operation=Operation.SUM, row_basis=2)

    partial = build_spec(facts, title="t", complete=False)

    assert partial is not None
    assert partial.option["xAxis"]["data"] == ["a", "b"]


def test_time_series_becomes_a_line_chart_on_a_time_axis() -> None:
    """A period axis with a measure is a line, on a real time axis."""
    chart = spec_for(index_table(complete=True))
    assert chart is not None
    assert chart.kind is ChartKind.LINE
    assert chart.option["xAxis"]["type"] == "time"


def test_a_line_series_carries_xy_pairs_so_gaps_stay_real() -> None:
    """A category axis would space irregular dates evenly, implying data we do not have."""
    chart = spec_for(index_table(complete=True))
    assert chart is not None

    data = chart.option["series"][0]["data"]
    assert all(isinstance(point, list) and len(point) == 2 for point in data)


def test_lines_are_never_smoothed() -> None:
    """Smoothing interpolates between reported points — precision the data does not have."""
    chart = spec_for(index_table(complete=True))
    assert chart is not None
    assert chart.option["series"][0]["smooth"] is False


def test_the_measure_axis_is_numeric_not_categorical() -> None:
    """Column.type says 'text' for 86,6 because Greek uses a decimal comma.

    Treating that as a category makes the axis sort lexically — '100' < '86,6' — and the
    chart's magnitudes come out scrambled.
    """
    chart = spec_for(index_table(complete=True))
    assert chart is not None
    assert chart.option["yAxis"]["type"] == "value"
    assert axis_type(CoercedKind.DECIMAL) == "value"
    assert axis_type(CoercedKind.TEMPORAL) == "time"


def test_caveat_is_a_literal_subtitle_never_a_template() -> None:
    """The caveat is exactly the text an attacker would want to rewrite."""
    chart = spec_for(asylum_table(), caveat="μερικά δεδομένα")
    assert chart is not None
    assert chart.option["title"]["subtext"] == "μερικά δεδομένα"


def test_untrusted_names_never_become_field_references() -> None:
    """A hostile column name must stay an inert label, never an accessor."""
    data = table([("Δείκτης [2021=100]", "text"), ("Πλήθος", "integer")],
                 [{"Δείκτης [2021=100]": "Α", "Πλήθος": "3"},
                  {"Δείκτης [2021=100]": "Β", "Πλήθος": "5"}])

    chart = spec_for(data)

    assert chart is not None
    # Values are plain arrays; no publisher string is ever read as a key.
    assert all(isinstance(v, int | float | type(None)) for v in chart.option["series"][0]["data"])


def test_the_colourblind_safe_palette_is_set_and_keeps_amber_last() -> None:
    """A data series painted amber-adjacent steals the accent's only job."""
    chart = spec_for(asylum_table())
    assert chart is not None

    palette = chart.option["color"]
    assert palette[0] == "#0072b2"
    assert palette.index("#e69f00") >= len(palette) - 2


def test_decal_patterns_are_enabled(  ) -> None:
    """A second, non-colour channel — DESIGN.md forbids encoding meaning in hue alone."""
    chart = spec_for(asylum_table())
    assert chart is not None
    assert chart.option["aria"]["enabled"] is True
    assert chart.option["aria"]["decal"]["show"] is True


def test_animation_is_off_so_reduced_motion_is_honoured() -> None:
    """A chart that animates on arrival regardless of the setting is a WCAG problem."""
    chart = spec_for(asylum_table())
    assert chart is not None
    assert chart.option["animation"] is False


@pytest.mark.parametrize(
    "hostile",
    [
        {"series": [{"type": "bar", "formatter": "alert(1)"}]},
        {"series": [{"type": "custom", "renderItem": "x"}]},
        {"tooltip": {"valueFormatter": "x"}},
        {"xAxis": {"axisLabel": {"formatter": "{value}"}}},
        {"dataset": {"source": []}},
        {"graphic": [{"type": "text"}]},
        {"toolbox": {"feature": {}}},
        {"title": {"on": "click"}},
        {"url": "https://attacker.example/x.json"},
    ],
)
def test_validate_spec_rejects_executable_keys(hostile: dict[str, object]) -> None:
    """ECharts accepts JS functions for these; JSON cannot carry one, but do not rely on that."""
    with pytest.raises(UnsafeSpecError):
        validate_spec(hostile)


def test_validate_spec_rejects_an_unknown_key() -> None:
    """Allowlist, not blocklist: a future ECharts option is rejected until considered."""
    with pytest.raises(UnsafeSpecError):
        validate_spec({"radar": {"indicator": []}})


def test_validate_spec_rejects_object_rows_in_data() -> None:
    """Data is where publisher values land, so it is the strictest check in the module."""
    with pytest.raises(UnsafeSpecError):
        validate_spec({"series": [{"type": "bar", "data": [{"value": 1, "url": "x"}]}]})


def test_validate_spec_rejects_an_over_long_data_pair() -> None:
    with pytest.raises(UnsafeSpecError):
        validate_spec({"series": [{"type": "bar", "data": [[1, 2, 3]]}]})


def test_validate_spec_accepts_what_we_generate() -> None:
    """The validator must not reject the shapes the builder actually emits."""
    for data in (asylum_table(), index_table(complete=True)):
        chart = spec_for(data)
        assert chart is not None
        validate_spec(chart.option)


def test_single_figure_produces_no_chart() -> None:
    """One bar is decoration. None is a legitimate outcome and must not be faked."""
    data = table([("Νομός", "text"), ("Πλήθος", "integer")],
                 [{"Νομός": "ΑΤΤΙΚΗΣ", "Πλήθος": "1"}])
    binding = bind_columns(data)
    facts = summarise(data, binding, language="el")
    assert facts is not None
    assert build_spec(facts, title="t") is None
