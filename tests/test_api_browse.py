"""Tests for the catalogue browse queries (issue #18).

Browse exists because retrieval is the measured ceiling — R@1 is 0.46, so a user who cannot
phrase a question the way a public body would is fighting the weakest component in the system.
Everything here is therefore **deterministic SQL**: no embeddings, no LLM, so a browse result
cannot be wrong in the way a retrieval result can.

The load-bearing rule is the CSV/JSON filter. Only 24.4% of datasets publish anything tabular,
so a browse that ignored that would be a machine for generating `unsupported` refusals.
"""

from __future__ import annotations

import json
import sqlite3

from pythia.api.browse import (
    PublisherKind,
    classify_publisher,
    count_datasets,
    list_datasets,
    list_publishers,
    list_themes,
)
from pythia.ingest import db
from pythia.ingest.models import DatasetRow, ResourceRow

_DCAT = "http://publications.europa.eu/resource/authority/data-theme"


def _dataset(id_: str, org: str, themes: list[str] | None = None) -> DatasetRow:
    """A dataset row carrying a publisher and DCAT themes."""
    return DatasetRow(
        id=id_, name=f"slug-{id_}", title=f"Τίτλος {id_}", title_en=None, notes=None,
        notes_en=None, org_name=None, org_title=org, license_id=None, license_title=None,
        frequency=None, language_options=[],
        theme=[f"{_DCAT}/{code}" for code in (themes or [])],
        num_resources=1, tags=[], temporal_start=None, temporal_end=None, spatial_text=None,
        metadata_created=None, last_updated="2026-01-01T00:00:00", state="active",
        harvested_at="2026-06-01T00:00:00", embed_text="x",
    )


def _resource(dataset_id: str, fmt: str, position: int = 0) -> ResourceRow:
    """One resource of a given declared format."""
    return ResourceRow(
        id=f"res-{dataset_id}-{fmt}-{position}", dataset_id=dataset_id, name="r",
        description=None, format=fmt, mimetype=None, url="https://example.org/r", size=1,
        datastore_active=False, position=position, last_modified=None, metadata_modified=None,
        state="active", is_tabular=True,
    )


