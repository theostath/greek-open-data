"""Tests for the Phase 7 recovery context — the data a refusal needs to offer a next move.

``RecoveryContext`` is resolved by ``Pipeline`` rather than inside ``synthesis/`` for the same
reason ``RefusalContext`` is: the catalog connection lives in the caller and ``synthesis/`` does
no I/O. These tests pin the three things the spec panel flagged as easy to get wrong — id-based
URLs, format resolution only where it is promised, and the third refusal shape.
"""

from __future__ import annotations

import sqlite3

import pytest
from config import Settings
from tests import synthesis_fixtures as fx

from pythia.api.service import Pipeline, build_recovery_context
from pythia.ingest import db
from pythia.ingest.models import DatasetRow, ResourceRow
from pythia.planning.models import PlanStatus, QueryPlan
from pythia.retrieval.search import Candidate
from pythia.synthesis import footer as footer_mod
from pythia.synthesis.models import Answer, AnswerStatus


def _dataset(id_: str, name: str) -> DatasetRow:
    """A dataset row; ``name`` is the CKAN slug, which upstream does NOT keep unique."""
    return DatasetRow(
        id=id_, name=name, title=f"Τίτλος {id_}", title_en=None, notes=None, notes_en=None,
        org_name=None, org_title="Φορέας", license_id=None, license_title=None, frequency=None,
        language_options=[], theme=[], num_resources=1, tags=[], temporal_start=None,
        temporal_end=None, spatial_text=None, metadata_created=None,
        last_updated="2026-01-01T00:00:00", state="active", harvested_at="2026-06-01T00:00:00",
        embed_text="x",
    )


def _resource(dataset_id: str, fmt: str) -> ResourceRow:
    """One resource of a given declared format."""
    return ResourceRow(
        id=f"res-{dataset_id}-{fmt}", dataset_id=dataset_id, name="r", description=None,
        format=fmt, mimetype=None, url="https://example.org/r", size=1, datastore_active=False,
        position=0, last_modified=None, metadata_modified=None, state="active", is_tabular=True,
    )


def _catalog(*rows: tuple[str, str, list[str]]) -> sqlite3.Connection:
    """In-memory catalog from (dataset_id, slug, formats) triples."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    for dataset_id, slug, formats in rows:
        db.upsert_dataset(conn, _dataset(dataset_id, slug))
        db.upsert_resources(conn, [_resource(dataset_id, fmt) for fmt in formats])
    conn.commit()
    return conn


def _plan(status: PlanStatus, candidates: list[Candidate], *, degraded: bool = False) -> QueryPlan:
    """A plan in a terminal state carrying a chosen shortlist."""
    return QueryPlan(
        question="ερώτηση", normalized_question="ερώτηση", language="el", status=status,
        dataset=candidates[0] if candidates else None,
        resource_id="res-1" if status is PlanStatus.MATCHED else None,
        resource_format="CSV" if status is PlanStatus.MATCHED else None,
        resource_url=None, access_path=None, params=fx.plan().params, confidence=0.5,
        reason="test", degraded=degraded, candidates=candidates,
    )


def _candidate(id_: str, name: str, title: str | None = "Τίτλος") -> Candidate:
    """A ranked candidate; ``title`` is nullable upstream."""
    return Candidate(id=id_, name=name, title=title, last_updated=None, rank=1, score=0.1)


def _refused(plan: QueryPlan) -> Answer:
    """A refusal carrying the plan, which is what the recovery context reads."""
    return Answer(
        question=plan.question, language="el", status=AnswerStatus.REFUSED, text="όχι",
        plan=plan, refusal_reason="όχι",
    )


def _answered(plan: QueryPlan) -> Answer:
    """A non-refused answer, which needs provenance to exist at all."""
    foot = footer_mod.build(fx.table([("a", "text")], [{"a": "1"}]))
    return Answer(
        question=plan.question, language="el", status=AnswerStatus.ANSWERED, text="ναι",
        plan=plan, footer=foot,
    )


def test_near_miss_urls_are_built_from_dataset_id_not_slug() -> None:
    """Two datasets share a slug upstream; linking by slug would cite the wrong one."""
    conn = _catalog(("ds-a", "collide", []), ("ds-b", "collide", []))
    plan = _plan(
        PlanStatus.NO_MATCH, [_candidate("ds-a", "collide"), _candidate("ds-b", "collide")]
    )

    ctx = build_recovery_context(plan, _refused(plan), conn=conn, settings=Settings())

    urls = [near.url for near in ctx.near_misses]
    assert urls == [
        "https://data.gov.gr/dataset/ds-a",
        "https://data.gov.gr/dataset/ds-b",
    ], "a colliding slug would make these two URLs identical"


def test_near_miss_title_falls_back_to_the_slug_when_upstream_has_none() -> None:
    """``Candidate.title`` is nullable, and a blank link label is not a next move."""
    conn = _catalog(("ds-a", "my-slug", []))
    plan = _plan(PlanStatus.NO_MATCH, [_candidate("ds-a", "my-slug", title=None)])

    ctx = build_recovery_context(plan, _refused(plan), conn=conn, settings=Settings())

    assert ctx.near_misses[0].title == "my-slug"


def test_near_misses_are_capped_at_five() -> None:
    """The shortlist is the full ranked list; the refusal screen shows a readable head of it."""
    conn = _catalog(*[(f"ds-{i}", f"slug-{i}", []) for i in range(8)])
    plan = _plan(
        PlanStatus.NO_MATCH, [_candidate(f"ds-{i}", f"slug-{i}") for i in range(8)]
    )

    ctx = build_recovery_context(plan, _refused(plan), conn=conn, settings=Settings())

    assert len(ctx.near_misses) == 5


def test_a_matched_plan_yields_no_near_misses() -> None:
    """Blocker 3: retrieval succeeded here, so near-miss framing states the opposite of truth."""
    conn = _catalog(("ds-a", "slug", ["CSV"]))
    plan = _plan(PlanStatus.MATCHED, [_candidate("ds-a", "slug")])

    ctx = build_recovery_context(plan, _refused(plan), conn=conn, settings=Settings())

    assert ctx.near_misses == []


def test_offered_formats_are_resolved_only_for_an_unsupported_plan() -> None:
    """The UNSUPPORTED refusal promises to name what the catalogue does list."""
    conn = _catalog(("ds-a", "slug", ["PDF", "XLSX"]))
    plan = _plan(PlanStatus.UNSUPPORTED, [_candidate("ds-a", "slug")])

    ctx = build_recovery_context(plan, _refused(plan), conn=conn, settings=Settings())

    assert ctx.offered_formats == ["PDF", "XLSX"]


def test_offered_formats_are_empty_for_a_no_match_plan() -> None:
    """No dataset was chosen, so there is no dataset whose formats could be listed."""
    conn = _catalog(("ds-a", "slug", ["PDF"]))
    plan = _plan(PlanStatus.NO_MATCH, [_candidate("ds-a", "slug")])

    ctx = build_recovery_context(plan, _refused(plan), conn=conn, settings=Settings())

    assert ctx.offered_formats == []


def test_matched_but_refused_is_the_third_refusal_shape() -> None:
    """A MATCHED plan that still refuses: the right dataset, the wrong slice of it."""
    conn = _catalog(("ds-a", "slug", ["CSV"]))
    plan = _plan(PlanStatus.MATCHED, [_candidate("ds-a", "slug", title="Δείκτης ΕΛΣΤΑΤ")])

    ctx = build_recovery_context(plan, _refused(plan), conn=conn, settings=Settings())

    assert ctx.matched_but_refused is True
    assert ctx.matched_title == "Δείκτης ΕΛΣΤΑΤ"


def test_matched_but_refused_is_false_when_the_answer_succeeded() -> None:
    """It marks a refusal shape, not merely a matched plan."""
    conn = _catalog(("ds-a", "slug", ["CSV"]))
    plan = _plan(PlanStatus.MATCHED, [_candidate("ds-a", "slug")])

    ctx = build_recovery_context(plan, _answered(plan), conn=conn, settings=Settings())

    assert ctx.matched_but_refused is False


def test_matched_but_refused_is_false_when_planning_refused() -> None:
    """NO_MATCH is the first refusal shape and must not claim the third one's wording."""
    conn = _catalog(("ds-a", "slug", []))
    plan = _plan(PlanStatus.NO_MATCH, [_candidate("ds-a", "slug")])

    ctx = build_recovery_context(plan, _refused(plan), conn=conn, settings=Settings())

    assert ctx.matched_but_refused is False


