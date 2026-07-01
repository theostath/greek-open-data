"""Normalize raw CKAN package dicts into typed catalog rows (Phase 2 ingest).

Pure functions only: no HTTP, no DB. Maps a ``package_search`` result object to a
:class:`DatasetRow` plus its :class:`ResourceRow` list, deriving ``num_resources``,
``embed_text`` and ``is_tabular``. Non-dataset or non-active packages are skipped.
"""

from __future__ import annotations

from typing import Any

from pythia.ingest.models import DatasetRow, ResourceRow

_TABULAR_FORMATS = {"CSV", "JSON", "XLS", "XLSX"}


def _coerce_size(value: Any) -> int | None:
    """Coerce a CKAN resource size (int, digit-string, or '') to int or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _build_embed_text(
    title: str | None,
    notes: str | None,
    tags: list[str],
    title_en: str | None,
    notes_en: str | None,
) -> str:
    """Join the non-empty bilingual fields into newline-separated embedding text.

    Fields are ordered high-signal-first — both titles then tags, with the long
    descriptions last — so that when the E5 encoder truncates at its 512-token window
    (~1.9% of datasets overflow it), the bilingual title/tag signal survives rather
    than the English translations (which, appended last, would otherwise drop first).
    """
    parts = [title, title_en, " ".join(tags), notes, notes_en]
    return "\n".join(p.strip() for p in parts if p and p.strip())


def _normalize_resource(raw: dict[str, Any], dataset_id: str) -> ResourceRow:
    """Map one raw CKAN resource dict to a :class:`ResourceRow`."""
    fmt: str | None = raw.get("format")
    return ResourceRow(
        id=raw["id"],
        dataset_id=dataset_id,
        name=raw.get("name"),
        description=raw.get("description"),
        format=fmt,
        mimetype=raw.get("mimetype") or raw.get("mimetype_inner"),
        url=raw.get("url"),
        size=_coerce_size(raw.get("size")),
        datastore_active=bool(raw.get("datastore_active")),
        position=raw.get("position"),
        last_modified=raw.get("last_modified"),
        metadata_modified=raw.get("metadata_modified"),
        state=raw.get("state"),
        is_tabular=(fmt or "").strip().upper() in _TABULAR_FORMATS,
    )


def normalize_package(
    raw: dict[str, Any], harvested_at: str
) -> tuple[DatasetRow, list[ResourceRow]] | None:
    """Normalize a raw CKAN package into a dataset row and its resource rows.

    Returns ``None`` when the package is not a ``type='dataset'`` / ``state='active'``
    record and must therefore be skipped.
    """
    if raw.get("type") != "dataset" or raw.get("state") != "active":
        return None

    raw_resources: list[dict[str, Any]] = raw.get("resources") or []
    organization: dict[str, Any] = raw.get("organization") or {}
    title: str | None = raw.get("title")
    title_en: str | None = (raw.get("title_translated") or {}).get("en")
    notes: str | None = raw.get("notes")
    notes_en: str | None = (raw.get("notes_translated") or {}).get("en")
    tags: list[str] = [t["name"] for t in raw.get("tags") or []]

    temporal = raw.get("temporal_coverage") or []
    temporal_start = temporal[0].get("start") if temporal else None
    temporal_end = temporal[0].get("end") if temporal else None
    spatial = raw.get("spatial_coverage") or []
    spatial_text = spatial[0].get("text") if spatial else None

    dataset = DatasetRow(
        id=raw["id"],
        name=raw["name"],
        title=title,
        title_en=title_en,
        notes=notes,
        notes_en=notes_en,
        org_name=organization.get("name"),
        org_title=organization.get("title"),
        license_id=raw.get("license_id"),
        license_title=raw.get("license_title"),
        frequency=raw.get("frequency"),
        language_options=raw.get("language_options") or [],
        theme=raw.get("theme") or [],
        num_resources=len(raw_resources),
        tags=tags,
        temporal_start=temporal_start,
        temporal_end=temporal_end,
        spatial_text=spatial_text,
        metadata_created=raw.get("metadata_created"),
        last_updated=raw.get("metadata_modified"),
        state=raw.get("state"),
        harvested_at=harvested_at,
        embed_text=_build_embed_text(title, notes, tags, title_en, notes_en),
    )
    resources = [_normalize_resource(r, raw["id"]) for r in raw_resources]
    return dataset, resources
