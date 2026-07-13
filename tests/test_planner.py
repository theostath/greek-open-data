"""Tests for the Phase 4 planner orchestrator (retrieval stubbed, LLM faked)."""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import date

import pytest
from config import Settings

from pythia.ingest import db
from pythia.ingest.models import DatasetRow, ResourceRow
from pythia.llm import FakeLLM, LLMError
from pythia.planning import planner as planner_module
from pythia.planning.models import PlanStatus, QueryPlan
from pythia.planning.planner import make_plan
from pythia.retrieval.search import Candidate

_HIGH_SCORE = 2.0 / 60  # tops both retrieval arms -> confidence ~1.0
_LOW_SCORE = 0.0001  # confidence well below the default 0.15 floor


def _dataset(id_: str) -> DatasetRow:
    """Build a dataset row with metadata the planner reads for the prompt."""
    return DatasetRow(
        id=id_, name=f"name-{id_}", title="Τροχαία ατυχήματα", title_en="Road accidents",
        notes="Στοιχεία τροχαίων", notes_en="Accident data", org_name=None, org_title=None,
        license_id=None, license_title=None, frequency=None, language_options=[], theme=[],
        num_resources=1, tags=["accidents"], temporal_start=None, temporal_end=None,
        spatial_text=None, metadata_created=None, last_updated="2026-01-01T00:00:00",
        state="active", harvested_at="2026-06-01T00:00:00", embed_text="x",
    )


def _resource(dataset_id: str, fmt: str = "CSV") -> ResourceRow:
    """Build one resource for the dataset."""
    return ResourceRow(
        id=f"res-{dataset_id}", dataset_id=dataset_id, name="data", description=None,
        format=fmt, mimetype=None, url=f"https://data.gov.gr/download/{dataset_id}.csv",
        size=10, datastore_active=False, position=0, last_modified=None,
        metadata_modified=None, state="active", is_tabular=True,
    )


def _catalog(resource_format: str | None = "CSV") -> sqlite3.Connection:
    """In-memory catalog with one dataset and (optionally) one resource of a given format."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.upsert_dataset(conn, _dataset("ds1"))
    if resource_format is not None:
        db.upsert_resources(conn, [_resource("ds1", resource_format)])
    conn.commit()
    return conn


def _stub_retrieval(monkeypatch: pytest.MonkeyPatch, candidates: list[Candidate]) -> None:
    """Replace find_dataset in the planner namespace with a canned result."""
    monkeypatch.setattr(planner_module, "find_dataset", lambda *a, **k: list(candidates))


def _candidate(score: float, id_: str = "ds1") -> Candidate:
    """A hydrated candidate with a chosen fusion score (find_dataset carries provenance)."""
    return Candidate(
        id=id_, name=f"name-{id_}", title="t", last_updated="2026-01-01T00:00:00",
        rank=0, score=score,
    )


def _plan(conn: sqlite3.Connection, llm: FakeLLM, **kw: object) -> QueryPlan:
    """Call make_plan with test defaults (no real model/chroma needed once stubbed)."""
    return make_plan(
        "Πόσα τροχαία ατυχήματα;", conn=conn, model=None, chroma_path="", llm=llm,  # type: ignore[arg-type]
        settings=Settings(), **kw,  # type: ignore[arg-type]
    )


def test_matched_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relevant=true with a CSV resource yields MATCHED with validated params + provenance."""
    _stub_retrieval(monkeypatch, [_candidate(_HIGH_SCORE)])
    llm = FakeLLM({"relevant": True, "reason": "ok", "params": {"group_by": "year"}})
    plan = _plan(_catalog(), llm)
    assert plan.status is PlanStatus.MATCHED
    assert plan.resource_id == "res-ds1"
    assert plan.resource_format == "CSV"
    assert plan.access_path == "download"
    assert plan.params.group_by == "year"
    assert plan.dataset is not None and plan.dataset.last_updated == "2026-01-01T00:00:00"
    assert plan.degraded is False


