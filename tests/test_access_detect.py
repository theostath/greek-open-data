"""Tests for content identification (Phase 5).

The catalog's `format` field lies: resources declared CSV are observed serving HTML, and
70% of CSV/JSON URLs are extensionless query APIs. This gate is what stops an error page
becoming a table.
"""

from __future__ import annotations

import pytest

from pythia.access import detect
from pythia.access.models import MalformedPayloadError


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"a,b\n1,2\n", "csv"),
        (b'{"records": []}', "json"),
        (b"[1,2]", "json"),
        (b"<!DOCTYPE html><html><body>404</body></html>", "html"),
        (b"  \n<html>", "html"),
        (b'<?xml version="1.0"?><wfs:FeatureCollection/>', "xml"),
        (b"PK\x03\x04\x14\x00", "zip"),
        (b"%PDF-1.4", "pdf"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1", "ole"),
        (b"\x1f\x8b\x08\x00", "gzip"),
        (b"", "empty"),
        (b"   \n ", "empty"),
    ],
)
def test_identify(body: bytes, expected: str) -> None:
    """Magic bytes and leading markers classify the payload."""
    assert detect.identify(body) == expected


def test_content_type_html_is_a_negative_signal() -> None:
    """text/html wins even when the body would otherwise look like CSV."""
    assert detect.identify(b"a,b\n1,2\n", "text/html; charset=utf-8") == "html"


def test_octet_stream_is_not_treated_as_a_signal() -> None:
    """Azure blobs send application/octet-stream for real CSVs."""
    assert detect.identify(b"a,b\n1,2\n", "application/octet-stream") == "csv"


@pytest.mark.parametrize("kind", ["html", "zip", "pdf", "ole", "xml", "binary", "empty"])
def test_non_tabular_kinds_are_refused(kind: str) -> None:
    """Anything that is not CSV/JSON is refused rather than parsed."""
    with pytest.raises(MalformedPayloadError):
        detect.ensure_matches_declared(kind, "CSV")


@pytest.mark.parametrize("kind", ["csv", "json"])
def test_csv_and_json_are_interchangeable(kind: str) -> None:
    """A declared-CSV resource serving JSON still parses; the declaration is often wrong."""
    detect.ensure_matches_declared(kind, "CSV")


def test_html_error_page_declared_csv_is_the_headline_case() -> None:
    """opendata.attica.gov.gr serves HTML for resources declared CSV."""
    body = b"<!DOCTYPE html>\n<html><head><title>404</title></head></html>"
    with pytest.raises(MalformedPayloadError, match="html"):
        detect.ensure_matches_declared(detect.identify(body), "CSV")
