"""In-memory job store for the Phase 7 interface.

The pipeline is synchronous and slow — a cold question loads an embedding model, calls Ollama
and fetches an off-portal file — so questions run on a bounded pool and the browser polls for
progress. Two ceilings are deliberately separate: ``api_max_pending_jobs`` bounds *work* (a
``ThreadPoolExecutor`` queue is unbounded by default) and ``api_max_jobs`` bounds *storage*
(TTL eviction only removes finished jobs, so it cannot relieve a queue).

Nothing here is durable. ``make dev`` runs ``--reload``, so any file save wipes every job;
each id therefore carries the process epoch, which lets a lost result say "the server
restarted" rather than the untrue "your result expired".
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import StrEnum
from threading import Lock
from typing import Any

from config import Settings

from pythia.logging_setup import get_logger, log_event, redact_secrets

LOGGER_NAME = "pythia.api.jobs"

#: Regenerated on every process start, so an id minted before a reload is recognisable.
_EPOCH_CHARS = 8
_ID_CHARS = 12

#: What the pipeline is asked to do: one question, optionally pinned to a resource, reporting
#: stages as it goes. Kept structural so tests never construct a real ``Pipeline``.
Runner = Callable[[str, str | None, Callable[[str], None]], Any]


class JobStatus(StrEnum):
    """Where a submitted question has got to."""

    QUEUED = "queued"  # accepted, no worker yet — a real state with only two workers
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Miss(StrEnum):
    """Why a job id did not resolve. The three cases need three different sentences."""

    EXPIRED = "expired"  # this process minted it; it aged out of the store
    RESTARTED = "restarted"  # a previous process minted it; nothing aged out
    UNKNOWN = "unknown"  # never a job id at all — a hand-edited URL


class JobRejected(Exception):
    """Backpressure: the store is full or too much work is already pending."""


@dataclass
class Job:
    """One submitted question and whatever is known about it so far."""

    id: str
    question: str
    resource_id: str | None
    status: JobStatus
    stage: str
    submitted_at: float
    started_at: float | None = None
    finished_at: float | None = None
    bundle: Any = None
    error: str | None = None

    @property
    def finished(self) -> bool:
        """Whether the job has reached a terminal state and may be evicted."""
        return self.status in {JobStatus.DONE, JobStatus.FAILED}


class JobStore:
    """A bounded, TTL-evicting, thread-safe map of job id to job."""

    def __init__(
        self,
        settings: Settings,
        *,
        run: Runner,
        executor: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wire the store to the work it runs, its executor and its clock."""
        self.settings = settings
        self.epoch = uuid.uuid4().hex[:_EPOCH_CHARS]
        self._run = run
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._lock = Lock()
        self._executor = executor if executor is not None else ThreadPoolExecutor(
            max_workers=settings.api_max_concurrent_jobs
        )

    def submit(self, question: str, resource_id: str | None = None) -> str:
        """Accept a question and return the id to poll, or raise ``JobRejected``."""
        with self._lock:
            self._evict_expired()

            # Dedupe only against work still in flight. A finished job is not reused: the
            # catalogue and the upstream file both move, so re-asking must re-run.
            for job in self._jobs.values():
                if (job.question, job.resource_id) == (question, resource_id) \
                        and not job.finished:
                    return job.id

            pending = sum(1 for job in self._jobs.values() if not job.finished)
            if pending >= self.settings.api_max_pending_jobs:
                raise JobRejected("too many questions are already running")
            if len(self._jobs) >= self.settings.api_max_jobs:
                raise JobRejected("the result store is full")

            job = Job(
                id=f"{self.epoch}-{uuid.uuid4().hex[:_ID_CHARS]}",
                question=question, resource_id=resource_id, status=JobStatus.QUEUED,
                stage=JobStatus.QUEUED.value, submitted_at=self._clock(),
            )
            self._jobs[job.id] = job

        self._executor.submit(self._work, job)
        return job.id

    def get(self, job_id: str) -> Job | None:
        """Return a **snapshot** of a job, or ``None`` if it is unknown or evicted.

        A copy, not the live object: a worker mutates the job under the lock, and a caller
        reading the live object field by field outside the lock could observe ``DONE`` before
        ``bundle`` was assigned and render a finished answer that has no result attached.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            return replace(job) if job is not None else None

    def miss_reason(self, job_id: str) -> Miss:
        """Explain a lookup that returned nothing, so the UI can say which thing happened."""
        prefix, _, rest = job_id.partition("-")
        if len(prefix) != _EPOCH_CHARS or len(rest) != _ID_CHARS:
            return Miss.UNKNOWN
        return Miss.EXPIRED if prefix == self.epoch else Miss.RESTARTED

    def now(self) -> float:
        """The store's clock, so callers compute elapsed time on the same time base."""
        return self._clock()

    def close(self) -> None:
        """Stop accepting work and let running jobs finish."""
        self._executor.shutdown(wait=False)

    def _work(self, job: Job) -> None:
        """Run one job. Catches **every** exception: an escape leaves the UI polling forever."""
        logger = get_logger(LOGGER_NAME)
        with self._lock:
            job.status = JobStatus.RUNNING
            job.started_at = self._clock()

        def on_stage(stage: str) -> None:
            with self._lock:
                job.stage = stage

        try:
            bundle = self._run(job.question, job.resource_id, on_stage)
        except BaseException as exc:  # noqa: BLE001 — a worker must never escape
            # Class and message only. A traceback tells the user nothing and leaks paths;
            # redaction runs before the string can reach a template, not merely a log.
            error = redact_secrets(f"{type(exc).__name__}: {exc}")
            with self._lock:
                job.status = JobStatus.FAILED
                job.error = error
                job.finished_at = self._clock()
            log_event(logger, logging.ERROR, "api.job_failed", job=job.id, error=error)
            return

        with self._lock:
            # Result before status: a reader must never see DONE without a bundle behind it.
            job.bundle = bundle
            job.finished_at = self._clock()
            job.status = JobStatus.DONE

    def _evict_expired(self) -> None:
        """Drop finished jobs past the TTL. Caller holds the lock.

        Only *finished* jobs are eligible: evicting a running one would orphan its worker and
        report a restart for a question still being answered.
        """
        ttl = self.settings.api_job_ttl_s
        now = self._clock()
        stale = [
            job_id for job_id, job in self._jobs.items()
            if job.finished and job.finished_at is not None and now - job.finished_at > ttl
        ]
        for job_id in stale:
            del self._jobs[job_id]