def _catalog(*rows: tuple[str, str, list[str], list[str]]) -> sqlite3.Connection:
    """In-memory catalogue from (dataset_id, org_title, themes, resource_formats)."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    for dataset_id, org, themes, formats in rows:
        db.upsert_dataset(conn, _dataset(dataset_id, org, themes))
        db.upsert_resources(
            conn, [_resource(dataset_id, fmt, i) for i, fmt in enumerate(formats)]
        )
    conn.commit()
    return conn


# ---- publisher classification -------------------------------------------------------------


def test_a_municipality_is_recognised_from_its_title() -> None:
    """Greek open data is published by municipality — this is the geographic signal."""
    assert classify_publisher("Δήμος Χανίων") is PublisherKind.MUNICIPALITY


def test_a_region_is_recognised() -> None:
    assert classify_publisher("Περιφέρεια Κρήτης") is PublisherKind.REGION
    assert classify_publisher("Περιφερειακή Ενότητα Ηρακλείου") is PublisherKind.REGION


def test_a_ministry_is_recognised_anywhere_in_the_title() -> None:
    """"Υπουργείο" is rarely the first word — it follows the portfolio name."""
    assert classify_publisher("Υπουργείο Υγείας") is PublisherKind.MINISTRY
    assert classify_publisher("Γενική Γραμματεία, Υπουργείο Παιδείας") is PublisherKind.MINISTRY


def test_anything_else_is_national_rather_than_guessed_at() -> None:
    """Agencies and authorities have no reliable place marker; do not invent one."""
    assert classify_publisher("Ελληνική Στατιστική Αρχή (ΕΛΣΤΑΤ)") is PublisherKind.NATIONAL
    assert classify_publisher("") is PublisherKind.NATIONAL


def test_classification_tolerates_leading_whitespace() -> None:
    assert classify_publisher("  Δήμος Αγρινίου") is PublisherKind.MUNICIPALITY


# ---- publishers ---------------------------------------------------------------------------


def test_only_publishers_with_tabular_data_are_listed() -> None:
    """A publisher whose every dataset is a PDF is a dead end, so it is not offered."""
    conn = _catalog(
        ("ds-1", "Δήμος Χανίων", [], ["CSV"]),
        ("ds-2", "Δήμος Μόνο-PDF", [], ["PDF"]),
    )

    names = [p.name for p in list_publishers(conn)]

    assert "Δήμος Χανίων" in names
    assert "Δήμος Μόνο-PDF" not in names


def test_publisher_counts_count_only_tabular_datasets() -> None:
    """Showing 2,228 and then listing 14 would be a lie of exactly the kind this product avoids."""
    conn = _catalog(
        ("ds-1", "Δήμος Χανίων", [], ["CSV"]),
        ("ds-2", "Δήμος Χανίων", [], ["JSON"]),
        ("ds-3", "Δήμος Χανίων", [], ["PDF"]),
    )

    publisher = next(p for p in list_publishers(conn) if p.name == "Δήμος Χανίων")

    assert publisher.dataset_count == 2


def test_publishers_are_ordered_by_how_much_they_actually_publish() -> None:
    conn = _catalog(
        ("ds-1", "Μικρός Δήμος", [], ["CSV"]),
        ("ds-2", "Δήμος Χανίων", [], ["CSV"]),
        ("ds-3", "Δήμος Χανίων", [], ["CSV"]),
    )

    assert [p.name for p in list_publishers(conn)][0] == "Δήμος Χανίων"


def test_a_publisher_carries_its_kind_for_grouping() -> None:
    conn = _catalog(("ds-1", "Περιφέρεια Κρήτης", [], ["CSV"]))

    assert list_publishers(conn)[0].kind is PublisherKind.REGION


# ---- themes -------------------------------------------------------------------------------


def test_themes_are_parsed_from_the_dcat_uri_array() -> None:
    """`theme` is a JSON array of DCAT URIs; the code is the last path segment."""
    conn = _catalog(("ds-1", "Δήμος Χανίων", ["REGI", "ENVI"], ["CSV"]))

    codes = {t.code for t in list_themes(conn)}

    assert codes == {"REGI", "ENVI"}


def test_a_theme_carries_a_human_label_because_REGI_means_nothing() -> None:
    conn = _catalog(("ds-1", "Δήμος Χανίων", ["REGI"], ["CSV"]))

    assert list_themes(conn)[0].label == "Regions and cities"


def test_an_unknown_theme_code_falls_back_to_the_code_rather_than_crashing() -> None:
    """Upstream can add a theme at any time; an unknown one must not break the page."""
    conn = _catalog(("ds-1", "Δήμος Χανίων", ["WEIRD"], ["CSV"]))

    theme = next(t for t in list_themes(conn) if t.code == "WEIRD")

    assert theme.label == "WEIRD"


def test_themes_only_count_tabular_datasets() -> None:
    conn = _catalog(
        ("ds-1", "Δήμος Χανίων", ["REGI"], ["CSV"]),
        ("ds-2", "Δήμος Χανίων", ["REGI"], ["PDF"]),
    )

    assert next(t for t in list_themes(conn) if t.code == "REGI").dataset_count == 1


def test_malformed_theme_json_is_skipped_not_fatal() -> None:
    """Harvested text is third-party content; one bad row must not take out the page."""
    conn = _catalog(("ds-1", "Δήμος Χανίων", ["REGI"], ["CSV"]))
    conn.execute("UPDATE datasets SET theme = ? WHERE id = ?", ("{not json", "ds-1"))
    conn.commit()

    assert list_themes(conn) == []


# ---- datasets -----------------------------------------------------------------------------


def test_only_datasets_with_a_csv_or_json_resource_are_listed() -> None:
    """The whole point: browse must never lead to an `unsupported` refusal."""
    conn = _catalog(
        ("ds-1", "Δήμος Χανίων", [], ["CSV"]),
        ("ds-2", "Δήμος Χανίων", [], ["PDF", "XLSX"]),
    )

    assert [d.id for d in list_datasets(conn)] == ["ds-1"]


def test_a_dataset_carries_a_resource_id_to_pin() -> None:
    """The handoff bypasses retrieval, which is the entire product win here."""
    conn = _catalog(("ds-1", "Δήμος Χανίων", [], ["CSV"]))

    assert list_datasets(conn)[0].resource_id == "res-ds-1-CSV-0"


def test_the_pinned_resource_is_the_one_planning_would_have_chosen() -> None:
    """One selection rule across the product: browse reuses planning.select.select_resource."""
    from pythia.planning.select import select_resource

    conn = _catalog(("ds-1", "Δήμος Χανίων", [], ["JSON", "CSV"]))

    chosen = select_resource(conn, "ds-1")
    assert chosen is not None
    assert list_datasets(conn)[0].resource_id == chosen.id


def test_datasets_can_be_filtered_by_publisher() -> None:
    conn = _catalog(
        ("ds-1", "Δήμος Χανίων", [], ["CSV"]),
        ("ds-2", "Δήμος Αγρινίου", [], ["CSV"]),
    )

    assert [d.id for d in list_datasets(conn, publisher="Δήμος Χανίων")] == ["ds-1"]


def test_datasets_can_be_filtered_by_theme() -> None:
    conn = _catalog(
        ("ds-1", "Δήμος Χανίων", ["ENVI"], ["CSV"]),
        ("ds-2", "Δήμος Χανίων", ["EDUC"], ["CSV"]),
    )

    assert [d.id for d in list_datasets(conn, theme="ENVI")] == ["ds-1"]


def test_a_theme_filter_cannot_be_smuggled_into_the_sql() -> None:
    """`theme` reaches a LIKE clause; it is user input from a URL path."""
    conn = _catalog(("ds-1", "Δήμος Χανίων", ["ENVI"], ["CSV"]))

    assert list_datasets(conn, theme="' OR 1=1 --") == []


def test_listing_is_paginated_so_a_625_dataset_publisher_is_not_one_page() -> None:
    conn = _catalog(*[(f"ds-{i}", "Δήμος Χανίων", [], ["CSV"]) for i in range(10)])

    page = list_datasets(conn, limit=4, offset=4)

    assert len(page) == 4
    assert page[0].id not in {d.id for d in list_datasets(conn, limit=4)}


def test_count_matches_what_listing_would_return() -> None:
    """The count drives the pagination UI; disagreeing with the list is a defect."""
    conn = _catalog(
        ("ds-1", "Δήμος Χανίων", [], ["CSV"]),
        ("ds-2", "Δήμος Χανίων", [], ["PDF"]),
        ("ds-3", "Δήμος Αγρινίου", [], ["CSV"]),
    )

    assert count_datasets(conn, publisher="Δήμος Χανίων") == 1
    assert count_datasets(conn) == 2


def test_a_dataset_summary_names_the_formats_it_offers() -> None:
    conn = _catalog(("ds-1", "Δήμος Χανίων", [], ["CSV", "PDF"]))

    assert list_datasets(conn)[0].formats == ["CSV", "PDF"]


def test_theme_json_is_stored_as_the_full_uri_not_the_bare_code() -> None:
    """Guards the fixture itself: filtering assumes URIs, so a bare code would pass vacuously."""
    conn = _catalog(("ds-1", "Δήμος Χανίων", ["ENVI"], ["CSV"]))

    raw = conn.execute("SELECT theme FROM datasets WHERE id = 'ds-1'").fetchone()[0]

    assert json.loads(raw) == [f"{_DCAT}/ENVI"]
