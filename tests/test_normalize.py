"""Tests for ``pythia.ingest.normalize`` — CKAN package -> typed rows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pythia.ingest.models import DatasetRow, ResourceRow
from pythia.ingest.normalize import normalize_package

HARVESTED_AT = "2026-06-01T00:00:00"
FIXTURE = Path(__file__).parent / "fixtures" / "package_search_sample.json"


@pytest.fixture
def packages() -> list[dict[str, Any]]:
    """Load the real ``package_search`` sample (3 packages)."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = data["result"]["results"]
    return results


@pytest.fixture
def first(packages: list[dict[str, Any]]) -> dict[str, Any]:
    """The first sample package: ``piraeus-enviromental-measurements`` (5 resources)."""
    return packages[0]


def test_first_package_dataset_fields(first: dict[str, Any]) -> None:
    """A real dataset normalizes with core fields populated and provenance set."""
    result = normalize_package(first, HARVESTED_AT)
    assert result is not None
    dataset, resources = result

    assert isinstance(dataset, DatasetRow)
    assert dataset.id == first["id"]
    assert dataset.name == "piraeus-enviromental-measurements"
    assert dataset.title == first["title"]
    assert dataset.title_en == "Enviromental measurements"
    assert dataset.notes == first["notes"]
    assert dataset.notes_en == "Enviromental measurements"
    assert dataset.org_name == "dimospeiraia"
    assert dataset.org_title == first["organization"]["title"]
    assert dataset.last_updated == first["metadata_modified"]
    assert dataset.metadata_created == first["metadata_created"]
    assert dataset.state == "active"
    assert dataset.harvested_at == HARVESTED_AT
    assert len(resources) == 5


def test_num_resources_derived_from_list(first: dict[str, Any]) -> None:
    """``num_resources`` is derived from len(resources), not the raw field."""
    result = normalize_package(first, HARVESTED_AT)
    assert result is not None
    dataset, resources = result
    assert dataset.num_resources == len(first["resources"])
    assert dataset.num_resources == len(resources) == 5


def test_embed_text_contains_title(first: dict[str, Any]) -> None:
    """The bilingual embed text includes the Greek title and the English title."""
    result = normalize_package(first, HARVESTED_AT)
    assert result is not None
    dataset, _ = result
    assert first["title"] in dataset.embed_text
    assert "Enviromental measurements" in dataset.embed_text


def test_embed_text_joins_only_nonempty(first: dict[str, Any]) -> None:
    """Empty tags contribute no blank lines to embed_text."""
    result = normalize_package(first, HARVESTED_AT)
    assert result is not None
    dataset, _ = result
    assert "\n\n" not in dataset.embed_text
    assert not dataset.embed_text.startswith("\n")
    assert not dataset.embed_text.endswith("\n")


def test_embed_text_high_signal_fields_lead() -> None:
    """Both titles and tags precede the long descriptions so they survive truncation."""
    raw = _minimal_dataset(
        title="AAA_title",
        title_translated={"en": "BBB_title_en"},
        tags=[{"name": "CCC_tag"}],
        notes="DDD_notes",
        notes_translated={"en": "EEE_notes_en"},
    )
    result = normalize_package(raw, HARVESTED_AT)
    assert result is not None
    dataset, _ = result
    text = dataset.embed_text
    notes_pos = text.index("DDD_notes")
    assert text.index("AAA_title") < notes_pos
    assert text.index("BBB_title_en") < notes_pos
    assert text.index("CCC_tag") < notes_pos
    assert notes_pos < text.index("EEE_notes_en")


def test_list_fields(first: dict[str, Any]) -> None:
    """List-valued fields are real lists with expected content."""
    result = normalize_package(first, HARVESTED_AT)
    assert result is not None
    dataset, _ = result
    assert dataset.tags == []
    assert dataset.theme == first["theme"]
    assert isinstance(dataset.language_options, list)


def test_temporal_and_spatial_absent(first: dict[str, Any]) -> None:
    """Missing temporal/spatial coverage yields None, not an error."""
    result = normalize_package(first, HARVESTED_AT)
    assert result is not None
    dataset, _ = result
    assert dataset.temporal_start is None
    assert dataset.temporal_end is None
    assert dataset.spatial_text is None


def test_resources_map_correctly(first: dict[str, Any]) -> None:
    """Each resource maps id/dataset_id and the boolean flags by format."""
    result = normalize_package(first, HARVESTED_AT)
    assert result is not None
    _, resources = result

    for r in resources:
        assert isinstance(r, ResourceRow)
        assert r.dataset_id == first["id"]

    by_id = {r.id: r for r in resources}
    raw_by_id = {r["id"]: r for r in first["resources"]}
    for rid, row in by_id.items():
        raw = raw_by_id[rid]
        assert row.datastore_active is bool(raw.get("datastore_active"))
        fmt = (raw.get("format") or "").strip().upper()
        assert row.is_tabular is (fmt in {"CSV", "JSON", "XLS", "XLSX"})


