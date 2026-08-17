"""Tests for the Phase 5 response cache."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from pythia.access import cache
from pythia.ingest.models import ResourceRow

BODY = b"a,b\n1,2\n"


def _resource(last_modified: str | None = "2026-01-01T00:00:00",
              metadata_modified: str | None = "2026-01-01T00:00:00") -> ResourceRow:
    """Build a resource row with controllable freshness fields."""
    return ResourceRow(
        id="res-1", dataset_id="ds-1", name="data", description=None, format="CSV",
        mimetype=None, url="https://data.gov.gr/x.csv", size=8, datastore_active=False,
        position=0, last_modified=last_modified, metadata_modified=metadata_modified,
        state="active", is_tabular=True,
    )


def _conn() -> sqlite3.Connection:
    """Open an initialised in-memory cache."""
    conn = sqlite3.connect(":memory:")
    cache.init_cache_db(conn)
    return conn


def test_round_trip_hit() -> None:
    """A stored body comes back byte-identical with its path and format."""
    conn, resource = _conn(), _resource()
    cache.put(conn, resource, body=BODY, access_path="download", complete=True,
              max_total_bytes=10_000_000)
    hit = cache.get(conn, resource, ttl_s=86_400)
    assert hit is not None
    assert hit.body == BODY
    assert hit.access_path == "download"
    assert hit.resource_format == "CSV"


def test_changed_last_modified_is_a_miss() -> None:
    """New freshness value means a refetch."""
    conn = _conn()
    cache.put(conn, _resource(), body=BODY, access_path="download", complete=True,
              max_total_bytes=10_000_000)
    assert cache.get(conn, _resource(last_modified="2026-06-01T00:00:00"), ttl_s=86_400) is None


def test_key_field_is_part_of_the_key() -> None:
    """A body cached under metadata_modified is not served once last_modified appears.

    Collapsing the two fields let an older body be resurrected when last_modified vanished.
    """
    conn = _conn()
    meta_only = _resource(last_modified=None, metadata_modified="2026-01-01T00:00:00")
    cache.put(conn, meta_only, body=BODY, access_path="download", complete=True,
              max_total_bytes=10_000_000)
    with_lm = _resource(last_modified="2026-01-01T00:00:00")
    assert cache.get(conn, with_lm, ttl_s=86_400) is None


def test_incomplete_body_is_never_cached() -> None:
    """A byte-capped slice must not become the canonical body for the resource."""
    conn, resource = _conn(), _resource()
    cache.put(conn, resource, body=BODY, access_path="download", complete=False,
              max_total_bytes=10_000_000)
    assert cache.get(conn, resource, ttl_s=86_400) is None


def test_ttl_ceiling_applies_to_every_row() -> None:
    """Even a last_modified-keyed row expires; content can change without the flag moving."""
    conn, resource = _conn(), _resource()
    cache.put(conn, resource, body=BODY, access_path="download", complete=True,
              max_total_bytes=10_000_000)
    stale = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    conn.execute("UPDATE response_cache SET cached_at = ?", (stale,))
    assert cache.get(conn, resource, ttl_s=2_592_000) is None


def test_parser_version_bump_invalidates() -> None:
    """A parser change must not reuse bodies parsed by the old rules."""
    conn, resource = _conn(), _resource()
    cache.put(conn, resource, body=BODY, access_path="download", complete=True,
              max_total_bytes=10_000_000)
    conn.execute("UPDATE response_cache SET parser_version = parser_version + 1")
    assert cache.get(conn, resource, ttl_s=86_400) is None


def test_corrupt_body_degrades_to_a_miss() -> None:
    """A corrupt BLOB causes a refetch, not an exception on the answer path."""
    conn, resource = _conn(), _resource()
    cache.put(conn, resource, body=BODY, access_path="download", complete=True,
              max_total_bytes=10_000_000)
    conn.execute("UPDATE response_cache SET body = ?", (b"not-zlib",))
    assert cache.get(conn, resource, ttl_s=86_400) is None


def test_missing_table_degrades_to_a_miss() -> None:
    """A deleted/absent cache DB is a miss, not a crash."""
    conn = sqlite3.connect(":memory:")  # never initialised
    assert cache.get(conn, _resource(), ttl_s=86_400) is None


def test_superseded_rows_are_replaced_not_accumulated() -> None:
    """One row per resource, so metadata churn cannot grow the cache without bound."""
    conn = _conn()
    cache.put(conn, _resource(), body=BODY, access_path="download", complete=True,
              max_total_bytes=10_000_000)
    cache.put(conn, _resource(last_modified="2026-06-01T00:00:00"), body=BODY,
              access_path="download", complete=True, max_total_bytes=10_000_000)
    assert conn.execute("SELECT count(*) FROM response_cache").fetchone()[0] == 1


def test_lru_eviction_bounds_growth() -> None:
    """Exceeding the byte budget evicts least-recently-used rows."""
    conn = _conn()
    for index in range(5):
        resource = _resource()
        resource = ResourceRow(**{**resource.__dict__, "id": f"res-{index}"})
        cache.put(conn, resource, body=b"x" * 5000, access_path="download", complete=True,
                  max_total_bytes=10_000_000)
    cache.evict(conn, max_total_bytes=1)
    assert conn.execute("SELECT count(*) FROM response_cache").fetchone()[0] <= 1


def test_purge_expired_removes_old_rows() -> None:
    """The TTL sweep is not scoped to a key type that no resource has."""
    conn, resource = _conn(), _resource()
    cache.put(conn, resource, body=BODY, access_path="download", complete=True,
              max_total_bytes=10_000_000)
    conn.execute("UPDATE response_cache SET cached_at = ?",
                 ((datetime.now(UTC) - timedelta(days=60)).isoformat(),))
    assert cache.purge_expired(conn, ttl_s=2_592_000) == 1
