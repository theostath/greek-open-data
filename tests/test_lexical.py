"""Tests for the FTS5 lexical search and Reciprocal Rank Fusion (Phase 3 retrieval)."""

from __future__ import annotations

import sqlite3

from pythia.ingest import db
from pythia.ingest.models import DatasetRow
from pythia.retrieval.lexical import build_fts_index, lexical_search, rrf_fuse


def _dataset(id_: str, embed_text: str) -> DatasetRow:
    """Build a minimal DatasetRow carrying a distinct embed_text."""
    return DatasetRow(
        id=id_,
        name=f"name-{id_}",
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
        state="active",
        harvested_at="2026-06-01T00:00:00",
        embed_text=embed_text,
    )


def _catalog() -> sqlite3.Connection:
    """Build an in-memory catalog with a few distinct Greek embed_texts."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.upsert_dataset(conn, _dataset("fires", "δασικές πυρκαγιές και καμένες εκτάσεις"))
    db.upsert_dataset(conn, _dataset("traffic", "τροχαία ατυχήματα στους δρόμους"))
    db.upsert_dataset(conn, _dataset("vaccines", "εμβολιασμός πληθυσμού κατά covid"))
    conn.commit()
    return conn


# --- build_fts_index --------------------------------------------------------


def test_build_fts_index_returns_row_count() -> None:
    """build_fts_index indexes every dataset and returns the count."""
    conn = _catalog()
    assert build_fts_index(conn) == 3


def test_build_fts_index_is_idempotent() -> None:
    """Calling build_fts_index twice rebuilds without error and same count."""
    conn = _catalog()
    assert build_fts_index(conn) == 3
    assert build_fts_index(conn) == 3
    rows = conn.execute("SELECT count(*) FROM datasets_fts").fetchone()[0]
    assert rows == 3


# --- lexical_search ---------------------------------------------------------


def test_lexical_search_finds_matching_dataset() -> None:
    """A query term returns the dataset whose embed_text contains it."""
    conn = _catalog()
    build_fts_index(conn)
    assert lexical_search(conn, "ατυχήματα", 5) == ["traffic"]


def test_lexical_search_is_accent_insensitive() -> None:
    """An un-accented query matches accented text (remove_diacritics 2)."""
    conn = _catalog()
    build_fts_index(conn)
    assert lexical_search(conn, "πυρκαγιες", 5) == ["fires"]


def test_lexical_search_respects_top_k() -> None:
    """top_k bounds the number of returned ids."""
    conn = _catalog()
    build_fts_index(conn)
    results = lexical_search(conn, "πυρκαγιές ατυχήματα εμβολιασμός", 2)
    assert len(results) <= 2


def test_lexical_search_empty_query_returns_empty() -> None:
    """An empty query yields [] without raising."""
    conn = _catalog()
    build_fts_index(conn)
    assert lexical_search(conn, "", 5) == []


def test_lexical_search_punctuation_only_returns_empty() -> None:
    """A punctuation-only query yields [] without raising."""
    conn = _catalog()
    build_fts_index(conn)
    assert lexical_search(conn, "  !?.- ", 5) == []


# --- rrf_fuse ---------------------------------------------------------------


def test_rrf_fuse_id_in_both_lists_wins() -> None:
    """An id present in both rankings outranks one present in only one."""
    fused = rrf_fuse([["a", "b"], ["b", "c"]])
    assert fused[0] == "b"
    assert set(fused) == {"a", "b", "c"}


def test_rrf_fuse_truncates_to_top_k() -> None:
    """top_k truncates the fused ranking."""
    fused = rrf_fuse([["a", "b", "c"], ["c", "b", "a"]], top_k=2)
    assert len(fused) == 2


def test_rrf_fuse_merges_disjoint_lists() -> None:
    """Disjoint rankings merge into the union of ids."""
    fused = rrf_fuse([["a"], ["b"]])
    assert set(fused) == {"a", "b"}


def test_rrf_fuse_stable_tie_break_by_first_appearance() -> None:
    """Equal scores break ties by first appearance order."""
    fused = rrf_fuse([["x", "y"], ["x", "y"]])
    assert fused == ["x", "y"]


def test_rrf_fuse_empty_returns_empty() -> None:
    """No rankings yields an empty list."""
    assert rrf_fuse([]) == []
