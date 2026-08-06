"""Tests for the Phase 5 orchestrator (transport faked, no network)."""

from __future__ import annotations

import logging
import sqlite3

import pytest
from config import Settings

from pythia.access import cache
from pythia.access.data_client import fetch_for_plan, fetch_resource
from pythia.access.models import (
    IncompleteReason,
    MalformedPayloadError,
    NoMatchError,
    ResourceUnavailableError,
    TableData,
    UnsupportedResourceError,
)
from pythia.access.transport import FakeTransport, RawResponse
from pythia.ingest import db
from pythia.ingest.models import DatasetRow, ResourceRow
from pythia.planning.models import PlanStatus, QueryParams, QueryPlan

CSV = "Νομός,plithos\nΑττικής,1234\n".encode()
SETTINGS = Settings()


def _resource(**over: object) -> ResourceRow:
    """Build a download-path CSV resource."""
    base = {
        "id": "res-1", "dataset_id": "ds-1", "name": "data", "description": None,
        "format": "CSV", "mimetype": None, "url": "https://data.gov.gr/x.csv", "size": 40,
        "datastore_active": False, "position": 0, "last_modified": "2026-01-01T00:00:00",
        "metadata_modified": "2026-01-01T00:00:00", "state": "active", "is_tabular": True,
    }
    return ResourceRow(**{**base, **over})  # type: ignore[arg-type]


def _raw(body: bytes, complete: bool = True) -> RawResponse:
    """Wrap a body as a successful transport response."""
    return RawResponse(body=body, complete=complete, status=200, content_type=None,
                       redirected=False, final_host="data.gov.gr")


def _cache_conn() -> sqlite3.Connection:
    """Open an initialised in-memory cache."""
    conn = sqlite3.connect(":memory:")
    cache.init_cache_db(conn)
    return conn


def test_download_happy_path() -> None:
    """A Greek CSV download parses with intact text and full provenance."""
    transport = FakeTransport(byte_responses={"x.csv": _raw(CSV)})
    table = fetch_resource(_resource(), transport=transport, settings=SETTINGS)
    assert table.complete is True
    assert table.incomplete_reason is None
    assert table.row_count == 1
    assert table.rows[0]["Νομός"] == "Αττικής"  # not mojibake
    assert table.access_path == "download"
    assert table.source_url == "https://data.gov.gr/x.csv"


def test_byte_cap_marks_incomplete_and_drops_partial_row() -> None:
    """A truncated body never yields a fabricated final record."""
    truncated = CSV + b"\xce\x98\xce\xb5\xcf\x83,56"  # partial trailing line
    transport = FakeTransport(byte_responses={"x.csv": _raw(truncated, complete=False)})
    table = fetch_resource(_resource(), transport=transport, settings=SETTINGS)
    assert table.complete is False
    assert table.incomplete_reason is IncompleteReason.BYTE_CAP
    assert table.row_count == 1  # the "56" fragment is gone, not padded into a row


def test_html_error_page_is_refused() -> None:
    """An HTML 404 served with 200 must not become a table."""
    transport = FakeTransport(byte_responses={"x.csv": _raw(b"<!DOCTYPE html><html>404")})
    with pytest.raises(MalformedPayloadError):
        fetch_resource(_resource(), transport=transport, settings=SETTINGS)


def test_unsupported_format_refused_before_any_call() -> None:
    """Format is checked independently of Phase 4's selection."""
    transport = FakeTransport()
    with pytest.raises(UnsupportedResourceError):
        fetch_resource(_resource(format="XLSX"), transport=transport, settings=SETTINGS)
    assert transport.calls == []


def test_missing_url_refused() -> None:
    """Six catalog resources have no URL."""
    with pytest.raises(UnsupportedResourceError):
        fetch_resource(_resource(url=None), transport=FakeTransport(), settings=SETTINGS)


def test_params_limit_bounds_rows() -> None:
    """Phase 4's clamped limit is honoured rather than ignored."""
    body = b"a\n" + b"".join(b"%d\n" % i for i in range(50))
    transport = FakeTransport(byte_responses={"x.csv": _raw(body)})
    table = fetch_resource(_resource(), transport=transport, settings=SETTINGS,
                           params=QueryParams(limit=5))
    assert table.row_count == 5
    assert table.incomplete_reason is IncompleteReason.ROW_CAP


def test_deferred_params_are_recorded() -> None:
    """Params this layer does not apply are surfaced so Phase 6 can filter uniformly."""
    transport = FakeTransport(byte_responses={"x.csv": _raw(CSV)})
    table = fetch_resource(_resource(), transport=transport, settings=SETTINGS,
                           params=QueryParams(date_from="2024-01-01", region="Αττική"))
    assert table.deferred_params == {"date_from": "2024-01-01", "region": "Αττική"}


def test_datastore_paging_sends_sort_and_pages() -> None:
    """DataStore paging uses a stable sort; without it deep offsets drift."""
    payload = {
        "success": True,
        "result": {"total": 2, "fields": [{"id": "_id", "type": "int4"},
                                          {"id": "n", "type": "int4"}],
                   "records": [{"n": 1}, {"n": 2}]},
    }
    transport = FakeTransport(json_responses={"datastore_search": payload})
    table = fetch_resource(_resource(datastore_active=True), transport=transport,
                           settings=SETTINGS)
    assert table.access_path == "datastore"
    assert table.upstream_total == 2
    assert [c.name for c in table.columns] == ["n"]  # synthetic _id dropped
    assert table.rows == [{"n": "1"}, {"n": "2"}]
    assert transport.calls[0][1]["sort"] == "_id asc"


