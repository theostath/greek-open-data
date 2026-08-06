"""Shared harness for the Phase 7 route tests.

Route tests run with fakes, never the real pipeline: no model load, no Ollama, no network, so
the suite stays runnable offline under ``HF_HUB_OFFLINE=1``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from config import Settings
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.synthesis_fixtures import asylum_table, plan, table

from pythia.api.app import create_app, get_jobs, get_pipeline, get_settings_dep
from pythia.api.jobs import JobStore
from pythia.api.service import AnswerBundle, NearMiss, RecoveryContext
from pythia.planning.models import PlanStatus
from pythia.synthesis.answer import answer_question
from pythia.synthesis.models import Answer

ORIGIN = "http://127.0.0.1:8000"


class Inline:
    """Runs submitted work immediately, so a test never races a worker thread."""

    def submit(self, fn: Any, *args: Any) -> None:
        fn(*args)

    def shutdown(self, wait: bool = True) -> None:
        return None


class Never:
    """Accepts work and never runs it, so a job stays observable in its queued state."""

    def submit(self, fn: Any, *args: Any) -> None:
        return None

    def shutdown(self, wait: bool = True) -> None:
        return None


def answered() -> AnswerBundle:
    """A real ANSWERED/PARTIAL answer with facts, a chart and provenance."""
    answer = answer_question("Πόσα αιτήματα ασύλου;", plan(), asylum_table())
    return AnswerBundle(answer=answer, recovery=RecoveryContext())


def no_match() -> AnswerBundle:
    """The first refusal shape, carrying near misses to offer as a next move."""
    answer = answer_question("ερώτηση", plan(status=PlanStatus.NO_MATCH))
    return AnswerBundle(answer=answer, recovery=RecoveryContext(
        near_misses=[NearMiss(title="Πυρκαγιές δασών",
                              url="https://data.gov.gr/dataset/ds-fires")],
        normalized_question="πυρκαγιές",
    ))


def unsupported() -> AnswerBundle:
    """The second refusal shape: the dataset exists but publishes nothing tabular."""
    answer = answer_question("ερώτηση", plan(status=PlanStatus.UNSUPPORTED))
    return AnswerBundle(answer=answer, recovery=RecoveryContext(
        offered_formats=["PDF", "XLSX"],
    ))


def matched_but_refused() -> AnswerBundle:
    """The third shape, from the ELSTAT case: right dataset, requested slice absent."""
    answer = answer_question("ερώτηση", plan(), table([("Νομός", "text")], []))
    return AnswerBundle(answer=answer, recovery=RecoveryContext(
        matched_but_refused=True,
        matched_title="Δείκτης τιμών ΕΛΣΤΑΤ",
        matched_last_updated="2016-06-01T00:00:00",
        near_misses=[NearMiss(title="ΛΑΘΟΣ — δεν πρέπει να εμφανιστεί",
                              url="https://data.gov.gr/dataset/ds-wrong")],
    ))


def bundle_of(answer: Answer, recovery: RecoveryContext | None = None) -> AnswerBundle:
    """Wrap an arbitrary answer for a store fixture."""
    return AnswerBundle(answer=answer, recovery=recovery or RecoveryContext())


def build(
    bundle: AnswerBundle | None = None, *, executor: Any = None, **overrides: Any
) -> tuple[TestClient, JobStore]:
    """A TestClient over the real app with a fake job store wired in."""
    settings = Settings(**overrides)
    store = JobStore(
        settings,
        run=lambda question, resource_id, on_stage: bundle,
        executor=executor if executor is not None else Inline(),
    )

    @asynccontextmanager
    async def fake_lifespan(app: FastAPI) -> Any:
        """No heavy resources. State is set here *and* the dependencies are overridden,
        because TestClient only runs lifespan when used as a context manager."""
        app.state.settings = settings
        app.state.pipeline = None
        app.state.jobs = store
        yield

    app = create_app(lifespan_handler=fake_lifespan)
    app.dependency_overrides[get_settings_dep] = lambda: settings
    app.dependency_overrides[get_jobs] = lambda: store
    app.dependency_overrides[get_pipeline] = lambda: None
    return TestClient(app), store
