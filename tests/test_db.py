"""Tests for the SQLite persistence layer (Phase 2 ingest)."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from pythia.ingest import db
from pythia.ingest.models import DatasetRow, ResourceRow


def _dataset(id_: str = "ds1", *, title: str | None = "Τίτλος") -> DatasetRow:
    """Build a small DatasetRow for tests."""
    return DatasetRow(
        id=id_,
        name=f"name-{id_}",
        title=title,
        title_en="Title",
        notes="σημειώσεις",
        notes_en="notes",
        org_name="org",
        org_title="Οργανισμός",
        license_id="cc-by",
        license_title="CC BY",
        frequency="annual",
        language_options=["el", "en"],
        theme=["transport"],
        num_resources=2,
        tags=["α", "β"],
        temporal_start="2020-01-01",
        temporal_end="2021-01-01",
        spatial_text="Ελλάδα",
        metadata_created="2020-01-01T00:00:00",
        last_updated="2021-06-01T00:00:00",
        state="active",
        harvested_at="2026-06-01T00:00:00",
        embed_text="embed",
    )


def _resource(id_: str, dataset_id: str = "ds1") -> ResourceRow:
    """Build a small ResourceRow for tests."""
    return ResourceRow(
        id=id_,
        dataset_id=dataset_id,
        name="res",
        description="desc",
        format="CSV",
        mimetype="text/csv",
        url="https://example.gr/r.csv",
        size=1024,
        datastore_active=True,
        position=0,
        last_modified="2021-06-01T00:00:00",
        metadata_modified="2021-06-01T00:00:00",
        state="active",
        is_tabular=True,
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    """An in-memory, initialized database."""
    c = db.connect(":memory:")
    db.init_db(c)
    return c


def test_connect_enables_foreign_keys() -> None:
    """connect turns on foreign key enforcement."""
    c = db.connect(":memory:")
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_connect_creates_parent_dirs(tmp_path: Path) -> None:
    """connect creates missing parent directories for a file path."""
    target = tmp_path / "nested" / "deeper" / "catalog.sqlite"
    c = db.connect(target)
    db.init_db(c)
    assert target.exists()


def test_init_db_creates_tables_and_is_idempotent() -> None:
    """init_db creates the tables and can be called repeatedly."""
    c = db.connect(":memory:")
    db.init_db(c)
    db.init_db(c)  # must not raise
    names = {
        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"datasets", "resources"} <= names


def test_dataset_round_trips(conn: sqlite3.Connection) -> None:
    """List fields round-trip via JSON; the row reads back."""
    db.upsert_dataset(conn, _dataset())
    row = conn.execute(
        "SELECT language_options, theme, tags, num_resources FROM datasets WHERE id=?", ("ds1",)
    ).fetchone()
    assert json.loads(row[0]) == ["el", "en"]
    assert json.loads(row[1]) == ["transport"]
    assert json.loads(row[2]) == ["α", "β"]
    assert row[3] == 2


def test_resource_booleans_round_trip_to_int(conn: sqlite3.Connection) -> None:
    """Boolean flags are stored as 0/1 integers."""
    db.upsert_dataset(conn, _dataset())
    db.upsert_resources(conn, [_resource("r1")])
    row = conn.execute(
        "SELECT datastore_active, is_tabular FROM resources WHERE id=?", ("r1",)
    ).fetchone()
    assert row[0] == 1
    assert row[1] == 1


def test_upsert_dataset_is_idempotent_and_updates(conn: sqlite3.Connection) -> None:
    """Re-upserting the same id keeps one row and applies new values."""
    db.upsert_dataset(conn, _dataset(title="first"))
    db.upsert_dataset(conn, _dataset(title="second"))
    assert db.count_datasets(conn) == 1
    title = conn.execute("SELECT title FROM datasets WHERE id=?", ("ds1",)).fetchone()[0]
    assert title == "second"


def test_upsert_resources_writes_multiple(conn: sqlite3.Connection) -> None:
    """Multiple resources are written under one dataset."""
    db.upsert_dataset(conn, _dataset())
    db.upsert_resources(conn, [_resource("r1"), _resource("r2")])
    n = conn.execute("SELECT count(*) FROM resources WHERE dataset_id=?", ("ds1",)).fetchone()[0]
    assert n == 2


def test_upsert_resources_is_idempotent(conn: sqlite3.Connection) -> None:
    """Re-upserting the same resource id keeps one row."""
    db.upsert_dataset(conn, _dataset())
    db.upsert_resources(conn, [_resource("r1")])
    db.upsert_resources(conn, [_resource("r1")])
    n = conn.execute("SELECT count(*) FROM resources WHERE id=?", ("r1",)).fetchone()[0]
    assert n == 1


def test_resource_foreign_key_enforced(conn: sqlite3.Connection) -> None:
    """A resource referencing a missing dataset is rejected."""
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_resources(conn, [_resource("orphan", dataset_id="missing")])


def test_upsert_allows_duplicate_names(conn: sqlite3.Connection) -> None:
    """Distinct dataset ids may share a slug; the store must not reject the second."""
    db.upsert_dataset(conn, dataclasses.replace(_dataset("a"), name="dup-slug"))
    db.upsert_dataset(conn, dataclasses.replace(_dataset("b"), name="dup-slug"))
    assert db.count_datasets(conn) == 2


def test_count_datasets(conn: sqlite3.Connection) -> None:
    """count_datasets returns the number of dataset rows."""
    assert db.count_datasets(conn) == 0
    db.upsert_dataset(conn, _dataset("a"))
    db.upsert_dataset(conn, _dataset("b"))
    assert db.count_datasets(conn) == 2
