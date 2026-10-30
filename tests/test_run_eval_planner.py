"""Tests for ``run_eval --planner``: the planner end-to-end gate (spec Step 2, AC6).

The contract pinned here, beyond what ``specs/spec-exact-dense-search.md`` states:

- ``run_eval.Bucket`` is a ``StrEnum`` whose values equal the member names.
- ``run_eval.bucket(plan, expected_id)`` is pure; ``expected_id=None`` means a negative question.
- ``run_eval.load_negative()`` loads ``golden_negative.yaml``; each item has ``.id``/``.question``.
- Seams monkeypatched below, all module attributes of ``pythia.eval.run_eval``:
  ``get_settings``, ``connect``, ``load_model``, ``load_reranker``, ``load_llm``, ``make_plan``,
  ``_llm_reachable(settings) -> bool``, ``_ollama_digest(settings) -> str | None``,
  ``_git_sha() -> str``.
- ``--report PATH`` names the JSON report file. Its keys: ``git_sha``, ``llm_model``,
  ``llm_digest``, ``counts`` (every ``Bucket`` value -> int, zeros included) and ``questions``
  (one row per question with ``id``, ``bucket``, ``dataset_id``).
- ``--planner --no-normalize`` is an argparse error: ``SystemExit(2)`` before any probe.
- Exit precedence: 2 (LLM unreachable) before anything; then 3 (any DEGRADED) over 1.

No test here touches Ollama, the network, ``data/`` or a real embedding model.
"""

from __future__ import annotations

import json
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

# Real ids from golden_questions.yaml: q01 -> mcp_traffic_accidents, q02 -> mcp_forest_fires.
TRAFFIC_ID = "47ed4dbf-5e14-4ad9-8edd-3cef6373694d"
FIRES_ID = "363b7676-1604-438e-a5ee-545c29d15caf"

ALL_BUCKETS = {
    "MATCHED_CORRECT",
    "MATCHED_WRONG",
    "UNSUPPORTED_CORRECT",
    "UNSUPPORTED_WRONG",
    "NO_MATCH",
    "REFUSED",
    "ANSWERED_WRONGLY",
    "DEGRADED",
}
TEST_LLM_MODEL = "qwen-test:0.5b"


def _candidate(dataset_id: str) -> Candidate:
    """Build a retrieval candidate shaped like ``search._hydrate`` output."""
    return Candidate(
        id=dataset_id, name=f"name-{dataset_id}", title="title", last_updated="2026-01-01",
        rank=1, score=2.0 / 60,
    )


def _plan(
    status: PlanStatus, dataset_id: str | None, *, degraded: bool = False,
    question: str = "q",
) -> QueryPlan:
    """Build a ``QueryPlan`` the way ``planner._plan`` does for the given outcome.

    MATCHED carries a resource; UNSUPPORTED and NO_MATCH do not. NO_MATCH may still carry the
    chosen dataset (the relevance gate refuses with ``dataset=chosen``) or none at all.
    """
    dataset = _candidate(dataset_id) if dataset_id is not None else None
    matched = status is PlanStatus.MATCHED
    return QueryPlan(
        question=question,
        normalized_question=question,
        language="el",
        status=status,
        dataset=dataset,
        resource_id="res-1" if matched else None,
        resource_format="CSV" if matched else None,
        resource_url="https://data.gov.gr/download/x.csv" if matched else None,
        access_path="download" if matched else None,
        params=QueryParams(),
        confidence=0.9 if dataset is not None else 0.0,
        reason="test",
        degraded=degraded,
        candidates=[dataset] if dataset is not None else [],
    )


# --- bucket(): golden questions -------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "dataset_id", "expected"),
    [
        (PlanStatus.MATCHED, TRAFFIC_ID, "MATCHED_CORRECT"),
        (PlanStatus.MATCHED, FIRES_ID, "MATCHED_WRONG"),
        (PlanStatus.UNSUPPORTED, TRAFFIC_ID, "UNSUPPORTED_CORRECT"),
        (PlanStatus.UNSUPPORTED, FIRES_ID, "UNSUPPORTED_WRONG"),
        (PlanStatus.NO_MATCH, None, "NO_MATCH"),
        (PlanStatus.NO_MATCH, TRAFFIC_ID, "NO_MATCH"),
        (PlanStatus.NO_MATCH, FIRES_ID, "NO_MATCH"),
    ],
)
def test_bucket_golden(status: PlanStatus, dataset_id: str | None, expected: str) -> None:
    """A golden plan is bucketed by status and by whether the chosen dataset is the expected one.

    NO_MATCH is NO_MATCH whatever dataset the relevance gate refused, right or wrong.
    """
    result = run_eval.bucket(_plan(status, dataset_id), TRAFFIC_ID)
    assert result is run_eval.Bucket[expected]


