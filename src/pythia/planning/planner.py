"""Phase 4 orchestrator: NL question -> grounded, typed ``QueryPlan``.

``make_plan`` chains deterministic stages around one structured LLM call: normalize the
question, retrieve scored candidates, optionally let the LLM disambiguate the shortlist,
select a CSV/JSON resource, then ask the LLM for a relevance verdict + intent parameters,
which are validated deterministically. Grounded-or-silent throughout; the LLM only proposes
language-level structure and is never trusted to select datasets or invent figures.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date
from functools import cache
from pathlib import Path
from time import perf_counter
from typing import Any

from config import Settings, get_settings
from sentence_transformers import SentenceTransformer

from pythia.llm import LLMClient, LLMError, Message
from pythia.logging_setup import get_logger, log_event
from pythia.planning.models import AGGREGATIONS, PlanStatus, QueryParams, QueryPlan
from pythia.planning.normalize import normalize_question
from pythia.planning.select import access_path, select_resource
from pythia.retrieval.rerank import Scorer
from pythia.retrieval.search import Candidate, confidence, find_dataset

LOGGER_NAME = "pythia.planning.planner"
_PROMPTS = Path(__file__).parent / "prompts"


@cache
def _prompt(name: str) -> str:
    """Load and cache a versioned prompt file by basename."""
    return (_PROMPTS / name).read_text(encoding="utf-8")


def make_plan(
    question: str,
    *,
    conn: sqlite3.Connection,
    model: SentenceTransformer,
    chroma_path: str,
    llm: LLMClient,
    reranker: Scorer | None = None,
    reference_date: date | None = None,
    settings: Settings | None = None,
) -> QueryPlan:
    """Turn a NL question into a typed, grounded ``QueryPlan``."""
    cfg = settings or get_settings()
    logger = get_logger(LOGGER_NAME)
    started = perf_counter()

    normalized, language = normalize_question(question)

    if len(question.strip()) < cfg.planning_min_question_chars:
        return _log_and_return(
            logger, started,
            _plan(question, normalized, language, PlanStatus.NO_MATCH, [], 0.0,
                  "question too short to plan", degraded=False),
        )

    candidates = find_dataset(
        normalized, conn=conn, model=model, chroma_path=chroma_path,
        top_k=cfg.retrieval_top_k, reranker=reranker, rerank_pool=cfg.rerank_pool,
    )
    if not candidates:
        return _log_and_return(
            logger, started,
            _plan(question, normalized, language, PlanStatus.NO_MATCH, [], 0.0,
                  "no dataset retrieved for this question", degraded=False),
        )

    conf = confidence(candidates[0].score)

    chosen = candidates[0]
    if cfg.planning_llm_disambiguate and len(candidates) > 1:
        index = _disambiguate(llm, question, candidates, cfg)
        if index == -1:
            return _log_and_return(
                logger, started,
                _plan(question, normalized, language, PlanStatus.NO_MATCH, candidates, conf,
                      "LLM found no relevant dataset among the candidates", degraded=False),
            )
        chosen = candidates[index] if 0 <= index < len(candidates) else candidates[0]

    meta = _fetch_dataset_meta(conn, chosen.id)
    if meta is None:
        log_event(logger, logging.WARNING, "planning.hydrate_miss", dataset_id=chosen.id)
        return _log_and_return(
            logger, started,
            _plan(question, normalized, language, PlanStatus.NO_MATCH, candidates, conf,
                  "catalog integrity issue: dataset row missing", degraded=True, dataset=chosen),
        )

    resource = select_resource(conn, chosen.id)
    if resource is None:
        return _log_and_return(
            logger, started,
            _plan(question, normalized, language, PlanStatus.UNSUPPORTED, candidates, conf,
                  "dataset matched but has no CSV/JSON resource (not yet supported)",
                  degraded=False, dataset=chosen),
        )

    messages: list[Message] = [
        {"role": "system", "content": _prompt("extract_plan.md")},
        {"role": "user", "content": _user_message(question, meta, reference_date)},
    ]
    try:
        raw = llm.complete_json(messages, max_tokens=cfg.llm_max_tokens)
        relevant = raw.get("relevant")
        if not isinstance(relevant, bool):
            raise LLMError("LLM response missing a boolean 'relevant'")
        reason = str(raw.get("reason") or "")
        if not relevant:
            plan = _plan(question, normalized, language, PlanStatus.NO_MATCH, candidates, conf,
                         reason or "LLM judged the dataset not relevant", degraded=False,
                         dataset=chosen)
        else:
            params = _validate_params(raw.get("params"), limit_max=cfg.planning_limit_max)
            plan = _plan(question, normalized, language, PlanStatus.MATCHED, candidates, conf,
                         reason or "dataset matched", degraded=False, dataset=chosen,
                         resource=resource, params=params)
    except LLMError as exc:
        plan = _degraded_plan(
            question, normalized, language, candidates, conf, resource, chosen, cfg
        )
        log_event(logger, logging.WARNING, "planning.llm_degraded",
                  error=str(exc), confidence=round(conf, 3), status=plan.status.value)

    return _log_and_return(logger, started, plan)


def _degraded_plan(
    question: str, normalized: str, language: str, candidates: list[Candidate],
    conf: float, resource: Any, chosen: Candidate, cfg: Settings,
) -> QueryPlan:
    """Build the plan used when the LLM is unavailable: fall back to the score floor."""
    if conf >= cfg.planning_score_threshold:
        return _plan(question, normalized, language, PlanStatus.MATCHED, candidates, conf,
                     "LLM unavailable; matched by retrieval score floor", degraded=True,
                     dataset=chosen, resource=resource)
    return _plan(question, normalized, language, PlanStatus.NO_MATCH, candidates, conf,
                 "LLM unavailable and retrieval confidence below the floor", degraded=True,
                 dataset=chosen)


def _plan(
    question: str, normalized: str, language: str, status: PlanStatus,
    candidates: list[Candidate], conf: float, reason: str, *, degraded: bool,
    dataset: Candidate | None = None, resource: Any = None,
    params: QueryParams | None = None,
) -> QueryPlan:
    """Assemble a ``QueryPlan``; resource fields populate only when a resource is chosen."""
    return QueryPlan(
        question=question,
        normalized_question=normalized,
        language=language,
        status=status,
        dataset=dataset,
        resource_id=resource.id if resource is not None else None,
        resource_format=resource.format if resource is not None else None,
        resource_url=resource.url if resource is not None else None,
        access_path=access_path(resource) if resource is not None else None,
        params=params or QueryParams(),
        confidence=conf,
        reason=reason,
        degraded=degraded,
        candidates=candidates,
    )


def _log_and_return(logger: logging.Logger, started: float, plan: QueryPlan) -> QueryPlan:
    """Emit the structured planning trace and return the plan."""
    log_event(
        logger, logging.INFO, "planning.done",
        language=plan.language,
        status=plan.status.value,
        dataset_id=plan.dataset.id if plan.dataset else None,
        resource_id=plan.resource_id,
        confidence=round(plan.confidence, 3),
        degraded=plan.degraded,
        latency_ms=round((perf_counter() - started) * 1000, 1),
    )
    return plan


def _fetch_dataset_meta(conn: sqlite3.Connection, dataset_id: str) -> dict[str, Any] | None:
    """Return title/notes/tags for the dataset, or ``None`` if the row is missing."""
    row = conn.execute(
        "SELECT title, title_en, notes, notes_en, tags FROM datasets WHERE id = ?",
        (dataset_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        tags = json.loads(row[4]) if row[4] else []
    except json.JSONDecodeError:
        tags = []
    return {"title": row[0], "title_en": row[1], "notes": row[2], "notes_en": row[3], "tags": tags}


def _user_message(question: str, meta: dict[str, Any], reference_date: date | None) -> str:
    """Render the user chat message: the untrusted question plus candidate context."""
    tags = ", ".join(str(t) for t in meta.get("tags") or [])
    lines = [
        f"Question: {question}",
        "",
        "Candidate dataset:",
        f"- title: {meta.get('title') or ''}",
        f"- title_en: {meta.get('title_en') or ''}",
        f"- notes: {meta.get('notes') or ''}",
        f"- tags: {tags}",
    ]
    if reference_date is not None:
        lines.append(f"- reference_date: {reference_date.isoformat()}")
    return "\n".join(lines)


def _validate_params(raw: Any, *, limit_max: int) -> QueryParams:
    """Deterministically validate the LLM's proposed params into a ``QueryParams``."""
    if not isinstance(raw, dict):
        return QueryParams()
    date_from = _valid_date(raw.get("date_from"))
    date_to = _valid_date(raw.get("date_to"))
    if date_from and date_to and date_from > date_to:
        date_from = date_to = None  # incoherent range: drop the pair
    aggregation = raw.get("aggregation")
    if not (isinstance(aggregation, str) and aggregation.lower() in AGGREGATIONS):
        aggregation = None
    else:
        aggregation = aggregation.lower()
    return QueryParams(
        date_from=date_from,
        date_to=date_to,
        region=_clean_str(raw.get("region")),
        metrics=[s.strip() for s in raw.get("metrics") or [] if isinstance(s, str) and s.strip()],
        aggregation=aggregation,
        group_by=_clean_str(raw.get("group_by")),
        limit=_valid_limit(raw.get("limit"), limit_max),
    )


def _valid_date(value: Any) -> str | None:
    """Return an ISO-8601 date string if ``value`` parses as one, else ``None``."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _valid_limit(value: Any, limit_max: int) -> int | None:
    """Return a positive int clamped to ``limit_max``, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return min(value, limit_max)


def _clean_str(value: Any) -> str | None:
    """Return a stripped non-empty string, or ``None``."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _disambiguate(
    llm: LLMClient, question: str, candidates: list[Candidate], cfg: Settings
) -> int:
    """Ask the LLM to pick the best candidate index (or -1); fall back to 0 on failure."""
    pool = candidates[: cfg.planning_disambiguate_pool]
    listing = "\n".join(f"{i}: {c.title or c.name}" for i, c in enumerate(pool))
    messages: list[Message] = [
        {"role": "system", "content": _prompt("disambiguate.md")},
        {"role": "user", "content": f"Question: {question}\n\nCandidates:\n{listing}"},
    ]
    try:
        raw = llm.complete_json(messages, max_tokens=cfg.llm_max_tokens)
        index = raw.get("index")
        return index if isinstance(index, int) and not isinstance(index, bool) else 0
    except LLMError:
        return 0