def test_datastore_404_falls_back_to_download_and_caches_correctly() -> None:
    """A stale datastore_active flag falls back, and the cache replays as CSV.

    Regression: caching under the live flag meant the next hit fed CSV bytes to the JSON
    parser forever.
    """
    transport = FakeTransport(
        json_responses={"datastore_search": {"success": False, "error": {}}},
        byte_responses={"x.csv": _raw(CSV)},
    )
    conn = _cache_conn()
    resource = _resource(datastore_active=True)
    first = fetch_resource(resource, transport=transport, cache_conn=conn, settings=SETTINGS)
    assert first.access_path == "download"

    transport.calls.clear()
    second = fetch_resource(resource, transport=transport, cache_conn=conn, settings=SETTINGS)
    assert second.from_cache is True
    assert second.access_path == "download"
    assert second.rows == first.rows
    assert transport.calls == []  # a cache hit performs zero network calls


def test_incomplete_body_is_not_cached() -> None:
    """A byte-capped body must not be replayed as the complete resource."""
    transport = FakeTransport(byte_responses={"x.csv": _raw(CSV, complete=False)})
    conn = _cache_conn()
    fetch_resource(_resource(), transport=transport, cache_conn=conn, settings=SETTINGS)
    assert conn.execute("SELECT count(*) FROM response_cache").fetchone()[0] == 0


def test_transport_failure_is_typed() -> None:
    """Upstream failure surfaces as ResourceUnavailableError, not an httpx error."""
    transport = FakeTransport(errors={"x.csv": ResourceUnavailableError("boom")})
    with pytest.raises(ResourceUnavailableError):
        fetch_resource(_resource(), transport=transport, settings=SETTINGS)


def _catalog() -> sqlite3.Connection:
    """In-memory catalog with one dataset + resource, for plan-level tests."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.upsert_dataset(conn, DatasetRow(
        id="ds-1", name="ds-1", title="Τροχαία", title_en=None, notes=None, notes_en=None,
        org_name="min", org_title="Υπουργείο Υποδομών", license_id=None, license_title=None,
        frequency=None, language_options=[], theme=[], num_resources=1, tags=[],
        temporal_start=None, temporal_end=None, spatial_text=None, metadata_created=None,
        last_updated="2026-01-01T00:00:00", state="active",
        harvested_at="2026-06-01T00:00:00", embed_text="x",
    ))
    db.upsert_resources(conn, [_resource()])
    conn.commit()
    return conn


def _plan(status: PlanStatus, resource_id: str | None = "res-1") -> QueryPlan:
    """Build a minimal plan in the given status."""
    return QueryPlan(
        question="q", normalized_question="q", language="el", status=status, dataset=None,
        resource_id=resource_id, resource_format="CSV", resource_url="https://data.gov.gr/x.csv",
        access_path="download", params=QueryParams(), confidence=0.9, reason="", degraded=False,
        candidates=[],
    )


def test_fetch_for_plan_carries_publisher_provenance() -> None:
    """Principle #2: the footer needs publisher, and nothing else in the pipeline carries it."""
    transport = FakeTransport(byte_responses={"x.csv": _raw(CSV)})
    table = fetch_for_plan(_plan(PlanStatus.MATCHED), conn=_catalog(), transport=transport,
                           settings=SETTINGS)
    assert table.publisher == "Υπουργείο Υποδομών"
    assert table.dataset_title == "Τροχαία"
    assert table.last_updated == "2026-01-01T00:00:00"


@pytest.mark.parametrize("status", [PlanStatus.NO_MATCH, PlanStatus.UNSUPPORTED])
def test_fetch_for_plan_refuses_unmatched(status: PlanStatus) -> None:
    """A non-MATCHED plan is NoMatchError, distinct from 'bad format'."""
    with pytest.raises(NoMatchError):
        fetch_for_plan(_plan(status), conn=_catalog(), transport=FakeTransport(),
                       settings=SETTINGS)


def test_fetch_for_plan_missing_resource_raises() -> None:
    """A plan pointing at a resource the catalog lost must not substitute another."""
    with pytest.raises(UnsupportedResourceError):
        fetch_for_plan(_plan(PlanStatus.MATCHED, resource_id="gone"), conn=_catalog(),
                       transport=FakeTransport(), settings=SETTINGS)


def test_complete_flag_cannot_lie() -> None:
    """TableData rejects an inconsistent completeness claim at construction."""
    with pytest.raises(ValueError):
        TableData(resource_id="r", dataset_id="d", columns=[], rows=[], row_count=0,
                  complete=False, incomplete_reason=None, header_trusted=True,
                  access_path="download", source_url="u", fetched_at="t")
    with pytest.raises(ValueError):
        TableData(resource_id="r", dataset_id="d", columns=[], rows=[], row_count=0,
                  complete=True, incomplete_reason=IncompleteReason.ROW_CAP,
                  header_trusted=True, access_path="download", source_url="u", fetched_at="t")


def test_header_trust_has_no_default() -> None:
    """A construction site must state whether the header is trustworthy.

    Same guard as ``complete``: a defaulted ``header_trusted=True`` would let every forgotten
    call site silently assert that banner-derived column names are the publisher's own.
    """
    with pytest.raises(TypeError):
        TableData(resource_id="r", dataset_id="d", columns=[], rows=[],  # type: ignore[call-arg]
                  row_count=0, complete=True, access_path="download",
                  source_url="u", fetched_at="t")


def test_access_log_never_contains_a_url(caplog: pytest.LogCaptureFixture) -> None:
    """The structured trace carries ids and counts, never a URL that could be signed."""
    transport = FakeTransport(byte_responses={"x.csv": _raw(CSV)})
    with caplog.at_level(logging.INFO):
        fetch_resource(_resource(), transport=transport, settings=SETTINGS)
    for record in caplog.records:
        rendered = str(getattr(record, "extra_fields", {})) + record.getMessage()
        assert "http" not in rendered
