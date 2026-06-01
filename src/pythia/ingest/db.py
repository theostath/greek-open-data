"""SQLite persistence layer for the harvested catalog (Phase 2 ingest).

Opens the database, applies the committed ``schema.sql``, and upserts normalized
``DatasetRow``/``ResourceRow`` records idempotently. List fields are serialized as
JSON text and boolean flags as 0/1 integers, mirroring the schema. Callers control
transactions; these functions never commit.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pythia.ingest.models import DatasetRow, ResourceRow

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_DATASET_COLUMNS = (
    "id",
    "name",
    "title",
    "title_en",
    "notes",
    "notes_en",
    "org_name",
    "org_title",
    "license_id",
    "license_title",
    "frequency",
    "language_options",
    "theme",
    "num_resources",
    "tags",
    "temporal_start",
    "temporal_end",
    "spatial_text",
    "metadata_created",
    "last_updated",
    "state",
    "harvested_at",
    "embed_text",
)

_RESOURCE_COLUMNS = (
    "id",
    "dataset_id",
    "name",
    "description",
    "format",
    "mimetype",
    "url",
    "size",
    "datastore_active",
    "position",
    "last_modified",
    "metadata_modified",
    "state",
    "is_tabular",
)


def _upsert_sql(table: str, columns: tuple[str, ...]) -> str:
    """Build an INSERT ... ON CONFLICT(id) DO UPDATE statement for the columns."""
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c != "id")
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}"
    )


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection, creating parent dirs and enabling foreign keys."""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Apply schema.sql; idempotent thanks to CREATE TABLE IF NOT EXISTS."""
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))


def upsert_dataset(conn: sqlite3.Connection, row: DatasetRow) -> None:
    """Insert or update one dataset row by id (does not commit)."""
    values = (
        row.id,
        row.name,
        row.title,
        row.title_en,
        row.notes,
        row.notes_en,
        row.org_name,
        row.org_title,
        row.license_id,
        row.license_title,
        row.frequency,
        json.dumps(row.language_options),
        json.dumps(row.theme),
        row.num_resources,
        json.dumps(row.tags),
        row.temporal_start,
        row.temporal_end,
        row.spatial_text,
        row.metadata_created,
        row.last_updated,
        row.state,
        row.harvested_at,
        row.embed_text,
    )
    conn.execute(_upsert_sql("datasets", _DATASET_COLUMNS), values)


def upsert_resources(conn: sqlite3.Connection, rows: list[ResourceRow]) -> None:
    """Insert or update many resource rows by id (does not commit)."""
    sql = _upsert_sql("resources", _RESOURCE_COLUMNS)
    params = [
        (
            row.id,
            row.dataset_id,
            row.name,
            row.description,
            row.format,
            row.mimetype,
            row.url,
            row.size,
            int(row.datastore_active),
            row.position,
            row.last_modified,
            row.metadata_modified,
            row.state,
            int(row.is_tabular),
        )
        for row in rows
    ]
    conn.executemany(sql, params)


def count_datasets(conn: sqlite3.Connection) -> int:
    """Return the number of rows in the datasets table."""
    count: int = conn.execute("SELECT count(*) FROM datasets").fetchone()[0]
    return count
