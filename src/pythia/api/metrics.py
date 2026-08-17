"""Metrics store: one row per answered question, and the aggregates ``/stats`` renders.

Two rules shape this module.

**Nothing user-generated is persisted.** No question text, no narration, no cell values — the
question is user content (§6) and the rows are third-party data. ``question_chars`` is the safe
substitute, and both the schema and the dataclass are asserted against text-shaped fields.

**Observability must never fail an answer.** Every write is wrapped: a broken or missing
metrics database degrades to silence rather than turning a successful answer into an error.
That asymmetry is deliberate — counting is worth less than answering.

The headline metric is the **refusal mix**, not latency. Grounded-or-silent means the
answered / unsupported / no_match ratio *is* the health signal: a sudden fall in ``no_match``
would suggest the honesty gate had loosened, which is the failure this project most needs to
notice.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pythia.logging_setup import get_logger

LOGGER_NAME = "pythia.api.metrics"

_SCHEMA = Path(__file__).with_name("metrics_schema.sql")

_COLUMNS = (
    "asked_at", "question_chars", "language", "pinned", "plan_status", "dataset_id",
    "confidence", "plan_degraded", "answer_status", "refusal_shape", "narration_rejected",
    "caveats", "row_count", "complete", "from_cache", "prompt_tokens", "completion_tokens",
    "llm_calls", "llm_ms", "plan_ms", "fetch_ms", "synth_ms", "total_ms",
)


@dataclass(frozen=True)
class AnswerMetric:
    """What one question cost and what it produced. Deliberately carries no free text."""

    asked_at: str
    question_chars: int
    language: str
    pinned: bool
    plan_status: str | None
    dataset_id: str | None
    confidence: float | None
    plan_degraded: bool
    answer_status: str
    refusal_shape: str | None
    narration_rejected: bool
    caveats: int
    row_count: int | None
    complete: bool | None
    from_cache: bool | None
    prompt_tokens: int
    completion_tokens: int
    llm_calls: int
    llm_ms: float
    total_ms: float
    plan_ms: float = 0.0
    fetch_ms: float = 0.0
    synth_ms: float = 0.0


@dataclass(frozen=True)
class Summary:
    """Aggregates for ``/stats``."""

    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_refusal: dict[str, int] = field(default_factory=dict)
    by_language: dict[str, int] = field(default_factory=dict)
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    cache_hits: int = 0
    fetched: int = 0
    degraded: int = 0
    guard_rejections: int = 0
    #: Pinned questions come from the browse surface and bypass retrieval; comparing their
    #: answered rate against searched ones is the direct measure of whether #18 helps.
    pinned_total: int = 0
    pinned_answered: int = 0
    searched_total: int = 0
    searched_answered: int = 0


def connect(db_path: str) -> sqlite3.Connection:
    """Open (creating parent dirs for) the metrics database."""
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(db_path, check_same_thread=False)


def init_db(conn: sqlite3.Connection) -> None:
    """Apply the committed schema."""
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    conn.commit()


def utc_now() -> str:
    """Timestamp for a new row."""
    return datetime.now(UTC).isoformat()


def record(conn: sqlite3.Connection, metric: AnswerMetric) -> None:
    """Persist one row. Never raises: a metrics failure must not fail an answer."""
    values = [getattr(metric, name) for name in _COLUMNS]
    placeholders = ", ".join("?" for _ in _COLUMNS)
    try:
        conn.execute(
            f"INSERT INTO answers ({', '.join(_COLUMNS)}) VALUES ({placeholders})",  # noqa: S608
            values,
        )
        conn.commit()
    except sqlite3.Error as exc:
        get_logger(LOGGER_NAME).log(logging.WARNING, "metrics write failed: %s", exc)


def purge(
    conn: sqlite3.Connection, *, ttl_s: int, max_rows: int, now: str | None = None
) -> int:
    """Drop rows past the TTL, then the oldest beyond ``max_rows``. Returns rows removed."""
    moment = datetime.fromisoformat(now) if now else datetime.now(UTC)
    cutoff = (moment.timestamp() - ttl_s)
    removed = 0
    with suppress(sqlite3.Error):
        stale = [
            row_id
            for row_id, asked_at in conn.execute("SELECT id, asked_at FROM answers")
            if _epoch(asked_at) < cutoff
        ]
        if stale:
            conn.executemany("DELETE FROM answers WHERE id = ?", [(i,) for i in stale])
            removed += len(stale)
        # Oldest first, so the ceiling keeps the most recent window.
        cursor = conn.execute(
            "DELETE FROM answers WHERE id NOT IN "
            "(SELECT id FROM answers ORDER BY asked_at DESC, id DESC LIMIT ?)",
            (max(0, max_rows),),
        )
        removed += cursor.rowcount if cursor.rowcount > 0 else 0
        conn.commit()
    return removed


def summary(conn: sqlite3.Connection) -> Summary:
    """Aggregate every row. Returns an empty summary rather than raising on a fresh store."""
    try:
        rows = conn.execute(
            "SELECT answer_status, refusal_shape, language, total_ms, prompt_tokens, "
            "completion_tokens, llm_calls, from_cache, plan_degraded, narration_rejected, "
            "pinned FROM answers"
        ).fetchall()
    except sqlite3.Error:
        return Summary()
    if not rows:
        return Summary()

    by_status: dict[str, int] = {}
    by_refusal: dict[str, int] = {}
    by_language: dict[str, int] = {}
    latencies: list[float] = []
    totals = {"prompt": 0, "completion": 0, "calls": 0, "cache": 0, "fetched": 0,
              "degraded": 0, "guard": 0, "pin_n": 0, "pin_ok": 0, "srch_n": 0, "srch_ok": 0}

    for (status, refusal, language, total_ms, prompt, completion, calls, from_cache,
         degraded, rejected, pinned) in rows:
        by_status[status] = by_status.get(status, 0) + 1
        if refusal:
            by_refusal[refusal] = by_refusal.get(refusal, 0) + 1
        by_language[language] = by_language.get(language, 0) + 1
        latencies.append(float(total_ms or 0.0))
        totals["prompt"] += int(prompt or 0)
        totals["completion"] += int(completion or 0)
        totals["calls"] += int(calls or 0)
        if from_cache is not None:
            totals["fetched"] += 1
            totals["cache"] += 1 if from_cache else 0
        totals["degraded"] += 1 if degraded else 0
        totals["guard"] += 1 if rejected else 0
        answered = status in {"answered", "partial"}
        if pinned:
            totals["pin_n"] += 1
            totals["pin_ok"] += 1 if answered else 0
        else:
            totals["srch_n"] += 1
            totals["srch_ok"] += 1 if answered else 0

    return Summary(
        total=len(rows), by_status=by_status, by_refusal=by_refusal, by_language=by_language,
        p50_ms=_percentile(latencies, 50), p95_ms=_percentile(latencies, 95),
        prompt_tokens=totals["prompt"], completion_tokens=totals["completion"],
        llm_calls=totals["calls"], cache_hits=totals["cache"], fetched=totals["fetched"],
        degraded=totals["degraded"], guard_rejections=totals["guard"],
        pinned_total=totals["pin_n"], pinned_answered=totals["pin_ok"],
        searched_total=totals["srch_n"], searched_answered=totals["srch_ok"],
    )


def _percentile(values: list[float], pct: int) -> float:
    """Nearest-rank percentile. Exact and obvious, which matters more here than smoothness."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, -(-pct * len(ordered) // 100))
    return ordered[min(rank, len(ordered)) - 1]


def _epoch(asked_at: str) -> float:
    """Parse a stored timestamp; an unparseable one is treated as ancient and purged."""
    try:
        return datetime.fromisoformat(asked_at).timestamp()
    except (TypeError, ValueError):
        return 0.0