@pytest.mark.parametrize(
    ("status", "dataset_id", "expected"),
    [
        (PlanStatus.NO_MATCH, None, "REFUSED"),
        (PlanStatus.NO_MATCH, FIRES_ID, "REFUSED"),
        (PlanStatus.MATCHED, FIRES_ID, "ANSWERED_WRONGLY"),
        (PlanStatus.UNSUPPORTED, FIRES_ID, "ANSWERED_WRONGLY"),
    ],
)
def test_bucket_negative(status: PlanStatus, dataset_id: str | None, expected: str) -> None:
    """A negative question (expected_id None) is REFUSED on no_match, else ANSWERED_WRONGLY."""
    result = run_eval.bucket(_plan(status, dataset_id), None)
    assert result is run_eval.Bucket[expected]


@pytest.mark.parametrize("expected_id", [TRAFFIC_ID, None], ids=["golden", "negative"])
@pytest.mark.parametrize(
    ("status", "dataset_id"),
    [
        (PlanStatus.MATCHED, TRAFFIC_ID),
        (PlanStatus.MATCHED, FIRES_ID),
        (PlanStatus.UNSUPPORTED, TRAFFIC_ID),
        (PlanStatus.UNSUPPORTED, FIRES_ID),
        (PlanStatus.NO_MATCH, None),
        (PlanStatus.NO_MATCH, FIRES_ID),
    ],
)
def test_bucket_degraded_overrides_every_status(
    status: PlanStatus, dataset_id: str | None, expected_id: str | None
) -> None:
    """Any ``degraded=True`` plan is DEGRADED, whatever its status, dataset or question kind."""
    result = run_eval.bucket(_plan(status, dataset_id, degraded=True), expected_id)
    assert result is run_eval.Bucket.DEGRADED


def test_bucket_enum_members_and_values() -> None:
    """``Bucket`` has exactly the eight spec buckets, and each value is its member name."""
    assert {member.name for member in run_eval.Bucket} == ALL_BUCKETS
    assert all(member.value == member.name for member in run_eval.Bucket)


def test_load_negative_is_the_committed_set() -> None:
    """The negative set loads as eight questions n01..n08, disjoint from the golden set."""
    negatives = run_eval.load_negative()
    assert [q.id for q in negatives] == [f"n{i:02d}" for i in range(1, 9)]
    assert all(q.question for q in negatives)
    golden_questions = {q.question for q in run_eval.load_golden()}
    assert not golden_questions & {q.question for q in negatives}


# --- main(["--planner"]) ---------------------------------------------------------------------

Outcome = tuple[PlanStatus, str | None, bool]  # (status, chosen dataset id, degraded)


@dataclass
class PlannerHarness:
    """Patched ``run_eval`` seams plus a record of what ``main`` called."""

    report: Path
    reachable: bool = True
    outcomes: dict[str, Outcome] = field(default_factory=dict)
    planned: list[str] = field(default_factory=list)
    model_loads: int = 0
    probes: int = 0

    def run(self, *extra: str) -> int:
        """Run ``main(["--planner", "--report", <tmp>, *extra])`` and return its exit code."""
        return run_eval.main(["--planner", "--report", str(self.report), *extra])

    def read_report(self) -> dict[str, Any]:
        """Parse the JSON report ``main`` wrote."""
        data: dict[str, Any] = json.loads(self.report.read_text(encoding="utf-8"))
        return data


def _default_outcomes() -> dict[str, Outcome]:
    """Every golden question MATCHED on its expected dataset; every negative refused."""
    outcomes: dict[str, Outcome] = {
        q.question: (PlanStatus.MATCHED, q.expected_id, False) for q in run_eval.load_golden()
    }
    for q in run_eval.load_negative():
        outcomes[q.question] = (PlanStatus.NO_MATCH, None, False)
    return outcomes


