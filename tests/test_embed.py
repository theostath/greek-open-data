"""Tests for the dense embedding + Chroma vector index layer (Phase 3)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from sentence_transformers import SentenceTransformer

from pythia.ingest import db
from pythia.ingest.models import DatasetRow
from pythia.retrieval import embed

# Tests pin the small model: fast, and the 384-dim assertions below depend on it.
TEST_MODEL = "intfloat/multilingual-e5-small"


@pytest.fixture(scope="module")
def model() -> SentenceTransformer:
    """Load the (small) test embedding model once for the whole module."""
    return embed.load_model(TEST_MODEL)


def _norm(vec: list[float]) -> float:
    """Return the L2 norm of a vector."""
    return math.sqrt(sum(x * x for x in vec))


def _row(dataset_id: str, embed_text: str) -> DatasetRow:
    """Build a minimal DatasetRow carrying only an id and embed_text."""
    return DatasetRow(
        id=dataset_id,
        name=dataset_id,
        title=None,
        title_en=None,
        notes=None,
        notes_en=None,
        org_name=None,
        org_title=None,
        license_id=None,
        license_title=None,
        frequency=None,
        language_options=[],
        theme=[],
        num_resources=0,
        tags=[],
        temporal_start=None,
        temporal_end=None,
        spatial_text=None,
        metadata_created=None,
        last_updated=None,
        state=None,
        harvested_at="2026-06-01T00:00:00Z",
        embed_text=embed_text,
    )


def test_embed_query_dims_and_norm(model: SentenceTransformer) -> None:
    """embed_query returns a 384-dim L2-normalized vector."""
    vec = embed.embed_query(model, "πόσα δασικά ατυχήματα;")
    assert len(vec) == 384
    assert all(isinstance(x, float) for x in vec)
    assert _norm(vec) == pytest.approx(1.0, abs=1e-3)


def test_embed_passages_dims_and_norm(model: SentenceTransformer) -> None:
    """embed_passages returns a list of 384-dim L2-normalized vectors."""
    vecs = embed.embed_passages(model, ["δασικές πυρκαγιές", "εμβολιασμοί"])
    assert len(vecs) == 2
    for vec in vecs:
        assert len(vec) == 384
        assert _norm(vec) == pytest.approx(1.0, abs=1e-3)


def test_build_index_and_dense_search(model: SentenceTransformer, tmp_path: Path) -> None:
    """Building the index returns the row count; dense_search ranks the right id first."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    rows = [
        _row("mcp_forest_fires", "Δασικές πυρκαγιές και καμένες εκτάσεις ανά έτος"),
        _row("mdg_emvolio", "Εμβολιασμοί κατά της COVID-19 και δόσεις εμβολίων"),
        _row("mcp_traffic_accidents", "Τροχαία ατυχήματα και οδικές συγκρούσεις"),
        _row("tourism_arrivals", "Τουρισμός και αφίξεις επισκεπτών στην Ελλάδα"),
    ]
    for row in rows:
        db.upsert_dataset(conn, row)
    conn.commit()

    chroma_path = str(tmp_path / "chroma")
    count = embed.build_chroma_index(conn, model, chroma_path=chroma_path)
    assert count == 4

    hits = embed.dense_search(
        "πόσα εμβόλια έγιναν;", top_k=4, model=model, chroma_path=chroma_path
    )
    assert hits[0] == "mdg_emvolio"

    fire_hits = embed.dense_search(
        "καμένες εκτάσεις από φωτιές", top_k=4, model=model, chroma_path=chroma_path
    )
    assert fire_hits[0] == "mcp_forest_fires"


def test_incremental_skips_unchanged(model: SentenceTransformer, tmp_path: Path) -> None:
    """A rebuild with no catalog changes re-embeds nothing."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    for row in [_row("a", "δασικές πυρκαγιές"), _row("b", "εμβολιασμοί"),
                _row("c", "τροχαία ατυχήματα")]:
        db.upsert_dataset(conn, row)
    conn.commit()
    chroma_path = str(tmp_path / "chroma")
    assert embed.build_chroma_index(conn, model, chroma_path=chroma_path) == 3
    assert embed.build_chroma_index(conn, model, chroma_path=chroma_path) == 0


def test_incremental_embeds_only_changed(model: SentenceTransformer, tmp_path: Path) -> None:
    """Only datasets whose embed_text changed are re-embedded on rebuild."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    for row in [_row("a", "δασικές πυρκαγιές"), _row("b", "εμβολιασμοί")]:
        db.upsert_dataset(conn, row)
    conn.commit()
    chroma_path = str(tmp_path / "chroma")
    embed.build_chroma_index(conn, model, chroma_path=chroma_path)
    db.upsert_dataset(conn, _row("b", "τουρισμός και αφίξεις επισκεπτών"))
    conn.commit()
    assert embed.build_chroma_index(conn, model, chroma_path=chroma_path) == 1


def test_incremental_deletes_removed(model: SentenceTransformer, tmp_path: Path) -> None:
    """Datasets removed from the catalog are dropped from the index."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    for row in [_row("a", "δασικές πυρκαγιές"), _row("b", "εμβολιασμοί")]:
        db.upsert_dataset(conn, row)
    conn.commit()
    chroma_path = str(tmp_path / "chroma")
    embed.build_chroma_index(conn, model, chroma_path=chroma_path)
    conn.execute("DELETE FROM datasets WHERE id = ?", ("b",))
    conn.commit()
    embed.build_chroma_index(conn, model, chroma_path=chroma_path)
    hits = embed.dense_search("εμβόλια", top_k=5, model=model, chroma_path=chroma_path)
    assert "b" not in hits
