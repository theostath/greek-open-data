"""Typed contract for Phase 4 planning output.

``make_plan`` produces a ``QueryPlan``: the inspectable bridge between retrieval (Phase 3)
and the data client (Phase 5). Parameters are normalized *intent* (there is no per-resource
column schema at plan time), validated deterministically; the LLM only proposes them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pythia.retrieval.search import Candidate

# Aggregations the planner recognizes; anything else is dropped to None on validation.
AGGREGATIONS = frozenset({"count", "sum", "avg", "min", "max"})


class PlanStatus(StrEnum):
    """Terminal outcome of planning for one question."""

    MATCHED = "matched"  # dataset + CSV/JSON resource + validated params
    NO_MATCH = "no_match"  # grounded refusal: nothing covers the question
    UNSUPPORTED = "unsupported"  # dataset matched but only non-CSV/JSON resources exist


@dataclass(frozen=True)
class QueryParams:
    """Normalized *intent* parameters — not yet bound to real columns.

    ``metrics``/``group_by``/``region`` are free-text intent (no schema to validate
    against); only dates, ``aggregation`` and ``limit`` are constrained. ``date_from`` is
    guaranteed ``<= date_to`` when both are present.
    """

    date_from: str | None = None  # ISO-8601 date
    date_to: str | None = None  # ISO-8601 date
    region: str | None = None  # spatial text as-said; never code-joined
    metrics: list[str] = field(default_factory=list)  # metric keywords (intent)
    aggregation: str | None = None  # one of AGGREGATIONS or None
    group_by: str | None = None  # e.g. "nationality" | "year" | "region"
    limit: int | None = None  # positive int, clamped to a configured max, or None


@dataclass(frozen=True)
class QueryPlan:
    """The inspectable output of Phase 4."""

    question: str  # original, verbatim
    normalized_question: str  # transliterated (greeklish path) else the original
    language: str  # detection label: "el" | "en" | "greeklish" (NOT an ISO code)
    status: PlanStatus
    dataset: Candidate | None  # chosen dataset (carries provenance + score)
    resource_id: str | None  # catalog resource key (both access paths)
    resource_format: str | None  # "CSV" | "JSON"
    resource_url: str | None  # download endpoint; Phase 5 resolves the SAS fresh
    access_path: str | None  # "datastore" | "download" | None
    params: QueryParams
    confidence: float  # 0..1 retrieval confidence; 0.0 when retrieval is empty
    reason: str  # human-readable why (logged; supports honesty)
    degraded: bool  # True if the LLM step was skipped/failed/malformed
    candidates: list[Candidate]  # full ranked shortlist, for inspectability