def _question_text(question_id: str) -> str:
    """Return the verbatim question text for a golden (qNN) or negative (nNN) id."""
    pool = [*run_eval.load_golden(), *run_eval.load_negative()]
    return next(q.question for q in pool if q.id == question_id)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PlannerHarness:
    """Patch every heavy or networked seam of ``run_eval`` and return a recording harness."""
    h = PlannerHarness(report=tmp_path / "planner-report.json")
    h.outcomes = _default_outcomes()
    settings = Settings(
        llm_model=TEST_LLM_MODEL, rerank_enabled=False,
        catalog_db_path=str(tmp_path / "absent.sqlite"),
    )

    def fake_make_plan(question: str, **_: Any) -> QueryPlan:
        """Return the scripted plan for this verbatim question (make_plan normalises itself)."""
        assert question in h.outcomes, f"make_plan got an unscripted question: {question!r}"
        h.planned.append(question)
        status, dataset_id, degraded = h.outcomes[question]
        return _plan(status, dataset_id, degraded=degraded, question=question)

    def fake_load_model(*_: Any, **__: Any) -> object:
        """Stand in for the 2.2 GB embedding model."""
        h.model_loads += 1
        return object()

    def fake_probe(_settings: Settings) -> bool:
        """Stand in for the Ollama ``/api/tags`` probe."""
        h.probes += 1
        return h.reachable

    def forbidden(*_: Any, **__: Any) -> Any:
        """Fail if planner mode falls through to the retrieval-metrics path."""
        raise AssertionError("--planner must not run the retrieval eval")

    monkeypatch.setattr(run_eval, "get_settings", lambda: settings)
    monkeypatch.setattr(run_eval, "connect", lambda *_a, **_k: sqlite3.connect(":memory:"))
    monkeypatch.setattr(run_eval, "load_model", fake_load_model)
    monkeypatch.setattr(run_eval, "load_reranker", forbidden)
    monkeypatch.setattr(run_eval, "find_dataset", forbidden)
    monkeypatch.setattr(run_eval, "load_llm", lambda *_a, **_k: FakeLLM({}))
    monkeypatch.setattr(run_eval, "make_plan", fake_make_plan)
    monkeypatch.setattr(run_eval, "_llm_reachable", fake_probe)
    monkeypatch.setattr(run_eval, "_ollama_digest", lambda _s: "sha256:feedface")
    monkeypatch.setattr(run_eval, "_git_sha", lambda: "0123456789abcdef")
    return h