def test_is_tabular_for_csv_and_zip(first: dict[str, Any]) -> None:
    """CSV/JSON are tabular; ZIP is not."""
    result = normalize_package(first, HARVESTED_AT)
    assert result is not None
    _, resources = result
    by_fmt: dict[str, list[ResourceRow]] = {}
    for r in resources:
        by_fmt.setdefault((r.format or "").upper(), []).append(r)

    assert all(r.is_tabular for r in by_fmt.get("CSV", []))
    assert all(r.is_tabular for r in by_fmt.get("JSON", []))
    assert all(not r.is_tabular for r in by_fmt.get("ZIP", []))


def test_datastore_active_true_on_csv(first: dict[str, Any]) -> None:
    """The Piraeus CSV resources are DataStore-active in the fixture."""
    result = normalize_package(first, HARVESTED_AT)
    assert result is not None
    _, resources = result
    csvs = [r for r in resources if (r.format or "").upper() == "CSV"]
    assert csvs
    assert all(r.datastore_active is True for r in csvs)


def test_skip_non_dataset_type() -> None:
    """A non-dataset object (e.g. showcase) is skipped."""
    raw: dict[str, Any] = {"id": "x", "name": "x", "type": "showcase", "state": "active"}
    assert normalize_package(raw, HARVESTED_AT) is None


def test_skip_deleted_state() -> None:
    """A non-active dataset is skipped."""
    raw: dict[str, Any] = {"id": "x", "name": "x", "type": "dataset", "state": "deleted"}
    assert normalize_package(raw, HARVESTED_AT) is None


def _minimal_dataset(**overrides: Any) -> dict[str, Any]:
    """Build a minimal valid dataset dict for edge-case tests."""
    base: dict[str, Any] = {
        "id": "id-1",
        "name": "slug-1",
        "type": "dataset",
        "state": "active",
        "title": "Τίτλος",
        "metadata_modified": "2026-01-01T00:00:00",
    }
    base.update(overrides)
    return base


def test_missing_organization() -> None:
    """A dataset without an organization yields None publisher fields."""
    result = normalize_package(_minimal_dataset(organization=None), HARVESTED_AT)
    assert result is not None
    dataset, resources = result
    assert dataset.org_name is None
    assert dataset.org_title is None
    assert resources == []


def test_missing_organization_key_absent() -> None:
    """Org fields are None when the key is absent entirely."""
    raw = _minimal_dataset()
    raw.pop("organization", None)
    result = normalize_package(raw, HARVESTED_AT)
    assert result is not None
    dataset, _ = result
    assert dataset.org_name is None
    assert dataset.org_title is None


def test_missing_title_translated() -> None:
    """Absent title_translated/notes_translated yield None English fields."""
    result = normalize_package(_minimal_dataset(), HARVESTED_AT)
    assert result is not None
    dataset, _ = result
    assert dataset.title_en is None
    assert dataset.notes_en is None


def test_empty_tags_and_resources() -> None:
    """Empty/absent tags and resources normalize to [] and 0."""
    result = normalize_package(_minimal_dataset(tags=[], resources=[]), HARVESTED_AT)
    assert result is not None
    dataset, resources = result
    assert dataset.tags == []
    assert dataset.num_resources == 0
    assert resources == []


def test_size_digit_string_to_int() -> None:
    """A digit-string size coerces to int."""
    raw = _minimal_dataset(
        resources=[{"id": "r1", "format": "CSV", "size": "1234"}],
    )
    result = normalize_package(raw, HARVESTED_AT)
    assert result is not None
    _, resources = result
    assert resources[0].size == 1234


def test_size_empty_string_to_none() -> None:
    """An empty-string size coerces to None."""
    raw = _minimal_dataset(
        resources=[{"id": "r1", "format": "CSV", "size": ""}],
    )
    result = normalize_package(raw, HARVESTED_AT)
    assert result is not None
    _, resources = result
    assert resources[0].size is None


def test_size_int_passthrough_and_none() -> None:
    """Integer sizes pass through; missing size is None."""
    raw = _minimal_dataset(
        resources=[
            {"id": "r1", "format": "CSV", "size": 99},
            {"id": "r2", "format": "CSV"},
        ],
    )
    result = normalize_package(raw, HARVESTED_AT)
    assert result is not None
    _, resources = result
    assert resources[0].size == 99
    assert resources[1].size is None


def test_mimetype_falls_back_to_inner() -> None:
    """mimetype falls back to mimetype_inner when mimetype is empty."""
    raw = _minimal_dataset(
        resources=[{"id": "r1", "format": "CSV", "mimetype": None, "mimetype_inner": "text/csv"}],
    )
    result = normalize_package(raw, HARVESTED_AT)
    assert result is not None
    _, resources = result
    assert resources[0].mimetype == "text/csv"
