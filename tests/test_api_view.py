"""Tests for the publish whitelist.

``Answer.plan`` carries the ranked retrieval shortlist with RRF scores and its own docstring
says it is kept server-side. ``view.py`` is the single place that decides what a template may
read, so anything added to ``QueryPlan`` later is invisible by default rather than by review.
"""

from __future__ import annotations

import dataclasses

from config import Settings
from tests.synthesis_fixtures import asylum_table, plan, table

from pythia.api.service import NearMiss, RecoveryContext
from pythia.api.view import AnswerView, RefusalShape, to_view
from pythia.planning.models import PlanStatus
from pythia.synthesis import footer as footer_mod
from pythia.synthesis.answer import answer_question
from pythia.synthesis.models import Answer, AnswerStatus  # noqa: F401  (Answer types _view)


def _view(answer: Answer, recovery: RecoveryContext | None = None) -> AnswerView:
    """Map an answer through the whitelist with default settings."""
    return to_view(answer, recovery or RecoveryContext(), settings=Settings())


def test_an_answered_view_carries_every_footer_field_a_citation_needs() -> None:
    """Principle #2 at the render layer: the template cannot cite what it was not given."""
    answer = answer_question("Πόσα αιτήματα ασύλου;", plan(), asylum_table())

    view = _view(answer)

    assert view.footer is not None
    assert view.footer.dataset_title and view.footer.publisher
    assert view.footer.last_updated and view.footer.dataset_url
    assert view.footer.row_coverage and view.footer.staleness


def test_the_chart_is_the_exact_validated_dict_not_a_reserialisation() -> None:
    """validate_spec approved *that* object; rebuilding one would bypass the guard."""
    answer = answer_question("Πόσα αιτήματα ασύλου;", plan(), asylum_table())
    assert answer.chart is not None

    view = _view(answer)

    assert view.chart is answer.chart.option, "must be the same object, not a copy"


def test_no_chart_is_a_legitimate_view_state() -> None:
    """A fake chart is worse than none; the template must be able to tell."""
    answer = answer_question("ερώτηση", plan(), table([("Νομός", "text")], [{"Νομός": "Α"}]))

    view = _view(answer)

    assert view.chart is None


def test_the_plan_is_not_reachable_from_the_view() -> None:
    """The whitelist is the boundary; a template must not be able to walk back to it."""
    answer = answer_question("Πόσα αιτήματα ασύλου;", plan(), asylum_table())

    view = _view(answer)

    published = {field.name for field in dataclasses.fields(view)}
    assert "plan" not in published
    assert "candidates" not in published
    assert not hasattr(view, "plan")


def test_retrieval_scores_and_ranks_never_reach_the_view() -> None:
    """Scores are an internal ranking signal and mean nothing to a journalist."""
    answer = answer_question("ερώτηση", plan(status=PlanStatus.NO_MATCH))
    recovery = RecoveryContext(
        near_misses=[NearMiss(title="Πυρκαγιές", url="https://data.gov.gr/dataset/ds-1")]
    )

    rendered = repr(_view(answer, recovery))

    for leaked in ("score", "rank", "confidence", "rrf"):
        assert leaked not in rendered.lower(), f"{leaked!r} leaked into the view"


def test_a_no_match_refusal_is_shaped_as_no_match() -> None:
    """The first refusal shape: nothing was chosen, so offer what was looked at."""
    answer = answer_question("ερώτηση", plan(status=PlanStatus.NO_MATCH))
    recovery = RecoveryContext(
        near_misses=[NearMiss(title="Πυρκαγιές", url="https://data.gov.gr/dataset/ds-1")],
        normalized_question="πυρκαγιές",
    )

    view = _view(answer, recovery)

    assert view.refusal is not None
    assert view.refusal.shape is RefusalShape.NO_MATCH
    assert view.refusal.near_misses[0].title == "Πυρκαγιές"
    assert view.refusal.normalized_question == "πυρκαγιές"


def test_an_unsupported_refusal_names_the_formats_the_catalogue_lists() -> None:
    """The second shape: the dataset exists but publishes nothing tabular."""
    answer = answer_question("ερώτηση", plan(status=PlanStatus.UNSUPPORTED))
    recovery = RecoveryContext(offered_formats=["PDF", "XLSX"])

    view = _view(answer, recovery)

    assert view.refusal is not None
    assert view.refusal.shape is RefusalShape.UNSUPPORTED
    assert view.refusal.offered_formats == ["PDF", "XLSX"]


def test_a_matched_plan_refusal_is_the_third_shape_and_shows_no_near_misses() -> None:
    """Retrieval and planning both succeeded; near-miss framing would invert the truth."""
    answer = answer_question(
        "ερώτηση", plan(), table([("Νομός", "text")], []),
    )
    # Candidates deliberately present: the view layer must drop them on its own, so that
    # neither it nor build_recovery_context can reintroduce the defect single-handed.
    recovery = RecoveryContext(
        matched_but_refused=True, matched_title="Δείκτης ΕΛΣΤΑΤ",
        near_misses=[NearMiss(title="Κάτι άλλο", url="https://data.gov.gr/dataset/ds-9")],
    )

    view = _view(answer, recovery)

    assert view.refusal is not None
    assert view.refusal.shape is RefusalShape.MATCHED_BUT_REFUSED
    assert view.refusal.matched_title == "Δείκτης ΕΛΣΤΑΤ"
    assert view.refusal.near_misses == []


def test_a_matched_plan_refusal_keeps_the_provenance_it_has() -> None:
    """The Phase 6 amendment only helps if the whitelist forwards it."""
    answer = answer_question("ερώτηση", plan(), table([("Νομός", "text")], []))
    view = _view(answer, RecoveryContext(matched_but_refused=True))

    assert view.footer is not None
    assert view.footer.publisher == "Ελληνική Κυβέρνηση"


def test_narration_and_planning_degradation_are_reported_separately() -> None:
    """``Answer.degraded`` ORs the two, and they need different sentences."""
    answer = answer_question("Πόσα αιτήματα ασύλου;", plan(), asylum_table())

    view = _view(answer, RecoveryContext(planning_degraded=False))

    assert view.narration_degraded is True, "no LLM was passed, so prose is templated"
    assert view.planning_degraded is False


def test_facts_are_published_as_preformatted_strings() -> None:
    """footer.format_number is canonical; a template must not reformat a figure itself."""
    answer = answer_question("Πόσα αιτήματα ασύλου;", plan(), asylum_table())

    view = _view(answer)

    assert view.facts, "an answered view states at least one figure"
    assert all(isinstance(fact.value, str) for fact in view.facts)
    assert any("." in fact.value or "," in fact.value for fact in view.facts)


def test_a_refusal_view_states_why() -> None:
    """A refusal without its reason is an error page, which is the thing to avoid."""
    answer = answer_question("ερώτηση", plan(status=PlanStatus.NO_MATCH))

    view = _view(answer)

    assert view.status is AnswerStatus.REFUSED
    assert view.refusal is not None and view.refusal.reason
    assert view.facts == [] and view.chart is None


def test_the_footer_figure_formatter_matches_the_canonical_one() -> None:
    """The guard normalises figures the same way; the two must not disagree."""
    assert footer_mod.format_number(1234567, "el") == "1.234.567"
    assert footer_mod.format_number(1234567, "en") == "1,234,567"
