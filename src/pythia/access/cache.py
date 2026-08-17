"""SQLite response cache for fetched resource bodies (Phase 5, ADR-0006).

Three properties are load-bearing and were each a bug in the first draft of the spec:

* **Incomplete bodies are never stored.** A 25 MB slice of a 174 MB file cached under a
  long-lived key would be served as the complete resource indefinitely.
* **A TTL ceiling applies to every row**, not just to rows with no freshness field. 74% of
  resources key on ``metadata_modified``, which is catalog-level and does not move when a
  file is replaced in place — without a ceiling they would never be re-fetched.
* **The path and format are stored**, not re-derived from ``datastore_active``, so a
  download body cached after a DataStore 404 is parsed as the CSV it actually is.
"""

from __future__ import annotations

import sqlite3
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pythia.ingest.db import connect
from pythia.ingest.models import ResourceRow

_SCHEMA_PATH = Path(__file__).parent / "cache_schema.sql"

# Bump when sniff/parse behaviour changes in a way that invalidates cached-body parses.
# v2: parse_csv skips banner/continuation/footnote rows, so a cached body parses to a
# different header and row set than it did under v1.
PARSER_VERSION = 2


@dataclass(frozen=True)
class CacheKey:
    """Which catalog field established freshness, and its value."""

    field: str
    value: str


@dataclass(frozen=True)
class CachedResponse:
    """A previously fetched body plus how it was obtained."""

    body: bytes
    access_path: str
    resource_format: str | None
    bytes_read: int


def connect_cache(path: str | Path) -> sqlite3.Connection:
    """Open the cache DB with WAL and a busy timeout (Phase 7 will read it concurrently)."""
    conn = connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_cache_db(conn: sqlite3.Connection) -> None:
    """Apply the committed cache schema; idempotent."""
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))


def freshness_key(resource: ResourceRow) -> CacheKey | None:
    """Return the freshness key for a resource, or ``None`` if it has no timestamp.

    Measured: no catalog resource lacks both fields, so ``None`` is defensive only — and a
    resource without any freshness signal must not be cached at all.
    """
    if resource.last_modified:
        return CacheKey("last_modified", resource.last_modified)
    if resource.metadata_modified:
        return CacheKey("metadata_modified", resource.metadata_modified)
    return None


def get(conn: sqlite3.Connection, resource: ResourceRow, *, ttl_s: int) -> CachedResponse | None:
    """Return a cached body, or ``None`` on miss/expiry/corruption.

    Any cache failure degrades to a miss: a corrupt BLOB or a missing table must cause a
    refetch, never an exception on the answer path.
    """
    key = freshness_key(resource)
    if key is None:
        return None
    try:
        row = conn.execute(
            """SELECT body, access_path, resource_format, bytes_read, cached_at, parser_version
               FROM response_cache
               WHERE resource_id = ? AND key_field = ? AND key_value = ?""",
            (resource.id, key.field, key.value),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or int(row[5]) != PARSER_VERSION:
        return None
    if _expired(str(row[4]), ttl_s):
        return None
    try:
        body = zlib.decompress(row[0])
    except zlib.error:
        return None
    _touch(conn, resource.id, key)
    return CachedResponse(
        body=body, access_path=str(row[1]),
        resource_format=None if row[2] is None else str(row[2]), bytes_read=int(row[3]),
    )


def put(conn: sqlite3.Connection, resource: ResourceRow, *, body: bytes, access_path: str,
        complete: bool, max_total_bytes: int) -> None:
    """Store a body, unless it is incomplete or the resource has no freshness key."""
    if not complete:
        return  # never cache a partial body: it would be served as the whole resource
    key = freshness_key(resource)
    if key is None:
        return
    now = datetime.now(UTC).isoformat()
    try:
        # One row per resource: superseded freshness keys are replaced, not accumulated.
        conn.execute("DELETE FROM response_cache WHERE resource_id = ?", (resource.id,))
        conn.execute(
            """INSERT INTO response_cache
               (resource_id, key_field, key_value, access_path, resource_format, body,
                bytes_read, parser_version, cached_at, last_accessed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (resource.id, key.field, key.value, access_path, resource.format,
             zlib.compress(body), len(body), PARSER_VERSION, now, now),
        )
        evict(conn, max_total_bytes=max_total_bytes)
    except sqlite3.Error:
        return  # a cache write failure must never fail the fetch


def evict(conn: sqlite3.Connection, *, max_total_bytes: int) -> int:
    """Drop least-recently-used rows until the cache fits; return rows removed."""
    total = conn.execute(
        "SELECT coalesce(sum(length(body)), 0) FROM response_cache"
    ).fetchone()[0]
    if total <= max_total_bytes:
        return 0
    removed = 0
    for resource_id, field, value, size in conn.execute(
        """SELECT resource_id, key_field, key_value, length(body)
           FROM response_cache ORDER BY last_accessed ASC"""
    ).fetchall():
        conn.execute(
            """DELETE FROM response_cache
               WHERE resource_id = ? AND key_field = ? AND key_value = ?""",
            (resource_id, field, value),
        )
        removed += 1
        total -= size
        if total <= max_total_bytes:
            break
    return removed


def purge_expired(conn: sqlite3.Connection, *, ttl_s: int) -> int:
    """Delete every row past the TTL ceiling; return rows removed."""
    cutoff = (datetime.now(UTC) - timedelta(seconds=ttl_s)).isoformat()
    cursor = conn.execute("DELETE FROM response_cache WHERE cached_at < ?", (cutoff,))
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def _expired(cached_at: str, ttl_s: int) -> bool:
    """Return whether a cached row is past the TTL ceiling."""
    try:
        stamp = datetime.fromisoformat(cached_at)
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (datetime.now(UTC) - stamp).total_seconds() > ttl_s


def _touch(conn: sqlite3.Connection, resource_id: str, key: CacheKey) -> None:
    """Record an access for LRU purposes; failures are irrelevant to correctness."""
    try:
        conn.execute(
            """UPDATE response_cache SET last_accessed = ?
               WHERE resource_id = ? AND key_field = ? AND key_value = ?""",
            (datetime.now(UTC).isoformat(), resource_id, key.field, key.value),
        )
    except sqlite3.Error:
        return
