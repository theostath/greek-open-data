"""Route tests for /stats (issue #22)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from config import Settings
from fastapi.testclient import TestClient

from pythia.api import metrics
from pythia.api.app import create_app, get_jobs, get_pipeline, get_settings_dep


def _metric(**kw: object) -> metrics.AnswerMetric:
    base = {
        "asked_at": "2026-08-07T10:00:00+00:00", "question_chars": 30, "language": "el",
        "pinned": False, "plan_status": "matched", "dataset_id": "ds-1", "confidence": 0.8,
        "plan_degraded": False, "answer_status": "answered", "refusal_shape": None,
        "narration_rejected": False, "caveats": 0, "row_count": 100, "complete": True,
        "from_cache": False, "prompt_tokens": 500, "completion_tokens": 80, "llm_calls": 2,
        "llm_ms": 9000.0, "total_ms": 12000.0,
    }
    return metrics.AnswerMetric(**{**base, **kw})  # type: ignore[arg-type]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, Settings]]:
    settings = Settings(metrics_db_path=str(tmp_path / "metrics.sqlite"))
    app = create_app(lifespan_handler=None)
    app.dependency_overrides[get_settings_dep] = lambda: settings
    app.dependency_overrides[get_jobs] = lambda: None
    app.dependency_overrides[get_pipeline] = lambda: None
    yield TestClient(app), settings


def _seed(settings: Settings, *rows: metrics.AnswerMetric) -> None:
    conn = metrics.connect(settings.metrics_db_path)
    metrics.init_db(conn)
    for row in rows:
        metrics.record(conn, row)
    conn.close()


def test_stats_on_a_fresh_install_says_so_rather_than_erroring(
    client: tuple[TestClient, Settings]
) -> None:
    """Reachable before anyone has asked anything — an empty dashboard is not a 500."""
    response = client[0].get("/stats")

    assert response.status_code == 200
    assert "Nothing recorded yet" in response.text


def test_the_refusal_mix_leads_the_page(client: tuple[TestClient, Settings]) -> None:
    """It is the product health signal, so it is not buried under latency."""
    test_client, settings = client
    _seed(settings,
          _metric(answer_status="answered"),
          _metric(answer_status="refused", refusal_shape="no_match"),
          _metric(answer_status="refused", refusal_shape="unsupported"))

    body = test_client.get("/stats").text

    assert "Outcomes" in body
    assert "no match" in body and "unsupported" in body
    assert body.index("Outcomes") < body.index("Time and cost")


def test_browsed_and_searched_answer_rates_are_compared(
    client: tuple[TestClient, Settings]
) -> None:
    """The direct measure of whether #18 pays off."""
    test_client, settings = client
    _seed(settings,
          _metric(pinned=True, answer_status="answered"),
          _metric(pinned=False, answer_status="refused", refusal_shape="no_match"))

    body = test_client.get("/stats").text

    assert "Pinned from browsing" in body and "Found by searching" in body
    assert "100% answered" in body and "0% answered" in body


def test_token_totals_are_shown(client: tuple[TestClient, Settings]) -> None:
    test_client, settings = client
    _seed(settings, _metric(prompt_tokens=1500, completion_tokens=250))

    body = test_client.get("/stats").text

    assert "1,500" in body and "250" in body


def test_the_page_never_shows_a_question(client: tuple[TestClient, Settings]) -> None:
    """Structural: there is no column that could hold one, and no field that could render it."""
    test_client, settings = client
    _seed(settings, _metric(question_chars=42))

    body = test_client.get("/stats").text

    for forbidden in ("question_chars", "dataset_id", "ds-1"):
        assert forbidden not in body, f"{forbidden!r} is internal and should not be rendered"


def test_stats_carries_the_security_headers(client: tuple[TestClient, Settings]) -> None:
    headers = client[0].get("/stats").headers

    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"


def test_percentages_never_divide_by_zero(client: tuple[TestClient, Settings]) -> None:
    """No pinned questions yet is the normal state on day one."""
    test_client, settings = client
    _seed(settings, _metric(pinned=False, answer_status="answered"))

    response = test_client.get("/stats")

    assert response.status_code == 200
    assert "0% answered" in response.text  # the pinned row, with no pinned questions
