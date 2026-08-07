"""Shared orchestration for the CLI and the web app (Phase 7).

``Pipeline`` holds the process-lifetime resources — the embedding model, the LLM client and
one httpx client — and runs the Phase 3→6 chain for a single question. ADR-0004 records why
this is one object rather than two copies: the planner's LLM path never worked because only
tests exercised it, and a web path that duplicated the CLI's orchestration would drift the
same way.

``RecoveryContext`` is resolved *here* rather than inside ``synthesis/`` for exactly the reason
``RefusalContext`` is: the catalog connection lives in the caller and ``synthesis/`` does no
I/O by design. No Phase 6 dataclass is amended to carry it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from time import perf_counter
from typing import Any

from config import Settings, get_settings

from pythia.access.models import AccessError
from pythia.llm import LLMClient, Usage
from pythia.logging_setup import get_logger, log_event
from pythia.planning.models import PlanStatus, QueryPlan
from pythia.synthesis.answer import answer_question
from pythia.synthesis.models import Answer, AnswerStatus, RefusalContext

#: A refusal screen offers a readable head of the shortlist, not the whole ranked list.
MAX_NEAR_MISSES = 5

#: Stages the code can actually observe. ``make_plan`` performs retrieval *and* the LLM call
#: inside one callback-free function, so "planning" spans both; claiming otherwise would be
#: narration the pipeline cannot back up.
STAGES = ("queued", "planning", "fetching", "synthesising")

StageCallback = Callable[[str], None]


class _StageClock:
    """Times each stage off the same callback that drives the progress fragment.

    Measuring what the user was actually shown, rather than instrumenting separately, means
    the two cannot drift — and it costs nothing, since the callback already exists.
    """

    def __init__(self, on_stage: StageCallback | None) -> None:
        self._on_stage = on_stage
        self._marks: list[tuple[str, float]] = []

    def __call__(self, stage: str) -> None:
        """Record the transition, then forward it unchanged."""
        self._marks.append((stage, perf_counter()))
        if self._on_stage is not None:
            self._on_stage(stage)

    def finish(self) -> None:
        """Close the final stage so its duration is measurable."""
        self._marks.append(("_end", perf_counter()))

    def elapsed(self, stage: str) -> float:
        """Milliseconds spent in ``stage``, or 0.0 if it never ran."""
        for index, (name, at) in enumerate(self._marks):
            if name == stage and index + 1 < len(self._marks):
                return (self._marks[index + 1][1] - at) * 1000.0
        return 0.0


@dataclass(frozen=True)
class NearMiss:
    """One catalogue dataset that ranked but did not match, as a link the user can follow."""

    title: str
    url: str


@dataclass(frozen=True)
class RecoveryContext:
    """What a refusal needs in order to be a route rather than a dead end.

    Deliberately empty-by-default: a successful answer carries one of these too, so callers
    never branch on its presence.
    """

    near_misses: list[NearMiss] = field(default_factory=list)
    offered_formats: list[str] = field(default_factory=list)
    #: The third refusal shape: planning MATCHED, synthesis still refused. Distinct from the
    #: other two because retrieval succeeded — near-miss framing would invert the truth.
    matched_but_refused: bool = False
    matched_title: str | None = None
    matched_last_updated: str | None = None
    #: Kept apart from ``Answer.degraded``, which ORs planning and narration degradation
    #: (``answer.py`` line 115). A score-floor dataset match and templated prose are different
    #: facts about the answer and need different wording.
    planning_degraded: bool = False
    #: ADR-0005 transliterates greeklish before retrieval; showing the result is how the user
    #: learns what was actually searched for.
    normalized_question: str = ""


class RefusalShape(StrEnum):
    """Which refusal this is. Three, not two — they need three different screens.

    Defined here rather than in ``view.py`` so the render layer and the metrics store share
    one definition: a dashboard that classified refusals differently from the page the user
    saw would be worse than no dashboard.
    """

    #: Nothing in the catalogue covers the question. Offer what was looked at.
    NO_MATCH = "no_match"
    #: The dataset exists but publishes nothing tabular. Name what it does publish.
    UNSUPPORTED = "unsupported"
    #: Planning MATCHED and synthesis still refused — most often the requested period falls
    #: outside the data's observed range. Retrieval succeeded, so this dataset is NOT a near
    #: miss, and saying otherwise tells the user the opposite of what happened.
    MATCHED_BUT_REFUSED = "matched_but_refused"


def refusal_shape(answer: Answer, recovery: RecoveryContext) -> RefusalShape | None:
    """Classify a refusal, or ``None`` when the answer is not one.

    ``matched_but_refused`` wins: it is the case that would otherwise mislead.
    """
    if answer.status is not AnswerStatus.REFUSED:
        return None
    if recovery.matched_but_refused:
        return RefusalShape.MATCHED_BUT_REFUSED
    if answer.plan.status is PlanStatus.UNSUPPORTED:
        return RefusalShape.UNSUPPORTED
    return RefusalShape.NO_MATCH


@dataclass(frozen=True)
class AnswerBundle:
    """An ``Answer`` and the caller-resolved context a refusal needs to offer a next move."""

    answer: Answer
    recovery: RecoveryContext


def dataset_url(dataset_id: str, settings: Settings) -> str:
    """Build the portal URL for a dataset from its **id**, never its slug.

    CLAUDE.md §8 records two upstream slug collisions. A colliding slug would link a refusal
    to the wrong dataset — a provenance defect in a product whose premise is provenance.
    """
    return f"{settings.data_gov_gr_base_url.rstrip('/')}/dataset/{dataset_id}"


def build_recovery_context(
    plan: QueryPlan, answer: Answer, *, conn: sqlite3.Connection, settings: Settings
) -> RecoveryContext:
    """Resolve the catalogue facts a refusal screen needs, using the caller's connection."""
    matched = plan.status is PlanStatus.MATCHED
    matched_but_refused = matched and answer.status is AnswerStatus.REFUSED

    # Near misses answer "what *did* you look at?", which is only an honest question when
    # nothing was chosen. On a MATCHED plan retrieval succeeded, so listing the shortlist
    # would tell the user the opposite of what happened.
    near_misses: list[NearMiss] = []
    if plan.status is PlanStatus.NO_MATCH:
        near_misses = [
            NearMiss(title=candidate.title or candidate.name,
                     url=dataset_url(candidate.id, settings))
            for candidate in plan.candidates[:MAX_NEAR_MISSES]
        ]

    offered_formats: list[str] = []
    if plan.status is PlanStatus.UNSUPPORTED and plan.dataset is not None:
        from pythia.access import catalog

        offered_formats = catalog.get_offered_formats(conn, plan.dataset.id)

    return RecoveryContext(
        near_misses=near_misses,
        offered_formats=offered_formats,
        matched_but_refused=matched_but_refused,
        matched_title=(plan.dataset.title or plan.dataset.name)
        if matched_but_refused and plan.dataset else None,
        matched_last_updated=plan.dataset.last_updated if matched_but_refused and plan.dataset
        else None,
        planning_degraded=plan.degraded,
        normalized_question=plan.normalized_question,
    )


