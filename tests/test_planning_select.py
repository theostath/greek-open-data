"""Tests for deterministic CSV/JSON resource selection (Phase 4)."""

from __future__ import annotations

import sqlite3

from pythia.ingest import db
from pythia.ingest.models import DatasetRow, ResourceRow
from pythia.planning.select import access_path, select_resource


def _dataset(id_: str) -> DatasetRow:
    """Build a minimal dataset row so resources can reference it (FK on)."""
    return DatasetRow(
        id=id_, name=f"name-{id_}", title=None, title_en=None, notes=None, notes_en=None,
        org_name=None, org_title=None, license_id=None, license_title=None, frequency=None,
        language_options=[], theme=[], num_resources=0, tags=[], temporal_start=None,
        temporal_end=None, spatial_text=None, metadata_created=None, last_updated=None,
        state="active", harvested_at="2026-06-01T00:00:00", embed_text="x",
    )


def _resource(
    id_: str, dataset_id: str, fmt: str, *, datastore: bool = False,
    position: int | None = 0, size: int | None = 10, is_tabular: bool = True,
    state: str | None = "active",
) -> ResourceRow:
    """Build a resource row with the fields that drive selection."""
    return ResourceRow(
        id=id_, dataset_id=dataset_id, name=id_, description=None, format=fmt,
        mimetype=None, url=f"https://data.gov.gr/download/{id_}.{fmt.lower()}", size=size,
        datastore_active=datastore, position=position, last_modified=None,
        metadata_modified=None, state=state, is_tabular=is_tabular,
    )


def _catalog(*resources: ResourceRow) -> sqlite3.Connection:
    """Build an in-memory catalog with one dataset and the given resources."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.upsert_dataset(conn, _dataset("ds1"))
    db.upsert_resources(conn, list(resources))
    conn.commit()
    return conn


def test_selects_csv_over_json() -> None:
    """CSV outranks JSON at equal position."""
    conn = _catalog(_resource("r-json", "ds1", "JSON"), _resource("r-csv", "ds1", "CSV"))
    chosen = select_resource(conn, "ds1")
    assert chosen is not None
    assert chosen.id == "r-csv"


def test_prefers_datastore_active() -> None:
    """A DataStore-active resource is preferred and maps to the datastore access path."""
    conn = _catalog(
        _resource("r-file", "ds1", "CSV", datastore=False),
        _resource("r-ds", "ds1", "CSV", datastore=True),
    )
    chosen = select_resource(conn, "ds1")
    assert chosen is not None
    assert chosen.id == "r-ds"
    assert access_path(chosen) == "datastore"


def test_xlsx_only_yields_no_selection() -> None:
    """XLS/XLSX are tabular but out of MVP scope: no CSV/JSON means no selection."""
    conn = _catalog(
        _resource("r-xlsx", "ds1", "XLSX"), _resource("r-xls", "ds1", "XLS")
    )
    assert select_resource(conn, "ds1") is None


def test_non_tabular_ignored() -> None:
    """A PDF/ZIP resource is never selected."""
    conn = _catalog(_resource("r-pdf", "ds1", "PDF", is_tabular=False))
    assert select_resource(conn, "ds1") is None


def test_null_size_sorts_last() -> None:
    """A resource with a known size beats one with NULL size at equal rank."""
    conn = _catalog(
        _resource("r-null", "ds1", "CSV", position=0, size=None),
        _resource("r-sized", "ds1", "CSV", position=0, size=5),
    )
    chosen = select_resource(conn, "ds1")
    assert chosen is not None
    assert chosen.id == "r-sized"


def test_inactive_resource_skipped() -> None:
    """A non-active resource is excluded."""
    conn = _catalog(_resource("r-del", "ds1", "CSV", state="deleted"))
    assert select_resource(conn, "ds1") is None


def test_missing_dataset_returns_none() -> None:
    """An unknown dataset id selects nothing."""
    conn = _catalog(_resource("r-csv", "ds1", "CSV"))
    assert select_resource(conn, "does-not-exist") is None
