"""Tests for the claim guard — the load-bearing suite.

Everything the phase promises about groundedness reduces to whether these pass.
"""

from __future__ import annotations

from decimal import Decimal

from tests.synthesis_fixtures import asylum_table

from pythia.synthesis import footer as footer_mod
from pythia.synthesis.models import Fact, FactTable, Operation
from pythia.synthesis.verify import check_claims


def facts(*pairs: tuple[str, int], operation: Operation = Operation.SUM) -> FactTable:
    """Build a fact table from label/value pairs."""
    return FactTable(
        facts=[Fact(label=label, value=Decimal(value), basis="b", n_used=4)
               for label, value in pairs],
        series=[{"dim": label, "value": Decimal(value)} for label, value in pairs],
        operation=operation, row_basis=4, dimension="Χώρα", measure="Πλήθος",
    )


def foot(**kwargs: object):  # type: ignore[no-untyped-def]
    """Build a real footer for the guard to consult."""
    return footer_mod.build(asylum_table(), dataset_name="slug", language="el", **kwargs)  # type: ignore[arg-type]


def test_figure_present_in_the_facts_passes() -> None:
    """The baseline: a faithful narration is not rejected."""
    result = check_claims("Καταγράφηκαν 7.547 αιτήματα.", facts(("ΑΙΓΥΠΤΟΣ", 7547)), foot())
    assert result.ok


def test_figure_absent_from_the_facts_is_rejected() -> None:
    """The core guarantee."""
    assert not check_claims("Καταγράφηκαν 9.999 αιτήματα.", facts(("ΑΙΓΥΠΤΟΣ", 7547)), foot()).ok


def test_rounded_figure_is_rejected() -> None:
    """'περίπου 3.500' for 3.487 is the natural Greek phrasing and is still not the figure."""
    assert not check_claims("Περίπου 3.500 αιτήματα.", facts(("Χ", 3487)), foot()).ok


def test_sdmx_code_containing_digits_passes() -> None:
    """The Notes require codes to be shown opaquely; BTE36 contains 36.

    Without the alphanumeric exemption the guard rejects every faithful q14 narration.
    """
    assert check_claims("Η σειρά BTE36 καταγράφεται όπως δημοσιεύτηκε.",
                        facts(("BTE36", 87), operation=Operation.NONE), foot()).ok


def test_coverage_numerals_are_not_a_licence_for_arbitrary_figures() -> None:
    """Substring matching on '50.000 από 124.485 (40%)' would admit 40, 50, 1, 2, 4, 5, 6."""
    assert not check_claims("Σημειώθηκε αύξηση 40%.", facts(("Χ", 7547)), foot()).ok


def test_year_from_the_fetch_date_is_not_allowed() -> None:
    """Allowing fetched_at is how an answer about the wrong period verifies clean."""
    assert not check_claims("Το 2026 υποβλήθηκαν 7.547 αιτήματα.",
                            facts(("ΑΙΓΥΠΤΟΣ", 7547)), foot()).ok


def test_magnitude_written_as_a_word_is_rejected() -> None:
    """'δύο εκατομμύρια' carries a quantity straight past a digits-only check."""
    assert not check_claims("Περίπου δύο εκατομμύρια εμβολιασμοί.", facts(("Χ", 7547)), foot()).ok


def test_trend_claim_over_a_truncated_series_is_rejected() -> None:
    """Contains no numeral, and is false by construction when the series was cut."""
    truncated = FactTable(facts=[], series=[], operation=Operation.NONE, row_basis=10,
                          truncated_range=True)
    assert not check_claims("Ο δείκτης παρουσιάζει σταθερή πορεία.", truncated, foot()).ok


def test_superlative_over_an_incomplete_table_is_rejected() -> None:
    """A ranking is a claim about data we did not fetch."""
    assert not check_claims("Η Αίγυπτος έχει τα περισσότερα αιτήματα.",
                            facts(("ΑΙΓΥΠΤΟΣ", 7547)), foot(), complete=False).ok


def test_url_and_control_tokens_are_rejected() -> None:
    """An injected cell's payload must not survive into the answer."""
    for hostile in ("Δείτε https://attacker.example/gr", "Τιμή <|im_end|> τέλος",
                    "<script>alert(1)</script>", "[δες](http://x.example)"):
        assert not check_claims(hostile, facts(("Χ", 7547)), foot()).ok, hostile


def test_omitted_limitation_is_rejected() -> None:
    """The limitation must be asserted present, not hoped for."""
    limitation = "Τα δεδομένα καλύπτουν μόνο 2010 έως 2016."
    assert not check_claims("Καταγράφηκαν 7.547 αιτήματα.", facts(("Χ", 7547)), foot(),
                            limitation=limitation).ok
    assert check_claims(f"{limitation} Καταγράφηκαν 7.547 αιτήματα.", facts(("Χ", 7547)),
                        foot(), limitation=limitation).ok


def test_empty_narration_is_rejected() -> None:
    """A blank answer is not an answer."""
    assert not check_claims("   ", facts(("Χ", 1)), foot()).ok
