"""Tests for the only module allowed to produce a number."""

from __future__ import annotations

from decimal import Decimal

from tests.synthesis_fixtures import (
    ASYLUM_TOTAL,
    asylum_table,
    index_table,
    table,
    vaccination_table,
)

from pythia.synthesis.bind import bind_columns
from pythia.synthesis.compute import non_negative, summarise
from pythia.synthesis.models import ColumnRole, Operation


def facts_for(data, language: str = "el"):  # type: ignore[no-untyped-def]
    """Bind and summarise a table in one step."""
    return summarise(data, bind_columns(data), language=language)


def test_embedded_total_row_is_not_double_counted() -> None:
    """The single most dangerous case in this data, and the cleanest to state.

    q16 carries a ΣΥΝΟΛΟ row equal to the sum of the others, so a naive total is exactly 2x
    the truth — 147,374 against 73,687. Two-times is plausible, and would never be spotted.
    """
    facts = facts_for(asylum_table())
    assert facts is not None
    assert facts.operation is Operation.SUM
    assert sum(int(fact.value) for fact in facts.facts) == ASYLUM_TOTAL
    assert all("ΣΥΝΟΛΟ" not in fact.label for fact in facts.facts)


def test_publisher_stated_total_is_reported_separately() -> None:
    """A total the publisher wrote is better evidence than ours, and is never added to it."""
    facts = facts_for(asylum_table())
    assert facts is not None
    assert facts.publisher_stated_total is not None
    assert facts.publisher_stated_total.value == Decimal(ASYLUM_TOTAL)


def test_cumulative_column_reduces_to_its_latest_value() -> None:
    """Summing a running total over every region-day inflates the truth enormously.

    The honest reduction is the value at the last period, summed across areas: 110 + 230 = 340,
    against a naive 960 over the six region-days.
    """
    data = table([("referencedate", "timestamp"), ("area", "text"),
                  ("totalvaccinations", "integer")],
                 [{"referencedate": f"{day} 00:00:00", "area": area,
                   "totalvaccinations": str(base + 10 * step)}
                  for step, day in enumerate(["2023-12-01", "2023-12-02", "2023-12-03"])
                  for area, base in [("ΑΤΤΙΚΗΣ", 100), ("ΚΡΗΤΗΣ", 200)]])
    facts = summarise(data, bind_columns(data), language="el")
    assert facts is not None
    assert facts.operation is Operation.LATEST
    assert facts.facts[0].value == Decimal(340)
    assert facts.measure_role is ColumnRole.RUNNING_CUMULATIVE


def test_a_running_total_is_never_summed() -> None:
    """Whatever operation is chosen, the cumulative column must not be aggregated.

    On the full vaccination panel the daily deltas are the summable columns, so the running
    total is reported through the series path rather than added up.
    """
    data = vaccination_table()
    binding = bind_columns(data)
    facts = summarise(data, binding, language="el")
    assert facts is not None
    assert binding.roles["totalvaccinations"] is ColumnRole.RUNNING_CUMULATIVE
    assert not (facts.operation is Operation.SUM and facts.measure == "totalvaccinations")


def test_interleaved_series_are_kept_apart() -> None:
    """q14 stacks many series; one line over all of them renders a meaningless sawtooth."""
    facts = facts_for(index_table())
    assert facts is not None
    assert facts.series_field == "series"
    assert facts.duplicate_key_count == 0
    assert len({point["series"] for point in facts.series}) == 2


def test_unresolved_series_identity_refuses_to_chart() -> None:
    """Two observations for one period inside one series means we have not understood it."""
    data = table([("TIME_PERIOD", "text"), ("OBS_VALUE", "text")],
                 [{"TIME_PERIOD": "2010-01", "OBS_VALUE": "86,6"},
                  {"TIME_PERIOD": "2010-01", "OBS_VALUE": "95,8"},
                  {"TIME_PERIOD": "2010-02", "OBS_VALUE": "87,0"}],
                 title="ΔΕΙΚΤΗΣ")
    facts = summarise(data, bind_columns(data), language="el")
    assert facts is not None
    assert facts.operation is Operation.LISTING
    assert facts.duplicate_key_count > 0


def test_truncated_table_marks_its_range() -> None:
    """A cut series must not be describable as a trend."""
    facts = facts_for(index_table(complete=False))
    assert facts is not None
    assert facts.truncated_range is True


def test_empty_table_refuses_rather_than_answering_zero() -> None:
    """sum([]) == 0 narrated as '0 αιτήματα' is a confident false zero."""
    assert summarise(table([("a", "text")], []), bind_columns(table([("a", "text")], []))) is None


def test_all_null_measure_refuses() -> None:
    """A measure with no usable values yields no fact, not a zero."""
    data = table([("Νομός", "text"), ("Πλήθος", "integer")],
                 [{"Νομός": "ΑΤΤΙΚΗΣ", "Πλήθος": None},
                  {"Νομός": "ΚΡΗΤΗΣ", "Πλήθος": ""}])
    facts = summarise(data, bind_columns(data), language="el")
    assert facts is None or facts.operation is Operation.COUNT


def test_top_n_remainder_carries_magnitude_not_just_a_count() -> None:
    """A reader sums the visible bars; the hidden tail's size has to be stated."""
    rows = [{"Χώρα": f"Χ{i:03d}", "Πλήθος": str(100 - i)} for i in range(40)]
    data = table([("Χώρα", "text"), ("Πλήθος", "integer")], rows)
    facts = summarise(data, bind_columns(data), language="el")
    assert facts is not None
    assert facts.omitted_categories == 15
    assert facts.facts[-1].label == "Λοιπά"
    assert facts.facts[-1].value > 0


def test_spelling_variants_group_into_one_category() -> None:
    """Splitting ΑΤΤΙΚΗ across three bars understates every one of them."""
    data = table([("Περιοχή", "text"), ("Πλήθος", "integer")],
                 [{"Περιοχή": "ΑΤΤΙΚΗ", "Πλήθος": "10"},
                  {"Περιοχή": "Αττική", "Πλήθος": "5"},
                  {"Περιοχή": "ATTIKH", "Πλήθος": "3"}])
    facts = summarise(data, bind_columns(data), language="el")
    assert facts is not None
    assert len(facts.facts) == 1
    assert facts.facts[0].value == Decimal(18)


def test_lower_bound_labelling_requires_a_non_negative_measure() -> None:
    """A partial sum bounds the truth only when the measure cannot go negative.

    daydiff reaches -87 in the real vaccination resource, across 5,550 of 35,076 rows.
    """
    data = vaccination_table()
    binding = bind_columns(data)
    assert non_negative(binding, "daytotal") is True
    assert non_negative(binding, "daydiff") is False
