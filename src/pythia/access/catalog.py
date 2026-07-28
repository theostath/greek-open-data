"""Catalog reads the access layer needs (Phase 5).

``get_resource`` fills a real gap: the only pre-existing resource query is
``planning.select.select_resource``, which is keyed on *dataset* id. Fetching by
``resource_id`` — which is what a ``QueryPlan`` carries — had no path.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from pythia.ingest.models import ResourceRow

_RESOURCE_SQL = """
SELECT id, dataset_id, name, description, format, mimetype, url, size,
       datastore_active, position, last_modified, metadata_modified, state, is_tabular
FROM resources
WHERE id = ?
"""

_PROVENANCE_SQL = "SELECT title, org_title, last_updated FROM datasets WHERE id = ?"


@dataclass(frozen=True)
class Provenance:
    """Dataset-level facts every answer must cite (Principle #2)."""

    dataset_title: str | None
    publisher: str | None
    last_updated: str | None


def get_resource(conn: sqlite3.Connection, resource_id: str) -> ResourceRow | None:
    """Return one resource by its own id, or ``None`` if the catalog has no such row."""
    row = conn.execute(_RESOURCE_SQL, (resource_id,)).fetchone()
    if row is None:
        return None
    return ResourceRow(
        id=row[0], dataset_id=row[1], name=row[2], description=row[3], format=row[4],
        mimetype=row[5], url=row[6], size=row[7], datastore_active=bool(row[8]),
        position=row[9], last_modified=row[10], metadata_modified=row[11], state=row[12],
        is_tabular=bool(row[13]),
    )


def get_provenance(conn: sqlite3.Connection, dataset_id: str) -> Provenance:
    """Return title/publisher/last_updated for the footer; blanks if the row is missing."""
    row = conn.execute(_PROVENANCE_SQL, (dataset_id,)).fetchone()
    if row is None:
        return Provenance(dataset_title=None, publisher=None, last_updated=None)
    return Provenance(dataset_title=row[0], publisher=row[1], last_updated=row[2])
