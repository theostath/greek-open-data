"""Tests for explicit Phase 6 coercion. Pure, offline, byte-literal inputs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pythia.synthesis.coerce import coerce_column, to_decimal, to_temporal
from pythia.synthesis.models import CoercedKind


def kinds(cells: list[str | None], name: str = "col") -> CoercedKind:
    """Coerce a column and return only its resolved kind."""
    return coerce_column(name, cells).kind


def test_greek_decimal_comma_parses() -> None:
    """The flagship statistical measure is 86,6 and must read as 86.6, not 866."""
    column = coerce_column("OBS_VALUE", ["86,6", "93", "91,3"])
    assert column.kind is CoercedKind.DECIMAL
    assert column.values[0] == Decimal("86.6")


def test_greek_thousands_and_decimal_together() -> None:
    """1.234,56 is 1234.56 under the Greek reading."""
    assert coerce_column("c", ["1.234,56"]).values[0] == Decimal("1234.56")


def test_english_thousands_and_decimal_together() -> None:
    """1,234.56 is 1234.56 under the English reading."""
    assert coerce_column("c", ["1,234.56"]).values[0] == Decimal("1234.56")


def test_unresolvable_ambiguity_stays_text() -> None:
    """A column of only 1,234-shaped cells reads both ways, so it is not guessed."""
    assert kinds(["1,234", "5,678"]) is CoercedKind.TEXT


def test_sibling_cell_resolves_ambiguity() -> None:
    """One unambiguous decimal cell settles the whole column."""
    column = coerce_column("c", ["1,234", "86,6"])
    assert column.kind is CoercedKind.DECIMAL
    assert column.values[0] == Decimal("1.234")


def test_mixed_readings_stay_text() -> None:
    """Both separators as decimals in one column has no honest resolution."""
    assert kinds(["1,5", "2.5"]) is CoercedKind.TEXT


def test_nan_never_promotes_a_column() -> None:
    """Decimal('nan') parses, so admitting it would make every later sum silently NaN."""
    for sentinel in ("NaN", "nan", "-Infinity", "inf", "sNaN"):
        assert kinds(["1", "2", sentinel]) is CoercedKind.TEXT, sentinel


def test_absurd_precision_is_rejected() -> None:
    """A multi-million-digit cell is a CPU denial of service, not a figure."""
    assert kinds(["1" * 40, "2"]) is CoercedKind.TEXT
    assert to_decimal("1e999999999", comma_decimal=False, max_digits=32) is None


def test_empty_string_and_none_coerce_identically() -> None:
    """The CSV path yields '' where DataStore yields None; both must mean 'no value'.

    Without this the same table aggregates via DataStore and refuses via CSV.
    """
    from_csv = coerce_column("c", ["1", "", "3"])
    from_datastore = coerce_column("c", ["1", None, "3"])
    assert from_csv.kind is from_datastore.kind is CoercedKind.DECIMAL
    assert from_csv.values == from_datastore.values
    assert from_csv.null_count == from_datastore.null_count == 1


def test_sentinels_are_nulls_not_values() -> None:
    """A ':' or 'Δ/Υ' must not keep an otherwise-numeric column as text."""
    column = coerce_column("c", ["1", "2", ":", "Δ/Υ", "N/A"])
    assert column.kind is CoercedKind.DECIMAL
    assert column.sentinel_count == 3
    assert column.null_count == 0


def test_observation_flag_is_captured_not_stripped() -> None:
    """'86,6 p' is provisional; the flag is recorded so superlatives can be withheld."""
    column = coerce_column("c", ["86,6 p", "90,1"])
    assert column.kind is CoercedKind.DECIMAL
    assert column.flagged_rows == frozenset({0})


def test_percent_is_captured_as_a_unit() -> None:
    """A stripped % would let percentages be summed as if they were counts."""
    column = coerce_column("c", ["45,3%", "12,0%"])
    assert column.unit == "%"


def test_scale_hint_is_read_and_never_applied() -> None:
    """'σε χιλιάδες' is carried into the rendering, not multiplied into the value."""
    column = coerce_column("Δαπάνες (σε χιλιάδες ευρώ)", ["4,5"])
    assert column.scale_hint == "χιλιάδες"
    assert column.values[0] == Decimal("4.5")


def test_year_month_period_is_temporal() -> None:
    """TIME_PERIOD='2010-01' is typed text upstream but is a period."""
    column = coerce_column("TIME_PERIOD", ["2010-01", "2010-02"])
    assert column.kind is CoercedKind.TEMPORAL
    assert column.values[0] == date(2010, 1, 1)


def test_greek_day_first_dates() -> None:
    """13/07/2026 is 13 July; the column resolves the order once for every row."""
    column = coerce_column("d", ["13/07/2026", "01/02/2026"])
    assert column.values == [date(2026, 7, 13), date(2026, 2, 1)]


def test_fully_ambiguous_dates_are_refused() -> None:
    """When no row disambiguates, Greek convention is a habit, not a proof."""
    assert kinds(["05/06/2026", "01/02/2026"], "d") is CoercedKind.TEXT


def test_timestamps_are_not_timezone_converted() -> None:
    """Converting +03:00 to UTC would move a Greek daily count onto the previous day."""
    assert to_temporal("2023-12-05T00:30:00+03:00") == date(2023, 12, 5)
