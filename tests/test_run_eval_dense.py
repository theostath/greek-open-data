"""Tests that ``run_eval`` loads one exact ``DenseIndex`` and threads it through (spec Step 6).

Seam pinned here (beyond those in ``test_run_eval_planner.py``): ``run_eval.load_dense_index``,
called as ``load_dense_index(settings.catalog_db_path, settings.embedding_model)`` once per run,
whose result reaches ``find_dataset`` / ``make_plan`` as ``dense=``. No real model, no Ollama.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from config import Settings

from pythia.eval import run_eval
from pythia.llm import FakeLLM
from pythia.planning.models import PlanStatus, QueryParams, QueryPlan
from pythia.retrieval.search import Candidate

MODEL = "fake/e5-test"


class _Snapshot:
    """A sentinel standing in for a loaded ``DenseIndex``."""


@dataclass
class Loads:
    """The sentinel snapshot and every ``load_dense_index`` call (args + kwarg values)."""

    snapshot: _Snapshot = field(default_factory=_Snapshot)
    calls: list[tuple[Any, ...]] = field(default_factory=list)


@pytest.fixture
def loads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Loads:
    """Patch settings, connect, model and dense loading; return the dense-load record."""
    rec = Loads()
    settings = Settings(
        rerank_enabled=False, embedding_model=MODEL,
        catalog_db_path=str(tmp_path / "absent.sqlite"),
    )

    def fake_load_dense(*args: Any, **kwargs: Any) -> _Snapshot:
        """Record the load and hand back the sentinel."""
        rec.calls.append((*args, *kwargs.values()))
        return rec.snapshot

    monkeypatch.setattr(run_eval, "get_settings", lambda: settings)
    monkeypatch.setattr(run_eval, "connect", lambda *_a, **_k: sqlite3.connect(":memory:"))
    monkeypatch.setattr(run_eval, "load_model", lambda *_a, **_k: object())
    monkeypatch.setattr(run_eval, "load_dense_index", fake_load_dense)
    monkeypatch.setattr(run_eval, "load_reranker", lambda *_a, **_k: None)
    return rec


def test_retrieval_eval_passes_the_loaded_snapshot_to_find_dataset(
    loads: Loads, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One load, from the catalogue file for the configured model, reused for every question."""
    snapshot = loads.snapshot
    seen: list[dict[str, Any]] = []

    def spy(question: str, **kw: Any) -> list[Candidate]:
        """Record the kwargs; return no candidates."""
        seen.append(kw)
        return []

    monkeypatch.setattr(run_eval, "find_dataset", spy)

    assert run_eval.main([]) == 0

    assert len(loads.calls) == 1
    assert loads.calls[0][0] == run_eval.get_settings().catalog_db_path
    assert MODEL in loads.calls[0]
    assert len(seen) == len(run_eval.load_golden())
    assert all(kw["dense"] is snapshot for kw in seen)
    assert all("chroma_path" not in kw for kw in seen)


def test_planner_eval_passes_the_loaded_snapshot_to_make_plan(
    loads: Loads, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--planner`` plans every question against the same single snapshot."""
    snapshot = loads.snapshot
    seen: list[dict[str, Any]] = []

    def fake_make_plan(question: str, **kw: Any) -> QueryPlan:
        """Record the kwargs and refuse."""
        seen.append(kw)
        return QueryPlan(
            question=question, normalized_question=question, language="el",
            status=PlanStatus.NO_MATCH, dataset=None, resource_id=None, resource_format=None,
            resource_url=None, access_path=None, params=QueryParams(), confidence=0.0,
            reason="test", degraded=False, candidates=[],
        )

    monkeypatch.setattr(run_eval, "make_plan", fake_make_plan)
    monkeypatch.setattr(run_eval, "load_llm", lambda *_a, **_k: FakeLLM({}))
    monkeypatch.setattr(run_eval, "_llm_reachable", lambda _s: True)
    monkeypatch.setattr(run_eval, "_ollama_digest", lambda _s: "sha256:feedface")
    monkeypatch.setattr(run_eval, "_git_sha", lambda: "0123456789abcdef")

    run_eval.main(["--planner", "--report", str(tmp_path / "report.json")])

    assert len(loads.calls) == 1
    assert seen and all(kw["dense"] is snapshot for kw in seen)
    assert all("chroma_path" not in kw for kw in seen)
