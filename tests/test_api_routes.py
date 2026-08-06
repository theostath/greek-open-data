"""Route tests. Fakes throughout: no model load, no Ollama, no network."""

from __future__ import annotations

from tests.api_harness import (
    ORIGIN,
    Never,
    answered,
    build,
    matched_but_refused,
    no_match,
    unsupported,
)

from pythia.api.jobs import JobStore


def _ask(client: object, question: str = "Πόσα αιτήματα ασύλου;") -> object:
    """Post a question from the app's own origin."""
    return client.post("/ask", data={"question": question},  # type: ignore[attr-defined]
                       headers={"Origin": ORIGIN})


def test_the_landing_page_renders_the_teaching_empty_state() -> None:
    """An empty box teaches nothing; the first run has to say what is answerable."""
    client, _ = build()

    body = client.get("/").text

    assert "What this can answer" in body
    assert "Try one of these" in body


def test_an_empty_question_renders_a_message_not_a_422_body() -> None:
    """The client is a browser, not a JSON consumer."""
    client, _ = build()

    response = _ask(client, "   ")

    assert response.status_code == 400
    assert "Nothing to ask" in response.text
    assert "detail" not in response.text.lower()[:200]


def test_an_over_length_question_is_refused_with_the_limit_named() -> None:
    """The field reaches an LLM prompt, so its bound is real and worth explaining."""
    client, _ = build(api_max_question_chars=20)

    response = _ask(client, "x" * 21)

    assert response.status_code == 400
    assert "too long" in response.text


def test_submitting_returns_a_fragment_that_polls() -> None:
    """Progress must appear within a second; the fragment drives it with hx-trigger."""
    client, _ = build(answered(), executor=Never())

    response = _ask(client)

    assert response.status_code == 200
    assert 'hx-trigger="every 1s"' in response.text


def test_a_queued_job_says_it_is_waiting_for_a_worker() -> None:
    """With two workers a third question waits with no stage at all. Say so."""
    client, _ = build(answered(), executor=Never())

    job_id = _ask(client).text.split('hx-get="/ask/')[1].split('"')[0]
    body = client.get(f"/ask/{job_id}").text

    assert "Waiting for a free worker" in body


def test_the_terminal_fragment_stops_the_polling() -> None:
    """Polling ends by the fragment omitting the attribute — there is no stop header."""
    client, _ = build(answered())

    job_id = _ask(client).text.split('hx-get="/ask/')[1].split('"')[0]
    body = client.get(f"/ask/{job_id}").text

    assert "hx-trigger" not in body


def test_an_answered_render_carries_every_citation_field() -> None:
    """Principle #2 enforced by the suite, not by review."""
    client, _ = build(answered())

    job_id = _ask(client).text.split('hx-get="/ask/')[1].split('"')[0]
    body = client.get(f"/ask/{job_id}").text

    assert "ΑΙΤΗΜΑΤΑ ΑΣΥΛΟΥ 2024 ΑΝΑ ΥΠΗΚΟΟΤΗΤΑ" in body   # dataset title
    assert "Ελληνική Κυβέρνηση" in body                      # publisher
    assert "ενημερώθηκε" in body                             # freshness, stated in words
    assert "όλες οι 5 γραμμές" in body                       # row coverage
    assert "https://data.gov.gr/dataset/" in body            # a followable source link


def test_a_no_match_refusal_offers_the_near_misses_as_links() -> None:
    """A refusal that leaves the user with no next move is a design failure."""
    client, _ = build(no_match())

    job_id = _ask(client).text.split('hx-get="/ask/')[1].split('"')[0]
    body = client.get(f"/ask/{job_id}").text

    assert "No dataset in the catalogue covers this" in body
    assert "https://data.gov.gr/dataset/ds-fires" in body
    assert "Πυρκαγιές δασών" in body


def test_an_unsupported_refusal_names_the_formats_the_catalogue_lists() -> None:
    """The dataset was found; say what it does publish."""
    client, _ = build(unsupported())

    job_id = _ask(client).text.split('hx-get="/ask/')[1].split('"')[0]
    body = client.get(f"/ask/{job_id}").text

    assert "not in a format this can read" in body
    assert "PDF" in body and "XLSX" in body


def test_a_matched_plan_refusal_is_never_framed_as_a_near_miss() -> None:
    """Blocker 3. Retrieval succeeded here; near-miss framing inverts the truth."""
    client, _ = build(matched_but_refused())

    job_id = _ask(client).text.split('hx-get="/ask/')[1].split('"')[0]
    body = client.get(f"/ask/{job_id}").text

    assert "This is the right dataset" in body
    assert "Δείκτης τιμών ΕΛΣΤΑΤ" in body
    assert "Closest in the catalogue" not in body
    assert "ΛΑΘΟΣ" not in body, "a near miss leaked into the third refusal shape"


def test_an_unknown_job_renders_the_expired_fragment() -> None:
    client, _ = build()

    body = client.get("/ask/deadbeef-000000000000").text

    assert "restarted" in body.lower() or "expired" in body.lower() or "No such result" in body


def test_a_permalink_to_an_unknown_job_returns_a_whole_page() -> None:
    """This route is reached by direct navigation; a bare partial has no head."""
    client, _ = build()

    response = client.get("/a/deadbeef-000000000000")

    assert response.status_code == 404
    assert "<html" in response.text and "</html>" in response.text
    assert "app.css" in response.text


def test_a_permalink_to_a_finished_job_renders_the_full_answer() -> None:
    client, _ = build(answered())

    job_id = _ask(client).text.split('hx-get="/ask/')[1].split('"')[0]
    response = client.get(f"/a/{job_id}")

    assert response.status_code == 200
    assert "<html" in response.text
    assert "Ελληνική Κυβέρνηση" in response.text


def test_healthz_reports_counts_and_no_secrets() -> None:
    """``db.connect()`` creates a missing file, so existence is not readiness."""
    client, _ = build()

    payload = client.get("/healthz").json()

    assert "datasets" in payload and isinstance(payload["datasets"], int)
    assert "dense_index" in payload and "lexical_index" in payload
    for leaked in ("token", "key", "secret", "password"):
        assert not any(leaked in name.lower() for name in payload), f"{leaked} in /healthz"


def test_a_busy_store_renders_a_message_rather_than_failing() -> None:
    """Backpressure is a UI state, not a 500."""
    client, _ = build(answered(), executor=Never(), api_max_pending_jobs=1)

    _ask(client, "πρώτη")
    response = _ask(client, "δεύτερη")

    assert response.status_code == 429
    assert "Busy right now" in response.text


def test_a_failed_job_blames_the_publisher_before_blaming_pythia() -> None:
    """§5: the upstream API has no SLA, so publisher failure is expected, not a bug."""
    def boom(question: str, resource_id: str | None, on_stage: object) -> object:
        raise RuntimeError("connection reset")

    client, store = build(answered())
    store._run = boom  # type: ignore[assignment]

    job_id = _ask(client).text.split('hx-get="/ask/')[1].split('"')[0]
    body = client.get(f"/ask/{job_id}").text

    assert "publisher" in body
    assert "Traceback" not in body


def test_the_job_store_dependency_is_the_one_the_app_was_built_with() -> None:
    """Guards the harness itself: a stale override would make every test vacuous."""
    client, store = build(answered())

    assert isinstance(store, JobStore)
    _ask(client)
    assert store.get(next(iter(store._jobs))) is not None