def test_planning_degraded_is_captured_separately_from_the_answer() -> None:
    """``Answer.degraded`` ORs narration and planning degradation; the wording differs."""
    conn = _catalog(("ds-a", "slug", []))
    plan = _plan(PlanStatus.NO_MATCH, [_candidate("ds-a", "slug")], degraded=True)

    ctx = build_recovery_context(plan, _refused(plan), conn=conn, settings=Settings())

    assert ctx.planning_degraded is True


def test_the_embedding_model_is_not_loaded_until_something_needs_it() -> None:
    """``--resource-id`` bypasses retrieval, and paying ~2.2 GB to answer it is a regression."""
    calls: list[int] = []

    pipeline = Pipeline(Settings(), model_loader=lambda: calls.append(1) or "model")

    assert calls == [], "constructing a Pipeline must not load the model"
    assert pipeline.model == "model"
    assert pipeline.model == "model"
    assert calls == [1], "the model must be loaded once and cached"


def test_warm_forces_the_embedding_load_up_front() -> None:
    """The web app preloads at startup so the first question does not pay the load."""
    calls: list[int] = []
    pipeline = Pipeline(Settings(), model_loader=lambda: calls.append(1) or "model")

    pipeline.warm()

    assert calls == [1]


def test_a_missing_resource_id_raises_ValueError_not_SystemExit() -> None:
    """A web handler must not be able to kill the process by asking for a bad resource."""
    conn = _catalog(("ds-a", "slug", ["CSV"]))
    pipeline = Pipeline(Settings())

    with pytest.raises(ValueError, match="no such resource"):
        pipeline._run("q", "nope", conn, conn, None, None)


def test_the_normalized_question_is_carried_for_the_greeklish_case() -> None:
    """ADR-0005 transliterates greeklish; showing the result explains what was searched."""
    conn = _catalog(("ds-a", "slug", []))
    plan = _plan(PlanStatus.NO_MATCH, [_candidate("ds-a", "slug")])
    plan = QueryPlan(**{**plan.__dict__, "normalized_question": "πυρκαγιές"})

    ctx = build_recovery_context(plan, _refused(plan), conn=conn, settings=Settings())

    assert ctx.normalized_question == "πυρκαγιές"
