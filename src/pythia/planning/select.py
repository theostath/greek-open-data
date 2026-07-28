"""Deterministic per-dataset resource selection (Phase 4).

Picks the single resource Phase 5 should fetch, using only catalog metadata — no LLM, no
network. MVP eligibility is **CSV/JSON only** (``api_findings.md §3``): a dataset whose
tabular resources are all XLS/XLSX yields no selection, which the planner reports as
``UNSUPPORTED``. Keys strictly on dataset ``id`` (the ``name`` slug is non-unique upstream).
"""

from __future__ import annotations

import sqlite3

from pythia.ingest.models import ResourceRow

_SUPPORTED_FORMATS = ("CSV", "JSON")

# CSV before JSON; DataStore-active first; then earliest position, smallest size (NULLS
# LAST), and finally id for a fully deterministic tiebreak.
_SELECT_SQL = """
SELECT id, dataset_id, name, description, format, mimetype, url, size,
       datastore_active, position, last_modified, metadata_modified, state, is_tabular
FROM resources
WHERE dataset_id = ?
  AND is_tabular = 1
  AND upper(format) IN ('CSV', 'JSON')
  AND (state IS NULL OR state = 'active')
ORDER BY datastore_active DESC,
         CASE upper(format) WHEN 'CSV' THEN 0 WHEN 'JSON' THEN 1 ELSE 2 END,
         position IS NULL, position ASC,
         size IS NULL, size ASC,
         id ASC
LIMIT 1
"""


def select_resource(conn: sqlite3.Connection, dataset_id: str) -> ResourceRow | None:
    """Return the best CSV/JSON resource for ``dataset_id``, or ``None`` if none qualifies."""
    row = conn.execute(_SELECT_SQL, (dataset_id,)).fetchone()
    if row is None:
        return None
    return ResourceRow(
        id=row[0],
        dataset_id=row[1],
        name=row[2],
        description=row[3],
        format=row[4],
        mimetype=row[5],
        url=row[6],
        size=row[7],
        datastore_active=bool(row[8]),
        position=row[9],
        last_modified=row[10],
        metadata_modified=row[11],
        state=row[12],
        is_tabular=bool(row[13]),
    )


def access_path(resource: ResourceRow) -> str:
    """Map a resource to its Phase 5 access path: ``datastore`` or ``download``."""
    return "datastore" if resource.datastore_active else "download"
