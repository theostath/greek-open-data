"""Security tests.

A loopback bind is not an access control: any page in any other tab can POST a cross-origin
form to this port, and CORS stops the reading of the response, not the request. These pin the
controls that actually apply.
"""

from __future__ import annotations

import pytest
from tests.api_harness import ORIGIN, answered, build, bundle_of
from tests.synthesis_fixtures import asylum_table, plan

from pythia.synthesis.answer import answer_question


def test_a_cross_origin_ask_is_rejected() -> None:
    """Otherwise any open tab can drive LLM inference and outbound fetches on this machine."""
    client, _ = build(answered())

    response = client.post("/ask", data={"question": "x"},
                           headers={"Origin": "http://evil.example"})

    assert response.status_code == 403
    assert "Blocked" in response.text


def test_a_same_origin_ask_is_accepted() -> None:
    client, _ = build(answered())

    response = client.post("/ask", data={"question": "x"}, headers={"Origin": ORIGIN})

    assert response.status_code == 200


def test_a_request_with_no_origin_is_accepted() -> None:
    """curl and same-origin navigations send none; this guards against other pages, not you."""
    client, _ = build(answered())

    assert client.post("/ask", data={"question": "x"}).status_code == 200


@pytest.mark.parametrize("path", ["/", "/healthz", "/ask/nope-000000000000"])
def test_security_headers_are_on_every_response(path: str) -> None:
    client, _ = build()

    headers = client.get(path).headers

    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "base-uri 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"


def test_the_csp_forbids_inline_script() -> None:
    """The chart spec ships as application/json data, so no inline execution is needed."""
    client, _ = build()

    csp = client.get("/").headers["content-security-policy"]

    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp


def test_a_hostile_dataset_title_renders_inert() -> None:
    """75% of fetchable resources are off-portal and titles come from CKAN."""
    hostile = "<script>alert(1)</script>"
    answer = answer_question("ερώτηση", plan(), asylum_table(title=hostile))
    client, _ = build(bundle_of(answer))

    job_id = client.post("/ask", data={"question": "q"},
                         headers={"Origin": ORIGIN}).text.split('hx-get="/ask/')[1].split('"')[0]
    body = client.get(f"/ask/{job_id}").text

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_a_hostile_label_cannot_break_out_of_the_chart_script() -> None:
    """|tojson escapes <, > and & — the spec is data, and must stay data."""
    answer = answer_question("ερώτηση", plan(), asylum_table(title="</script><script>x()"))
    client, _ = build(bundle_of(answer))

    job_id = client.post("/ask", data={"question": "q"},
                         headers={"Origin": ORIGIN}).text.split('hx-get="/ask/')[1].split('"')[0]
    body = client.get(f"/ask/{job_id}").text

    assert "</script><script>x()" not in body


def test_the_plan_never_reaches_the_browser() -> None:
    """Answer.plan carries the ranked shortlist with RRF scores and stays server-side."""
    client, _ = build(answered())

    job_id = client.post("/ask", data={"question": "q"},
                         headers={"Origin": ORIGIN}).text.split('hx-get="/ask/')[1].split('"')[0]
    body = client.get(f"/ask/{job_id}").text

    for leaked in ("normalized_question", "resource_url", "confidence", "candidates",
                   "rrf", "access_path"):
        assert leaked not in body, f"{leaked!r} leaked into the rendered page"


def test_a_greek_question_and_title_survive_the_round_trip() -> None:
    """Encoding is this project's oldest recurring bug class (§5)."""
    client, _ = build(answered())

    response = client.post("/ask", data={"question": "πόσες πυρκαγιές το 2023;"},
                           headers={"Origin": ORIGIN})
    job_id = response.text.split('hx-get="/ask/')[1].split('"')[0]
    body = client.get(f"/ask/{job_id}").text

    assert "Ελληνική Κυβέρνηση" in body
    assert "Ï" not in body and "Î" not in body, "mojibake in the rendered page"
