"""Tests for the metrics store (issue #22).

The privacy rule is the one worth breaking the build over: **no question text and no row
values are ever persisted.** Everything else here is aggregation.

The metric that actually matters is the refusal mix, not latency. Grounded-or-silent means the
answered/unsupported/no_match ratio *is* the product health signal — a sudden fall in
``no_match`` would suggest the honesty gate had loosened, which is the failure this project
most needs to notice.
"""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest
from config import Settings

from pythia.api.metrics import AnswerMetric, connect, init_db, purge, record, summary


def _metric(**kw: object) -> AnswerMetric:
    """A representative answered row; override any field per test."""
    base = {
        "asked_at": "2026-08-07T10:00:00+00:00", "question_chars": 30, "language": "el",
        "pinned": False, "plan_status": "matched", "dataset_id": "ds-1", "confidence": 0.8,
        "plan_degraded": False, "answer_status": "answered", "refusal_shape": None,
        "narration_rejected": False, "caveats": 0, "row_count": 100, "complete": True,
        "from_cache": False, "prompt_tokens": 500, "completion_tokens": 80, "llm_calls": 2,
        "llm_ms": 9000.0, "total_ms": 12000.0,
    }
    return AnswerMetric(**{**base, **kw})  # type: ignore[arg-type]


@pytest.fixture
def conn() -> sqlite3.Connection:
    store = connect(":memory:")
    init_db(store)
    return store


def test_a_row_is_recorded_and_counted(conn: sqlite3.Connection) -> None:
    record(conn, _metric())

    assert summary(conn).total == 1


def test_the_schema_has_no_column_that_could_hold_a_question(
    conn: sqlite3.Connection
) -> None:
    """§6: the question is user content and must not be persisted. Enforced structurally."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(answers)")}

    for forbidden in ("question", "text", "narration", "answer_text", "rows", "values"):
        assert forbidden not in columns, f"{forbidden!r} could hold user content"
    assert "question_chars" in columns, "length is the safe substitute"


def test_the_metric_type_itself_cannot_carry_the_question(
    conn: sqlite3.Connection
) -> None:
    """Guards the guard: a schema check is worthless if the dataclass grows a text field."""
    fields = {f.name for f in dataclasses.fields(AnswerMetric)}

    assert "question" not in fields and "text" not in fields


def test_the_refusal_mix_is_reported(conn: sqlite3.Connection) -> None:
    """The product health signal: grounded-or-silent lives or dies on this ratio."""
    record(conn, _metric(answer_status="answered"))
    record(conn, _metric(answer_status="refused", refusal_shape="no_match"))
    record(conn, _metric(answer_status="refused", refusal_shape="no_match"))
    record(conn, _metric(answer_status="refused", refusal_shape="unsupported"))

    result = summary(conn)

    assert result.by_status["answered"] == 1
    assert result.by_status["refused"] == 3
    assert result.by_refusal["no_match"] == 2
    assert result.by_refusal["unsupported"] == 1


def test_latency_percentiles_are_reported(conn: sqlite3.Connection) -> None:
    for ms in (100.0, 200.0, 300.0, 400.0, 5000.0):
        record(conn, _metric(total_ms=ms))

    result = summary(conn)

    assert result.p50_ms == 300.0
    assert result.p95_ms == 5000.0


def test_percentiles_on_an_empty_store_are_zero_not_a_crash(
    conn: sqlite3.Connection
) -> None:
    """/stats is reachable on a fresh checkout, before anyone has asked anything."""
    result = summary(conn)

    assert result.total == 0 and result.p50_ms == 0.0 and result.p95_ms == 0.0


def test_token_totals_are_summed(conn: sqlite3.Connection) -> None:
    record(conn, _metric(prompt_tokens=100, completion_tokens=10))
    record(conn, _metric(prompt_tokens=250, completion_tokens=40))

    result = summary(conn)

    assert result.prompt_tokens == 350
    assert result.completion_tokens == 50


def test_pinned_and_searched_questions_are_compared(conn: sqlite3.Connection) -> None:
    """Directly measures whether browsing (#18) beats retrieval — the reason it was built."""
    record(conn, _metric(pinned=True, answer_status="answered"))
    record(conn, _metric(pinned=True, answer_status="answered"))
    record(conn, _metric(pinned=False, answer_status="answered"))
    record(conn, _metric(pinned=False, answer_status="refused", refusal_shape="no_match"))
    record(conn, _metric(pinned=False, answer_status="refused", refusal_shape="no_match"))

    result = summary(conn)

    assert result.pinned_total == 2
    assert result.pinned_answered == 2
    assert result.searched_total == 3
    assert result.searched_answered == 1


def test_cache_hit_rate_is_reported(conn: sqlite3.Connection) -> None:
    record(conn, _metric(from_cache=True))
    record(conn, _metric(from_cache=False))
    record(conn, _metric(from_cache=None))  # a refusal never fetched anything

    assert summary(conn).cache_hits == 1


def test_a_refusal_records_its_shape_and_no_row_data(conn: sqlite3.Connection) -> None:
    record(conn, _metric(answer_status="refused", refusal_shape="matched_but_refused",
                         row_count=None, complete=None, from_cache=None))

    assert summary(conn).by_refusal["matched_but_refused"] == 1


def test_rows_past_the_ttl_are_purged(conn: sqlite3.Connection) -> None:
    record(conn, _metric(asked_at="2020-01-01T00:00:00+00:00"))
    record(conn, _metric(asked_at="2026-08-07T10:00:00+00:00"))

    removed = purge(conn, ttl_s=86_400, max_rows=1000, now="2026-08-07T12:00:00+00:00")

    assert removed == 1
    assert summary(conn).total == 1


def test_the_row_ceiling_drops_the_oldest_first(conn: sqlite3.Connection) -> None:
    """A local tool must not grow a database nobody prunes."""
    for day in range(1, 6):
        record(conn, _metric(asked_at=f"2026-08-0{day}T10:00:00+00:00"))

    purge(conn, ttl_s=10**9, max_rows=2, now="2026-08-07T12:00:00+00:00")

    remaining = [r[0] for r in conn.execute("SELECT asked_at FROM answers ORDER BY asked_at")]
    assert remaining == ["2026-08-04T10:00:00+00:00", "2026-08-05T10:00:00+00:00"]


def test_recording_never_raises_on_a_broken_store(conn: sqlite3.Connection) -> None:
    """Observability must never be able to fail an answer that already succeeded."""
    conn.execute("DROP TABLE answers")

    record(conn, _metric())  # must not raise


def test_the_settings_carry_bounds(conn: sqlite3.Connection) -> None:
    settings = Settings()

    assert settings.metrics_max_rows > 0
    assert settings.metrics_ttl_s > 0