def test_planner_all_correct_exits_0_and_plans_every_question(
    harness: PlannerHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean run plans all 26 + 8 questions verbatim, prints each id, and exits 0."""
    assert harness.run() == 0
    assert sorted(harness.planned) == sorted(_default_outcomes())
    assert len(harness.planned) == 34
    out = capsys.readouterr().out
    for qid in [*(f"q{i:02d}" for i in range(1, 27)), *(f"n{i:02d}" for i in range(1, 9))]:
        assert qid in out
    assert "MATCHED_CORRECT" in out
    assert "REFUSED" in out


def test_planner_report_contents(harness: PlannerHarness) -> None:
    """The JSON report carries the SHA, LLM model, digest, all bucket counts and 34 rows."""
    assert harness.run() == 0
    report = harness.read_report()
    assert report["git_sha"] == "0123456789abcdef"
    assert report["llm_model"] == TEST_LLM_MODEL
    assert report["llm_digest"] == "sha256:feedface"
    assert set(report["counts"]) == ALL_BUCKETS
    assert report["counts"]["MATCHED_CORRECT"] == 26
    assert report["counts"]["REFUSED"] == 8
    assert sum(report["counts"].values()) == 34
    rows = {row["id"]: row for row in report["questions"]}
    assert len(rows) == 34
    assert rows["q01"]["bucket"] == "MATCHED_CORRECT"
    assert rows["q01"]["dataset_id"] == TRAFFIC_ID
    assert rows["n01"]["bucket"] == "REFUSED"
    assert rows["n01"]["dataset_id"] is None


def test_planner_matched_wrong_exits_1(
    harness: PlannerHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """One golden question matched on another dataset fails the gate with exit 1."""
    harness.outcomes[_question_text("q01")] = (PlanStatus.MATCHED, FIRES_ID, False)
    assert harness.run() == 1
    out = capsys.readouterr().out
    assert "MATCHED_WRONG" in out
    assert FIRES_ID in out  # the per-question table names the chosen dataset
    report = harness.read_report()
    assert report["counts"]["MATCHED_WRONG"] == 1
    assert {row["id"]: row["bucket"] for row in report["questions"]}["q01"] == "MATCHED_WRONG"


def test_planner_unsupported_wrong_and_answered_wrongly_do_not_fail(
    harness: PlannerHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """UNSUPPORTED_WRONG and ANSWERED_WRONGLY are baseline-judged: reported, but exit stays 0."""
    harness.outcomes[_question_text("q01")] = (PlanStatus.UNSUPPORTED, FIRES_ID, False)
    harness.outcomes[_question_text("n04")] = (PlanStatus.MATCHED, TRAFFIC_ID, False)
    harness.outcomes[_question_text("n07")] = (PlanStatus.UNSUPPORTED, FIRES_ID, False)
    assert harness.run() == 0
    out = capsys.readouterr().out
    assert "UNSUPPORTED_WRONG" in out
    assert "ANSWERED_WRONGLY" in out
    counts = harness.read_report()["counts"]
    assert counts["UNSUPPORTED_WRONG"] == 1
    assert counts["ANSWERED_WRONGLY"] == 2
    assert counts["REFUSED"] == 6


@pytest.mark.parametrize("question_id", ["q05", "n02"])
def test_planner_degraded_exits_3(harness: PlannerHarness, question_id: str) -> None:
    """Any DEGRADED plan, golden or negative, makes the run INVALID (exit 3)."""
    harness.outcomes[_question_text(question_id)] = (PlanStatus.NO_MATCH, None, True)
    assert harness.run() == 3
    report = harness.read_report()
    assert report["counts"]["DEGRADED"] == 1


def test_planner_degraded_outranks_matched_wrong(harness: PlannerHarness) -> None:
    """An invalid run is reported as INVALID (3) even when it also has a MATCHED_WRONG."""
    harness.outcomes[_question_text("q01")] = (PlanStatus.MATCHED, FIRES_ID, False)
    harness.outcomes[_question_text("q02")] = (PlanStatus.MATCHED, FIRES_ID, True)
    assert harness.run() == 3


def test_planner_llm_unreachable_exits_2_before_planning(harness: PlannerHarness) -> None:
    """With Ollama unreachable, main aborts with 2: no model load, no plan, no report."""
    harness.reachable = False
    assert harness.run() == 2
    assert harness.probes == 1
    assert harness.planned == []
    assert harness.model_loads == 0
    assert not harness.report.exists()


def test_planner_rejects_no_normalize(harness: PlannerHarness) -> None:
    """``--planner --no-normalize`` is an argparse error (SystemExit 2) before any probe."""
    with pytest.raises(SystemExit) as excinfo:
        harness.run("--no-normalize")
    assert excinfo.value.code == 2
    assert harness.probes == 0
    assert harness.planned == []


# --- existing behaviour is unchanged ---------------------------------------------------------


def test_retrieval_eval_still_default_without_planner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main([])`` still runs the retrieval eval (find_dataset per golden question), exit 0."""
    calls: list[str] = []
    settings = Settings(rerank_enabled=False, catalog_db_path=str(tmp_path / "absent.sqlite"))

    def spy_find_dataset(question: str, **_: Any) -> list[Candidate]:
        """Record the question and return no candidates."""
        calls.append(question)
        return []

    def fake_load_model(*_: Any, **__: Any) -> object:
        """Stand in for the embedding model."""
        return object()

    monkeypatch.setattr(run_eval, "get_settings", lambda: settings)
    monkeypatch.setattr(run_eval, "connect", lambda *_a, **_k: sqlite3.connect(":memory:"))
    monkeypatch.setattr(run_eval, "load_model", fake_load_model)
    monkeypatch.setattr(run_eval, "find_dataset", spy_find_dataset)

    assert run_eval.main([]) == 0
    assert len(calls) == len(run_eval.load_golden())
    assert "OVERALL" in capsys.readouterr().out


def test_pure_scoring_helpers_unchanged() -> None:
    """``reciprocal_rank`` and ``hit_at_k`` keep their contracts."""
    assert run_eval.reciprocal_rank("b", ["a", "b", "c"]) == 0.5
    assert run_eval.reciprocal_rank("z", ["a"]) == 0.0
    assert run_eval.hit_at_k("c", ["a", "b", "c"], 3) is True
    assert run_eval.hit_at_k("c", ["a", "b", "c"], 2) is False