class Pipeline:
    """Process-lifetime resources plus the Phase 3→6 chain for one question.

    SQLite connections are **not** held on the instance: ``sqlite3`` defaults to
    ``check_same_thread=True`` and handlers run in a worker thread, so a startup-created
    connection would raise on first use. They are opened per call and closed in a ``finally``.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        model: Any = None,
        model_loader: Callable[[], Any] | None = None,
        llm: LLMClient | None = None,
        client: Any = None,
    ) -> None:
        """Assemble a pipeline from already-loaded resources (tests inject fakes here)."""
        self.settings = settings
        self.llm = llm
        self.client = client
        self._model = model
        self._model_loader = model_loader
        self._model_lock = threading.Lock()
        self._inference = threading.Semaphore(settings.api_max_concurrent_jobs)

    @property
    def model(self) -> Any:
        """The embedding model, loaded on first use.

        Lazy on purpose: the ``resource_id`` path bypasses retrieval entirely, and making it
        pay a ~2.2 GB load to answer a resource it was handed by name would be a regression
        against the CLI behaviour this class replaces.
        """
        if self._model is None and self._model_loader is not None:
            with self._model_lock:
                if self._model is None:
                    self._model = self._model_loader()
        return self._model

    def warm(self) -> None:
        """Force the embedding load now, so the first question does not pay for it."""
        _ = self.model

    @classmethod
    def create(cls, settings: Settings | None = None, *, with_llm: bool = True) -> Pipeline:
        """Load the process-lifetime resources once, mirroring ``answer.main()``."""
        import httpx

        from pythia.llm import load_llm
        from pythia.logging_setup import configure_logging
        from pythia.net import use_system_trust_store
        from pythia.retrieval.embed import load_model

        cfg = settings or get_settings()
        configure_logging()
        use_system_trust_store()  # entrypoint only: it mutates ssl process-wide
        client = httpx.Client(
            timeout=httpx.Timeout(cfg.access_read_timeout_s,
                                  connect=cfg.access_connect_timeout_s),
            follow_redirects=False,
        )
        return cls(
            cfg,
            model_loader=lambda: load_model(cfg.embedding_model),
            llm=load_llm(cfg) if with_llm else None,
            client=client,
        )

    def close(self) -> None:
        """Release the httpx client. Called from the app's lifespan shutdown."""
        if self.client is not None:
            self.client.close()

    def answer(
        self,
        question: str,
        *,
        resource_id: str | None = None,
        on_stage: StageCallback | None = None,
    ) -> AnswerBundle:
        """Plan, fetch and synthesise one question, resolving refusal context as the caller."""
        from pythia.access.cache import connect_cache, init_cache_db
        from pythia.access.transport import HttpxTransport
        from pythia.ingest.db import connect

        cfg = self.settings
        conn = connect(cfg.catalog_db_path)
        cache_conn = connect_cache(cfg.cache_db_path)
        # Clear any usage left on this thread by a previous question, so the row we write
        # counts this question's tokens and only this question's.
        if self.llm is not None:
            self.llm.drain_usage()
        stages = _StageClock(on_stage)
        started = perf_counter()
        try:
            init_cache_db(cache_conn)
            cache_conn.execute("PRAGMA journal_mode=WAL")
            transport = HttpxTransport(
                self.client, max_redirects=cfg.access_max_redirects,
                attempts=cfg.access_retry_attempts,
                min_throughput_bps=cfg.access_min_throughput_bps,
                host_min_interval_s=cfg.access_host_min_interval_s,
            )
            with self._inference:
                bundle = self._run(question, resource_id, conn, cache_conn, transport, stages)
            stages.finish()
        finally:
            conn.close()
            cache_conn.close()

        self._record(question, bundle, resource_id, stages, perf_counter() - started)
        return bundle

    def _record(
        self, question: str, bundle: AnswerBundle, resource_id: str | None,
        stages: _StageClock, elapsed_s: float,
    ) -> None:
        """Write one metrics row. Never raises: counting must not fail a finished answer."""
        cfg = self.settings
        if not cfg.metrics_enabled:
            return
        try:
            from pythia.api import metrics

            answer, plan = bundle.answer, bundle.answer.plan
            usage = self.llm.drain_usage() if self.llm is not None else Usage()
            table = answer.footer
            shape = refusal_shape(answer, bundle.recovery)
            conn = metrics.connect(cfg.metrics_db_path)
            try:
                metrics.init_db(conn)
                metrics.record(conn, metrics.AnswerMetric(
                    asked_at=metrics.utc_now(),
                    # Length, never the text: the question is user content (§6).
                    question_chars=len(question),
                    language=plan.language,
                    pinned=resource_id is not None,
                    plan_status=plan.status.value,
                    dataset_id=plan.dataset.id if plan.dataset else None,
                    confidence=plan.confidence,
                    plan_degraded=plan.degraded,
                    answer_status=answer.status.value,
                    refusal_shape=shape.value if shape else None,
                    narration_rejected=answer.narration_rejected,
                    caveats=len(answer.caveats),
                    row_count=None,
                    complete=table.complete if table else None,
                    from_cache=None,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    llm_calls=usage.calls,
                    llm_ms=usage.total_ms,
                    plan_ms=stages.elapsed("planning"),
                    fetch_ms=stages.elapsed("fetching"),
                    synth_ms=stages.elapsed("synthesising"),
                    total_ms=elapsed_s * 1000.0,
                ))
                metrics.purge(conn, ttl_s=cfg.metrics_ttl_s, max_rows=cfg.metrics_max_rows)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — observability never fails an answer
            log_event(get_logger("pythia.api.service"), logging.WARNING,
                      "metrics.record_failed", error=f"{type(exc).__name__}: {exc}")

    def _run(
        self,
        question: str,
        resource_id: str | None,
        conn: sqlite3.Connection,
        cache_conn: sqlite3.Connection,
        transport: Any,
        on_stage: StageCallback | None,
    ) -> AnswerBundle:
        """The orchestration ``answer.py::_run`` held, with no ``args`` and no ``SystemExit``."""
        from pythia.access import catalog
        from pythia.access.data_client import fetch_for_plan, fetch_resource

        cfg = self.settings

        def stage(name: str) -> None:
            if on_stage is not None:
                on_stage(name)

        if resource_id:
            # Bypass retrieval and planning entirely. Golden-set MRR is 0.544, so routing a
            # probe through retrieval means a synthesis failure and a retrieval miss look
            # identical — and this path also avoids loading the embedding model.
            stage("fetching")
            resource = catalog.get_resource(conn, resource_id)
            if resource is None:
                raise ValueError(f"no such resource: {resource_id}")
            prov = catalog.get_provenance(conn, resource.dataset_id)
            table = fetch_resource(resource, transport=transport, cache_conn=cache_conn,
                                   settings=cfg, provenance=prov)
            plan = _direct_plan(question, resource, prov)
            stage("synthesising")
            answer = answer_question(question, plan, table, llm=self.llm, settings=cfg)
            return self._bundle(plan, answer, conn)

        if self.llm is None:
            raise ValueError("planning needs an LLM; pass resource_id to bypass it")

        from pythia.planning.planner import make_plan

        stage("planning")
        plan = make_plan(
            question, conn=conn, model=self.model, chroma_path=cfg.chroma_path,
            llm=self.llm, settings=cfg,
        )
        ctx = None
        if plan.dataset is not None:
            prov = catalog.get_provenance(conn, plan.dataset.id)
            ctx = RefusalContext(
                dataset_title=prov.dataset_title, publisher=prov.publisher,
                last_updated=prov.last_updated,
                offered_formats=catalog.get_offered_formats(conn, plan.dataset.id),
            )
        if plan.status is not PlanStatus.MATCHED:
            stage("synthesising")
            answer = answer_question(question, plan, refusal_ctx=ctx, llm=self.llm, settings=cfg)
            return self._bundle(plan, answer, conn)
        try:
            stage("fetching")
            table = fetch_for_plan(plan, conn=conn, transport=transport, cache_conn=cache_conn,
                                   settings=cfg)
        except AccessError as exc:
            stage("synthesising")
            answer = answer_question(question, plan, error=exc, refusal_ctx=ctx, llm=self.llm,
                                     settings=cfg)
            return self._bundle(plan, answer, conn)
        stage("synthesising")
        answer = answer_question(question, plan, table, refusal_ctx=ctx, llm=self.llm,
                                 settings=cfg)
        return self._bundle(plan, answer, conn)

    def _bundle(
        self, plan: QueryPlan, answer: Answer, conn: sqlite3.Connection
    ) -> AnswerBundle:
        """Pair an answer with its recovery context, resolved on the caller's connection."""
        return AnswerBundle(
            answer=answer,
            recovery=build_recovery_context(plan, answer, conn=conn, settings=self.settings),
        )


def _direct_plan(question: str, resource: Any, prov: Any) -> QueryPlan:
    """Build a MATCHED plan for a resource named directly, with no retrieval involved."""
    from pythia.planning.models import QueryParams
    from pythia.planning.normalize import detect_language
    from pythia.retrieval.search import Candidate

    candidate = Candidate(
        id=resource.dataset_id, name=resource.dataset_id, title=prov.dataset_title,
        last_updated=prov.last_updated, rank=1, score=0.0,
    )
    return QueryPlan(
        question=question, normalized_question=question, language=detect_language(question),
        status=PlanStatus.MATCHED, dataset=candidate, resource_id=resource.id,
        resource_format=resource.format, resource_url=resource.url, access_path=None,
        params=QueryParams(), confidence=1.0, reason="resource named directly on the CLI",
        degraded=False, candidates=[candidate],
    )
