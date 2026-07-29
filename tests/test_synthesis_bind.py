"""Tests for column-role classification and parameter binding."""

from __future__ import annotations

import pytest
from tests.synthesis_fixtures import asylum_table, index_table, table, vaccination_table

from pythia.synthesis.bind import TableTooLargeError, bind_columns, range_overlap
from pythia.synthesis.models import ColumnRole


def test_number_of_x_is_a_measure_not_an_identifier() -> None:
    """The regression that collapsed the flagship demo into a chart of 1-high bars.

    "Number of X" is the commonest measure-name form in this catalogue, so a name rule that
    matches `arith`/`αρ` destroys the very column the answer needs.
    """
    binding = bind_columns(asylum_table())
    assert binding.roles["ARIThMOS AITEMATON"] is ColumnRole.MEASURE
    assert binding.roles["UPEKOOTETA"] is ColumnRole.DIMENSION
    assert binding.dimension == "UPEKOOTETA"


def test_greek_measure_names_are_measures() -> None:
    """ΑΡΙΘΜΟΣ ΑΤΥΧΗΜΑΤΩΝ is a count of accidents, not a row counter."""
    data = table([("Νομός", "text"), ("ΑΡΙΘΜΟΣ ΑΤΥΧΗΜΑΤΩΝ", "integer")],
                 [{"Νομός": "ΑΤΤΙΚΗΣ", "ΑΡΙΘΜΟΣ ΑΤΥΧΗΜΑΤΩΝ": "1234"},
                  {"Νομός": "ΚΡΗΤΗΣ", "ΑΡΙΘΜΟΣ ΑΤΥΧΗΜΑΤΩΝ": "567"}])
    assert bind_columns(data).roles["ΑΡΙΘΜΟΣ ΑΤΥΧΗΜΑΤΩΝ"] is ColumnRole.MEASURE


def test_row_counter_and_region_code_are_identifiers() -> None:
    """A dense 1..n sequence and an id-suffixed code are both keys, never quantities."""
    binding = bind_columns(vaccination_table())
    assert binding.roles["areaid"] is ColumnRole.IDENTIFIER
    data = table([("Arithmese", "number"), ("Idruma", "text")],
                 [{"Arithmese": str(i + 1), "Idruma": f"ΠΑΝ{i}"} for i in range(4)])
    assert bind_columns(data).roles["Arithmese"] is ColumnRole.IDENTIFIER


def test_cumulative_column_is_detected_and_deltas_are_not() -> None:
    """totalvaccinations accumulates; daytotal and daydiff are per-day observations."""
    binding = bind_columns(vaccination_table())
    assert binding.roles["totalvaccinations"] is ColumnRole.RUNNING_CUMULATIVE
    assert binding.roles["daydiff"] is ColumnRole.MEASURE
    assert "totalvaccinations" not in binding.measures


def test_cumulative_without_a_time_axis_is_unknown_not_measure() -> None:
    """Absence of evidence is not evidence of summability.

    Without a temporal column the monotonicity test cannot run, and defaulting to "safe to
    sum" is what puts a vaccination total four orders of magnitude out.
    """
    data = table([("area", "text"), ("totalvaccinations", "integer")],
                 [{"area": "ΑΤΤΙΚΗΣ", "totalvaccinations": "100"},
                  {"area": "ΚΡΗΤΗΣ", "totalvaccinations": "200"}])
    binding = bind_columns(data)
    assert binding.roles["totalvaccinations"] is ColumnRole.UNKNOWN
    assert binding.measures == []


def test_base_year_is_not_a_measure() -> None:
    """BASE_PER holds 2021 — the index base year, not a quantity."""
    assert bind_columns(index_table()).roles["BASE_PER"] is not ColumnRole.MEASURE


def test_price_index_is_not_aggregable() -> None:
    """OBS_VALUE is an index; summing it across activities is arithmetically meaningless."""
    binding = bind_columns(index_table())
    assert binding.roles["OBS_VALUE"] is ColumnRole.INDEX
    assert "OBS_VALUE" not in binding.measures


def test_period_column_becomes_the_time_axis() -> None:
    """Without this the flagship line chart is unreachable."""
    assert bind_columns(index_table()).temporal == "TIME_PERIOD"


def test_constant_columns_are_context_not_dimensions() -> None:
    """FREQ=M everywhere describes the table; it does not divide it."""
    binding = bind_columns(index_table())
    assert "FREQ" in binding.constant_columns
    assert binding.dimension != "FREQ"


def test_greek_spelling_variants_are_reported_as_merged() -> None:
    """Case, accent and Latin-homoglyph spellings of one category must not split it."""
    data = table([("Περιοχή", "text"), ("Πλήθος", "integer")],
                 [{"Περιοχή": "ΑΤΤΙΚΗ", "Πλήθος": "10"},
                  {"Περιοχή": "Αττική", "Πλήθος": "5"},
                  {"Περιοχή": "ATTIKH", "Πλήθος": "3"},
                  {"Περιοχή": "ΚΡΗΤΗ", "Πλήθος": "7"}])
    merged = bind_columns(data).merged_variants
    assert any(len(spellings) == 3 for spellings in merged.values())


def test_percent_measure_forbids_sum_and_average() -> None:
    """Percentages have no meaningful sum and no unweighted mean."""
    data = table([("Νομός", "text"), ("Ποσοστό", "number")],
                 [{"Νομός": "ΑΤΤΙΚΗΣ", "Ποσοστό": "45,3%"},
                  {"Νομός": "ΚΡΗΤΗΣ", "Ποσοστό": "12,0%"}])
    from pythia.synthesis.models import Operation
    forbidden = bind_columns(data).forbidden_ops
    assert Operation.SUM in forbidden and Operation.AVG in forbidden


def test_oversized_table_is_refused_rather_than_coerced() -> None:
    """access_max_bytes bounds the wire, not resident Decimals."""
    wide = [(f"c{i}", "text") for i in range(30)]
    rows = [{f"c{i}": "x" for i in range(30)} for _ in range(30)]
    from config import get_settings
    cfg = get_settings().model_copy(update={"synthesis_max_cells": 100})
    with pytest.raises(TableTooLargeError):
        bind_columns(table(wide, rows), settings=cfg)


def test_range_overlap_classifies_the_three_cases() -> None:
    """A disjoint request is a refusal; a partial one is a qualified answer."""
    observed = ("2016-01-01", "2018-12-31")
    assert range_overlap(observed, "2024-01-01", "2024-12-31") == "none"
    assert range_overlap(observed, "2017-01-01", "2024-12-31") == "partial"
    assert range_overlap(observed, None, None) == "full"


def test_unbound_filter_is_surfaced_not_dropped() -> None:
    """A region filter that no column can satisfy must reach the caller."""
    from pythia.planning.models import QueryParams
    binding = bind_columns(asylum_table(), QueryParams(region="Κρήτη"))
    assert "region" in binding.unbound
