"""Typed row models for the catalog SQLite store — the Phase 2 ingest contract.

These mirror the columns in ``schema.sql``. ``normalize`` produces them from raw
CKAN package dicts; ``db`` persists them. List fields are stored as JSON text and
the boolean flags as 0/1 by the ``db`` layer, not here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DatasetRow:
    """One normalized dataset, mirroring the ``datasets`` table."""

    id: str
    name: str
    title: str | None
    title_en: str | None
    notes: str | None
    notes_en: str | None
    org_name: str | None
    org_title: str | None
    license_id: str | None
    license_title: str | None
    frequency: str | None
    language_options: list[str]
    theme: list[str]
    num_resources: int
    tags: list[str]
    temporal_start: str | None
    temporal_end: str | None
    spatial_text: str | None
    metadata_created: str | None
    last_updated: str | None  # CKAN metadata_modified
    state: str | None
    harvested_at: str
    embed_text: str


@dataclass(frozen=True)
class ResourceRow:
    """One normalized resource, mirroring the ``resources`` table."""

    id: str
    dataset_id: str
    name: str | None
    description: str | None
    format: str | None
    mimetype: str | None
    url: str | None
    size: int | None
    datastore_active: bool
    position: int | None
    last_modified: str | None
    metadata_modified: str | None
    state: str | None
    is_tabular: bool
