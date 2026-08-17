"""Tests for the Phase 7 job store.

The pipeline is slow and CPU-bound, so every bound here is backpressure rather than tidiness:
without them a burst of submissions parks work behind two workers for tens of minutes while
the browser polls an empty stage. The restart-vs-expiry distinction matters because ``make
dev`` runs ``--reload``, so any file save wipes every job — routine during development.
"""

from __future__ import annotations

from typing import Any

import pytest
from config import Settings

from pythia.api.jobs import Job, JobRejected, JobStatus, JobStore, Miss


class _Inline:
    """An executor that runs work immediately, so tests never race a thread."""

    def submit(self, fn: Any, *args: Any) -> None:
        fn(*args)

    def shutdown(self, wait: bool = True) -> None:
        return None


class _Never:
    """An executor that accepts work and never runs it, leaving jobs queued."""

    def submit(self, fn: Any, *args: Any) -> None:
        return None

    def shutdown(self, wait: bool = True) -> None:
        return None


class _Clock:
    """A hand-cranked clock, so TTL behaviour is asserted rather than slept for."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _store(
    executor: Any = None, *, run: Any = None, clock: Any = None, **overrides: Any
) -> JobStore:
    """A store wired to a fake runner and a controllable clock."""
    return JobStore(
        Settings(**overrides),
        run=run or (lambda question, resource_id, on_stage: f"bundle:{question}"),
        executor=executor if executor is not None else _Inline(),
        clock=clock or _Clock(),
    )


def test_a_completed_job_carries_its_result() -> None:
    """The happy path: submit, the worker runs, the answer is retrievable by id."""
    store = _store()

    job_id = store.submit("πόσες πυρκαγιές;")
    job = store.get(job_id)

    assert job is not None
    assert job.status is JobStatus.DONE
    assert job.bundle == "bundle:πόσες πυρκαγιές;"


def test_a_job_starts_queued_before_a_worker_picks_it_up() -> None:
    """With two workers and ~2-minute answers, a third submission genuinely waits."""
    store = _store(_Never())

    job = store.get(store.submit("q"))

    assert job is not None
    assert job.status is JobStatus.QUEUED
    assert job.stage == "queued"


def test_a_worker_exception_marks_the_job_failed() -> None:
    """An escaped exception would leave the job queued and the UI polling until TTL."""
    def boom(question: str, resource_id: str | None, on_stage: Any) -> Any:
        raise RuntimeError("ollama is down")

    store = _store(run=boom)

    job = store.get(store.submit("q"))

    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.bundle is None


def test_a_failure_records_the_class_and_message_but_never_a_traceback() -> None:
    """A traceback in a browser is an information leak and tells the user nothing."""
    def boom(question: str, resource_id: str | None, on_stage: Any) -> Any:
        raise RuntimeError("ollama is down")

    store = _store(run=boom)

    job = store.get(store.submit("q"))

    assert job is not None and job.error is not None
    assert "RuntimeError" in job.error and "ollama is down" in job.error
    assert "Traceback" not in job.error and "line " not in job.error


def test_a_failure_message_is_redacted_before_it_can_be_rendered() -> None:
    """Publisher URLs carry their own credentials; §6 forbids leaking them anywhere."""
    def boom(question: str, resource_id: str | None, on_stage: Any) -> Any:
        raise RuntimeError("failed on https://host/x?token=SUPERSECRET&a=1")

    store = _store(run=boom)

    job = store.get(store.submit("q"))

    assert job is not None and job.error is not None
    assert "SUPERSECRET" not in job.error


def test_an_identical_in_flight_question_reuses_the_running_job() -> None:
    """Double-clicking must not burn one of only two inference slots."""
    store = _store(_Never())

    first = store.submit("ίδια ερώτηση")
    second = store.submit("ίδια ερώτηση")

    assert first == second


def test_a_different_resource_id_is_a_different_job() -> None:
    """Dedupe is on the whole request, not the question text alone."""
    store = _store(_Never())

    first = store.submit("q", resource_id="res-1")
    second = store.submit("q", resource_id="res-2")

    assert first != second


def test_a_finished_job_is_not_reused_for_a_new_submission() -> None:
    """Re-asking must re-run: the catalogue and the upstream file both move."""
    store = _store()

    first = store.submit("q")
    second = store.submit("q")

    assert first != second


def test_submit_is_rejected_once_pending_jobs_reach_the_ceiling() -> None:
    """A ThreadPoolExecutor queue is unbounded; this is the backpressure that bounds it."""
    store = _store(_Never(), api_max_pending_jobs=2)

    store.submit("q1")
    store.submit("q2")

    with pytest.raises(JobRejected):
        store.submit("q3")


def test_submit_is_rejected_once_the_store_reaches_its_hard_ceiling() -> None:
    """Storage is bounded separately from pending work; finished jobs still occupy it."""
    store = _store(api_max_jobs=2)

    store.submit("q1")
    store.submit("q2")

    with pytest.raises(JobRejected):
        store.submit("q3")


def test_finished_jobs_are_evicted_once_past_the_ttl() -> None:
    """The store is in-memory and unbounded growth is the failure mode it must not have."""
    clock = _Clock()
    store = _store(clock=clock, api_job_ttl_s=10)
    job_id = store.submit("q")

    clock.advance(11)
    store.submit("other")

    assert store.get(job_id) is None


def test_eviction_never_removes_a_job_that_is_still_running() -> None:
    """TTL applies to results, not to work; evicting a running job orphans a worker."""
    clock = _Clock()
    store = _store(_Never(), clock=clock, api_job_ttl_s=10)
    job_id = store.submit("q")

    clock.advance(1000)
    store.submit("other")

    assert store.get(job_id) is not None


def test_an_id_from_an_earlier_process_reports_a_restart_not_an_expiry() -> None:
    """``make dev`` reloads on every save; "your result expired" would be a lie."""
    old = _store()
    stale_id = old.submit("q")
    fresh = _store()

    assert fresh.get(stale_id) is None
    assert fresh.miss_reason(stale_id) is Miss.RESTARTED


def test_an_evicted_id_from_this_process_reports_an_expiry() -> None:
    """Same process, so the result really did age out — a different message."""
    clock = _Clock()
    store = _store(clock=clock, api_job_ttl_s=10)
    job_id = store.submit("q")
    clock.advance(11)
    store.submit("other")

    assert store.miss_reason(job_id) is Miss.EXPIRED


def test_an_unparseable_id_is_neither_a_restart_nor_an_expiry() -> None:
    """A hand-typed URL must not be reported as a lost result."""
    store = _store()

    assert store.miss_reason("not-a-job-id") is Miss.UNKNOWN


def test_the_stage_callback_is_recorded_on_the_job() -> None:
    """The progress fragment renders this; an unwired callback shows a permanent spinner."""
    seen: list[Job] = []

    def run(question: str, resource_id: str | None, on_stage: Any) -> Any:
        on_stage("planning")
        on_stage("fetching")
        return "bundle"

    store = _store(run=run)
    job_id = store.submit("q")
    job = store.get(job_id)
    seen.append(job)  # type: ignore[arg-type]

    assert job is not None
    assert job.stage == "fetching"