def test_relevance_false_is_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relevant=false is a grounded refusal."""
    _stub_retrieval(monkeypatch, [_candidate(_HIGH_SCORE)])
    plan = _plan(_catalog(), FakeLLM({"relevant": False, "reason": "off-topic"}))
    assert plan.status is PlanStatus.NO_MATCH
    assert plan.resource_id is None


def test_empty_retrieval_is_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """No candidates yields NO_MATCH with zero confidence and no LLM call."""
    _stub_retrieval(monkeypatch, [])
    llm = FakeLLM({"relevant": True})
    plan = _plan(_catalog(), llm)
    assert plan.status is PlanStatus.NO_MATCH
    assert plan.confidence == 0.0
    assert llm.calls == []


def test_short_question_is_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """A too-short question short-circuits before retrieval and the LLM."""
    _stub_retrieval(monkeypatch, [_candidate(_HIGH_SCORE)])
    llm = FakeLLM({"relevant": True})
    plan = make_plan(
        "?", conn=_catalog(), model=None, chroma_path="", llm=llm,  # type: ignore[arg-type]
        settings=Settings(),
    )
    assert plan.status is PlanStatus.NO_MATCH
    assert llm.calls == []


def test_no_tabular_resource_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A matched dataset with only XLSX resources is UNSUPPORTED, not MATCHED."""
    _stub_retrieval(monkeypatch, [_candidate(_HIGH_SCORE)])
    llm = FakeLLM({"relevant": True})
    plan = _plan(_catalog("XLSX"), llm)
    assert plan.status is PlanStatus.UNSUPPORTED
    assert plan.dataset is not None
    assert plan.resource_id is None
    assert llm.calls == []  # decided before the LLM call


def test_llm_error_degrades_to_score_floor_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM failure with strong retrieval degrades to MATCHED via the score floor."""
    _stub_retrieval(monkeypatch, [_candidate(_HIGH_SCORE)])
    plan = _plan(_catalog(), FakeLLM(error=LLMError("ollama down")))
    assert plan.status is PlanStatus.MATCHED
    assert plan.degraded is True


def test_llm_error_below_floor_is_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM failure with weak retrieval degrades to NO_MATCH (below the floor)."""
    _stub_retrieval(monkeypatch, [_candidate(_LOW_SCORE)])
    plan = _plan(_catalog(), FakeLLM(error=LLMError("ollama down")))
    assert plan.status is PlanStatus.NO_MATCH
    assert plan.degraded is True


def test_missing_relevant_key_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """A response lacking a boolean 'relevant' is treated as degraded."""
    _stub_retrieval(monkeypatch, [_candidate(_HIGH_SCORE)])
    plan = _plan(_catalog(), FakeLLM({"reason": "no verdict"}))
    assert plan.degraded is True


def test_invalid_params_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad dates/aggregation/limit are validated away; a reversed range is nulled."""
    _stub_retrieval(monkeypatch, [_candidate(_HIGH_SCORE)])
    llm = FakeLLM({
        "relevant": True,
        "params": {
            "date_from": "2024-12-31", "date_to": "2024-01-01",  # reversed -> both dropped
            "aggregation": "median",  # not allowed -> None
            "limit": 10**9,  # clamped to the configured max
            "metrics": ["fires", ""],  # empty dropped
        },
    })
    plan = _plan(_catalog(), llm)
    assert plan.params.date_from is None and plan.params.date_to is None
    assert plan.params.aggregation is None
    assert plan.params.limit == Settings().planning_limit_max
    assert plan.params.metrics == ["fires"]


def test_relative_date_reference_is_passed_to_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The injected reference_date is rendered into the user message for the LLM."""
    _stub_retrieval(monkeypatch, [_candidate(_HIGH_SCORE)])
    llm = FakeLLM({"relevant": True, "params": {}})
    _plan(_catalog(), llm, reference_date=date(2025, 7, 1))
    user_message = llm.calls[0][1]["content"]
    assert "2025-07-01" in user_message


def test_plan_is_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """QueryPlan is an immutable value object."""
    _stub_retrieval(monkeypatch, [_candidate(_HIGH_SCORE)])
    plan = _plan(_catalog(), FakeLLM({"relevant": True, "params": {}}))
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.status = PlanStatus.NO_MATCH  # type: ignore[misc]
