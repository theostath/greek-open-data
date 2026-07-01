"""Tests for hybrid dataset search (dense + lexical, RRF-fused)."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sentence_transformers import SentenceTransformer

from pythia.ingest.db import connect, init_db, upsert_dataset
from pythia.ingest.models import DatasetRow
from pythia.retrieval.embed import build_chroma_index, load_model
from pythia.retrieval.lexical import build_fts_index
from pythia.retrieval.search import Candidate, find_dataset

_TOPICS = {
    "ds-fires": "Δασικές πυρκαγιές ανά έτος και περιοχή στην Ελλάδα",
    "ds-vacc": "Εμβολιασμοί για τον κορονοϊό COVID-19 ανά ημέρα",
    "ds-traffic": "Τροχαία ατυχήματα και οδική ασφάλεια",
    "ds-tourism": "Αφίξεις τουριστών και διανυκτερεύσεις σε ξενοδοχεία",
}


@pytest.fixture(scope="module")
def model() -> SentenceTransformer:
    """Load the (small) test embedding model once for the module."""
    return load_model("intfloat/multilingual-e5-small")


def _row(id_: str, text: str) -> DatasetRow:
    """Build a minimal DatasetRow carrying the given embed_text."""
    return DatasetRow(
        id=id_, name=f"name-{id_}", title=text, title_en=None, notes=None, notes_en=None,
        org_name=None, org_title=None, license_id=None, license_title=None, frequency=None,
        language_options=[], theme=[], num_resources=0, tags=[], temporal_start=None,
        temporal_end=None, spatial_text=None, metadata_created=None,
        last_updated="2026-01-01T00:00:00", state="active",
        harvested_at="2026-06-01T00:00:00", embed_text=text,
    )


@pytest.fixture
def indexed(model: SentenceTransformer, tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, str]]:
    """A populated catalog with both the Chroma and FTS indexes built."""
    conn = connect(":memory:")
    init_db(conn)
    for id_, text in _TOPICS.items():
        upsert_dataset(conn, _row(id_, text))
    conn.commit()
    chroma_path = str(tmp_path / "chroma")
    build_chroma_index(conn, model, chroma_path=chroma_path)
    build_fts_index(conn)
    yield conn, chroma_path


def test_find_dataset_returns_topical_match_first(
    indexed: tuple[sqlite3.Connection, str], model: SentenceTransformer
) -> None:
    """A Greek question about fires returns the fires dataset first, hydrated."""
    conn, chroma_path = indexed
    results = find_dataset(
        "πόσες πυρκαγιές καίνε δάση κάθε χρόνο;",
        conn=conn, model=model, chroma_path=chroma_path, top_k=3,
    )
    assert results
    assert results[0].id == "ds-fires"
    assert results[0].name == "name-ds-fires"
    assert results[0].rank == 0
    assert isinstance(results[0], Candidate)


def test_find_dataset_respects_top_k(
    indexed: tuple[sqlite3.Connection, str], model: SentenceTransformer
) -> None:
    """At most top_k candidates are returned."""
    conn, chroma_path = indexed
    results = find_dataset(
        "τουρισμός", conn=conn, model=model, chroma_path=chroma_path, top_k=2
    )
    assert len(results) <= 2


def test_find_dataset_lexical_exact_token(
    indexed: tuple[sqlite3.Connection, str], model: SentenceTransformer
) -> None:
    """An exact rare token surfaces its dataset via the lexical arm."""
    conn, chroma_path = indexed
    results = find_dataset(
        "διανυκτερεύσεις", conn=conn, model=model, chroma_path=chroma_path, top_k=4
    )
    assert "ds-tourism" in [c.id for c in results]


def test_candidate_is_frozen() -> None:
    """Candidate is an immutable value object."""
    c = Candidate(id="x", name="n", title="t", last_updated=None, rank=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.rank = 1  # type: ignore[misc]
