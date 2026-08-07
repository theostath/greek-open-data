"""Route tests for the browse surface (issue #18).

These use a real temporary catalogue rather than a fake, because the whole point of browse is
that it is deterministic SQL — stubbing the queries would test nothing that matters.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from config import Settings
from fastapi.testclient import TestClient
from tests.api_harness import ORIGIN, Inline, answered

from pythia.api.app import create_app, get_jobs, get_pipeline, get_settings_dep
from pythia.api.jobs import JobStore
from pythia.ingest import db
from pythia.ingest.models import DatasetRow, ResourceRow

_DCAT = "http://publications.europa.eu/resource/authority/data-theme"


def _dataset(id_: str, org: str, title: str, themes: list[str]) -> DatasetRow:
    return DatasetRow(
        id=id_, name=f"slug-{id_}", title=title, title_en=None, notes=None, notes_en=None,
        org_name=None, org_title=org, license_id=None, license_title=None, frequency=None,
        language_options=[], theme=[f"{_DCAT}/{c}" for c in themes], num_resources=1, tags=[],
        temporal_start=None, temporal_end=None, spatial_text=None, metadata_created=None,
        last_updated="2026-01-01T00:00:00", state="active", harvested_at="2026-06-01T00:00:00",
        embed_text="x",
    )


def _resource(dataset_id: str, fmt: str) -> ResourceRow:
    return ResourceRow(
        id=f"res-{dataset_id}", dataset_id=dataset_id, name="r", description=None, format=fmt,
        mimetype=None, url="https://example.org/r", size=1, datastore_active=False, position=0,
        last_modified=None, metadata_modified=None, state="active", is_tabular=True,
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, JobStore]]:
    """A client over a real on-disk catalogue — browse opens it read-only, per request."""
    catalog = tmp_path / "catalog.sqlite"
    conn = db.connect(str(catalog))
    db.init_db(conn)
    rows = [
        ("ds-1", "Δήμος Χανίων", "Αρχαιολογικοί χώροι Χανίων", ["REGI"], "CSV"),
        ("ds-2", "Δήμος Χανίων", "Σημεία ανακύκλωσης", ["ENVI"], "JSON"),
        ("ds-3", "Περιφέρεια Κρήτης", "Δείκτης ανεργίας", ["SOCI"], "CSV"),
        ("ds-4", "Δήμος Χανίων", "Μόνο σε PDF", ["REGI"], "PDF"),
    ]
    for dataset_id, org, title, themes, fmt in rows:
        db.upsert_dataset(conn, _dataset(dataset_id, org, title, themes))
        db.upsert_resources(conn, [_resource(dataset_id, fmt)])
    conn.commit()
    conn.close()

    settings = Settings(catalog_db_path=str(catalog))
    store = JobStore(settings, run=lambda q, rid, on_stage: answered(), executor=Inline())
    app = create_app(lifespan_handler=None)
    app.dependency_overrides[get_settings_dep] = lambda: settings
    app.dependency_overrides[get_jobs] = lambda: store
    app.dependency_overrides[get_pipeline] = lambda: None
    yield TestClient(app), store


def test_explore_lists_publishers_grouped_by_kind(
    client: tuple[TestClient, JobStore]
) -> None:
    """Geography comes from the publishing body, which is the finding this surface rests on."""
    body = client[0].get("/explore").text

    assert "Municipalities" in body and "Regions" in body
    assert "Δήμος Χανίων" in body and "Περιφέρεια Κρήτης" in body


def test_explore_counts_only_readable_datasets(
    client: tuple[TestClient, JobStore]
) -> None:
    """Δήμος Χανίων has three datasets but only two Pythia can read."""
    body = client[0].get("/explore").text

    marker = body.split("Δήμος Χανίων")[1]
    assert ">2<" in marker.split("</li>")[0], "the count must exclude the PDF-only dataset"


def test_explore_lists_themes_with_human_labels(
    client: tuple[TestClient, JobStore]
) -> None:
    body = client[0].get("/explore").text

    assert "Regions and cities" in body and "Environment" in body
    assert ">REGI<" not in body, "a bare DCAT code means nothing to a reader"


def test_a_publisher_listing_excludes_unreadable_datasets(
    client: tuple[TestClient, JobStore]
) -> None:
    """The load-bearing rule: browse must never lead to an `unsupported` refusal."""
    body = client[0].get("/explore/datasets", params={"publisher": "Δήμος Χανίων"}).text

    assert "Αρχαιολογικοί χώροι Χανίων" in body
    assert "Σημεία ανακύκλωσης" in body
    assert "Μόνο σε PDF" not in body


def test_a_theme_listing_filters_correctly(client: tuple[TestClient, JobStore]) -> None:
    body = client[0].get("/explore/datasets", params={"theme": "ENVI"}).text

    assert "Σημεία ανακύκλωσης" in body
    assert "Αρχαιολογικοί χώροι Χανίων" not in body


def test_each_dataset_offers_a_pinned_ask(client: tuple[TestClient, JobStore]) -> None:
    """Pinning is the product win: it bypasses retrieval, the measured ceiling."""
    body = client[0].get("/explore/datasets", params={"publisher": "Δήμος Χανίων"}).text

    assert 'name="resource_id"' in body
    assert 'value="res-ds-1"' in body
    assert 'hx-post="/ask"' in body


def test_a_pinned_ask_reaches_the_job_store_with_its_resource(
    client: tuple[TestClient, JobStore]
) -> None:
    test_client, store = client

    test_client.post("/ask", data={"question": "Αρχαιολογικοί χώροι", "resource_id": "res-ds-1"},
                     headers={"Origin": ORIGIN})

    job = next(iter(store._jobs.values()))
    assert job.resource_id == "res-ds-1"


def test_a_malformed_resource_id_is_rejected_before_the_pipeline(
    client: tuple[TestClient, JobStore]
) -> None:
    """A miss inside the worker surfaces as "the publisher failed", blaming the wrong party."""
    response = client[0].post(
        "/ask", data={"question": "q", "resource_id": "../../etc/passwd"},
        headers={"Origin": ORIGIN},
    )

    assert response.status_code == 400
    assert "isn't valid" in response.text


def test_an_unpinned_ask_still_works(client: tuple[TestClient, JobStore]) -> None:
    """Browse is additive; the plain question path must be untouched."""
    test_client, store = client

    response = test_client.post("/ask", data={"question": "πόσες πυρκαγιές;"},
                                headers={"Origin": ORIGIN})

    assert response.status_code == 200
    assert next(iter(store._jobs.values())).resource_id is None


def test_the_landing_page_points_at_browse(client: tuple[TestClient, JobStore]) -> None:
    """A user who does not know what is answerable needs a door other than the empty box."""
    assert 'href="/explore"' in client[0].get("/").text


def test_greek_publisher_names_survive_the_query_string(
    client: tuple[TestClient, JobStore]
) -> None:
    """Encoding is this project's oldest recurring bug class (§5)."""
    body = client[0].get("/explore/datasets", params={"publisher": "Δήμος Χανίων"}).text

    assert "Δήμος Χανίων" in body
    assert "Ï" not in body and "Î" not in body


def test_browse_pages_carry_the_security_headers(
    client: tuple[TestClient, JobStore]
) -> None:
    headers = client[0].get("/explore").headers

    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"


def test_a_missing_catalogue_does_not_manufacture_an_empty_one(tmp_path: Path) -> None:
    """`db.connect()` creates the file; browse opens read-only so a gap fails visibly."""
    settings = Settings(catalog_db_path=str(tmp_path / "absent.sqlite"))

    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(f"file:{settings.catalog_db_path}?mode=ro", uri=True).execute(
            "SELECT 1 FROM datasets"
        )
    assert not (tmp_path / "absent.sqlite").exists()
