"""End-to-end tests for the orchestrator, including every refusal path."""

from __future__ import annotations

import pytest
from tests.synthesis_fixtures import (
    asylum_table,
    index_table,
    plan,
    table,
    vaccination_table,
)

from pythia.access.models import (
    MalformedPayloadError,
    ResourceUnavailableError,
    UnsupportedResourceError,
)
from pythia.llm import FakeLLM
from pythia.planning.models import PlanStatus, QueryParams
from pythia.synthesis.answer import answer_question
from pythia.synthesis.models import AnswerStatus, RefusalContext


def test_answer_without_an_llm_is_complete_and_grounded() -> None:
    """The whole phase must work with no model at all — every gate leans on that."""
    answer = answer_question("Πόσα αιτήματα ασύλου;", plan(), asylum_table())
    assert answer.status in {AnswerStatus.ANSWERED, AnswerStatus.PARTIAL}
    assert answer.footer is not None
    assert answer.degraded is True
    assert "73.687" in answer.text or "7.547" in answer.text


def test_every_non_refused_answer_carries_provenance() -> None:
    """Principle #2, enforced structurally rather than by convention."""
    for data in (asylum_table(), vaccination_table(), index_table()):
        answer = answer_question("ερώτηση", plan(), data)
        if answer.status is not AnswerStatus.REFUSED:
            assert answer.footer is not None
            assert answer.footer.publisher
            assert answer.footer.dataset_url


def test_missing_catalog_metadata_degrades_wording_not_the_answer() -> None:
    """get_provenance legitimately returns nulls; that must not crash the answer path."""
    data = asylum_table(publisher="", last_updated="", title="")
    answer = answer_question("ερώτηση", plan(), data)
    assert answer.footer is not None
    assert "κατάλογο" in answer.footer.publisher


def test_a_no_rows_refusal_keeps_the_provenance_it_already_built() -> None:
    """The footer is built before this branch, and ``Answer`` permits one on a refusal.

    "The right dataset, but no rows match" is substantially more useful with the dataset,
    publisher and freshness attached, and Principle #2 argues for surfacing provenance
    wherever it exists.
    """
    answer = answer_question("ερώτηση", plan(), table([("Νομός", "text")], []))

    assert answer.status is AnswerStatus.REFUSED
    assert answer.facts is None and answer.chart is None
    assert answer.footer is not None, "the footer was built at answer.py:86 and thrown away"
    assert answer.footer.publisher == "Ελληνική Κυβέρνηση"


def test_the_other_refusals_still_carry_no_footer() -> None:
    """Only the no-rows path has provenance to keep; a planning refusal never fetched."""
    answer = answer_question("ερώτηση", plan(status=PlanStatus.NO_MATCH))

    assert answer.footer is None


def test_no_match_refusal_names_the_closest_candidates() -> None:
    """12/26 of the golden set ends here, so the refusal has to be useful."""
    answer = answer_question("Ποιος είναι ο πληθυσμός του Άρη;",
                             plan(status=PlanStatus.NO_MATCH))
    assert answer.status is AnswerStatus.REFUSED
    assert answer.facts is None and answer.chart is None
    assert "Σύνολο δεδομένων" in answer.text


def test_unsupported_refusal_names_publisher_and_formats() -> None:
    """A refusal that says what *is* published is still a useful answer."""
    ctx = RefusalContext(dataset_title="Πυρκαγιές", publisher="Πυροσβεστικό Σώμα",
                         offered_formats=["XLSX", "PDF"])
    answer = answer_question("ερώτηση", plan(status=PlanStatus.UNSUPPORTED), refusal_ctx=ctx)
    assert answer.status is AnswerStatus.REFUSED
    assert "Πυροσβεστικό Σώμα" in answer.text
    assert "XLSX" in answer.text


@pytest.mark.parametrize(
    ("error", "needle"),
    [
        (ResourceUnavailableError("x"), "διακομιστής"),
        (MalformedPayloadError("x"), "μορφή"),
        (UnsupportedResourceError("x"), "υποστηρίζεται"),
    ],
)
def test_access_errors_produce_distinct_refusals(error: Exception, needle: str) -> None:
    """A server failure must not read as 'the data is bad'."""
    answer = answer_question("ερώτηση", plan(), error=error)  # type: ignore[arg-type]
    assert answer.status is AnswerStatus.REFUSED
    assert needle in answer.text


def test_disjoint_period_is_refused_not_footnoted() -> None:
    """Answering with a caveat is read by everyone as the figure for the year they asked."""
    params = QueryParams(date_from="2024-01-01", date_to="2024-12-31")
    answer = answer_question("Τι έγινε το 2024;", plan(params=params), index_table(complete=True))
    assert answer.status is AnswerStatus.REFUSED
    assert "2010" in answer.text


def test_incomplete_table_is_partial_and_states_its_coverage() -> None:
    """The q14 failure: a truncated series must disclose where it actually stops."""
    answer = answer_question("Ποια η εξέλιξη του δείκτη;", plan(), index_table(complete=False))
    assert answer.status is AnswerStatus.PARTIAL
    assert answer.caveats
    assert answer.footer is not None and not answer.footer.complete


def test_untrusted_header_suppresses_column_name_claims() -> None:
    """When the banner ate the header, the labels are not the publisher's."""
    answer = answer_question("ερώτηση", plan(), asylum_table(header_trusted=False))
    assert answer.status is AnswerStatus.PARTIAL
    assert any("επικεφαλίδες" in caveat for caveat in answer.caveats)


def test_matched_plan_without_a_table_or_error_is_a_programming_error() -> None:
    """Mirrors fetch_for_plan: the impossible combination raises rather than guessing."""
    with pytest.raises(ValueError):
        answer_question("ερώτηση", plan())


def test_hallucinated_figure_is_rejected_and_the_template_used() -> None:
    """The guard's whole purpose, exercised through the orchestrator."""
    llm = FakeLLM({"answer": "Υποβλήθηκαν 999.999 αιτήματα συνολικά."})
    answer = answer_question("Πόσα αιτήματα;", plan(), asylum_table(), llm=llm)
    assert answer.narration_rejected is True
    assert "999.999" not in answer.text


def test_faithful_narration_is_kept() -> None:
    """A model that copies the placeholders is not penalised."""
    llm = FakeLLM({"answer": "Η κατηγορία {LABEL_1} καταγράφει {FACT_1} αιτήματα."})
    answer = answer_question("Πόσα αιτήματα;", plan(), asylum_table(), llm=llm)
    assert answer.narration_rejected is False
    assert "{FACT_1}" not in answer.text
    assert "40.000" in answer.text


def test_english_question_answers_in_english() -> None:
    """Output language derives from the detection label, once, in one place."""
    answer = answer_question("How many?", plan(language="en"), asylum_table())
    assert answer.language == "en"
    assert "Source:" in answer.text


def test_greeklish_question_answers_in_greek() -> None:
    """Greeklish is Greek typed on a Latin keyboard; the asker reads Greek."""
    answer = answer_question("posa aitimata;", plan(language="greeklish"), asylum_table())
    assert answer.language == "el"


def test_log_never_contains_a_cell_value(caplog: pytest.LogCaptureFixture) -> None:
    """Greek open data names individuals; the answer log must stay metadata-only."""
    with caplog.at_level("INFO"):
        answer_question("ερώτηση", plan(), asylum_table())
    combined = " ".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert "ΑΙΓΥΠΤΟΣ" not in combined
    assert "7547" not in combined
