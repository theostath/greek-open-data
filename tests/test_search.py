"""Tests for hybrid dataset search (dense + lexical, RRF-fused).

The dense leg is an exact ``DenseIndex`` snapshot (ADR-0010), passed in as ``dense=``.
Fixtures build it the way production does: ``build_dense_index`` over a FILE catalogue with
the real e5-small model, then ``load_dense_index`` from that file.
"""

from __future__ import annotations

import dataclasses
import inspect
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sentence_transformers import SentenceTransformer

from pythia.ingest.db import connect, init_db, upsert_dataset
from pythia.ingest.models import DatasetRow
from pythia.retrieval.dense import DenseIndex, build_dense_index, load_dense_index
from pythia.retrieval.embed import load_model
from pythia.retrieval.lexical import build_fts_index, lexical_search
from pythia.retrieval.search import Candidate, find_dataset

TEST_MODEL = "intfloat/multilingual-e5-small"

_TOPICS = {
    "ds-fires": "Δασικές πυρκαγιές ανά έτος και περιοχή στην Ελλάδα",
    "ds-vacc": "Εμβολιασμοί για τον κορονοϊό COVID-19 ανά ημέρα",
    "ds-traffic": "Τροχαία ατυχήματα και οδική ασφάλεια",
    "ds-tourism": "Αφίξεις τουριστών και διανυκτερεύσεις σε ξενοδοχεία",
}

#: English, so it shares no token with any (Greek-only) embed_text: only the dense leg can
#: find ``ds-traffic``. Probed on e5-small: ds-traffic 0.849 vs runner-up 0.774.
_DENSE_ONLY_QUESTION = "car crashes on roads"


@pytest.fixture(scope="module")
def model() -> SentenceTransformer:
    """Load the (small) test embedding model once for the module."""
    return load_model(TEST_MODEL)


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
def indexed(
    model: SentenceTransformer, tmp_path: Path
) -> Iterator[tuple[sqlite3.Connection, DenseIndex]]:
    """A populated FILE catalogue with the dense table and FTS built, plus its loaded snapshot."""
    path = tmp_path / "catalog.sqlite"
    conn = connect(path)
    init_db(conn)
    for id_, text in _TOPICS.items():
        upsert_dataset(conn, _row(id_, text))
    conn.commit()
    assert build_dense_index(conn, model, model_name=TEST_MODEL) == len(_TOPICS)
    build_fts_index(conn)
    conn.commit()
    dense = load_dense_index(path, TEST_MODEL)
    assert len(dense.ids) == len(_TOPICS)
    yield conn, dense
    conn.close()


class SpyDense:
    """Duck-typed ``DenseIndex`` that records every ``search(q, k)`` call."""

    def __init__(self, hits: list[tuple[str, float]] | None = None) -> None:
        """Return ``hits`` (best-first) from every search."""
        self.calls: list[tuple[list[float], int]] = []
        self.hits = hits or []

    def search(self, q: Any, k: int) -> list[tuple[str, float]]:
        """Record the query vector and k; return the canned hits truncated to k."""
        self.calls.append(([float(x) for x in q], k))
        return list(self.hits[:k])


def test_find_dataset_returns_topical_match_first(
    indexed: tuple[sqlite3.Connection, DenseIndex], model: SentenceTransformer
) -> None:
    """A Greek question about fires returns the fires dataset first, hydrated."""
    conn, dense = indexed
    results = find_dataset(
        "πόσες πυρκαγιές καίνε δάση κάθε χρόνο;",
        conn=conn, model=model, dense=dense, top_k=3,
    )
    assert results
    assert results[0].id == "ds-fires"
    assert results[0].name == "name-ds-fires"
    assert results[0].rank == 0
    assert isinstance(results[0], Candidate)


def test_find_dataset_respects_top_k(
    indexed: tuple[sqlite3.Connection, DenseIndex], model: SentenceTransformer
) -> None:
    """At most top_k candidates are returned."""
    conn, dense = indexed
    results = find_dataset("τουρισμός", conn=conn, model=model, dense=dense, top_k=2)
    assert len(results) <= 2


def test_find_dataset_lexical_exact_token(
    indexed: tuple[sqlite3.Connection, DenseIndex], model: SentenceTransformer
) -> None:
    """An exact rare token surfaces its dataset via the lexical arm."""
    conn, dense = indexed
    results = find_dataset("διανυκτερεύσεις", conn=conn, model=model, dense=dense, top_k=4)
    assert "ds-tourism" in [c.id for c in results]


def test_find_dataset_dense_leg_alone_finds_the_dataset(
    indexed: tuple[sqlite3.Connection, DenseIndex], model: SentenceTransformer
) -> None:
    """With zero lexical overlap, the real dense snapshot still ranks the right dataset first."""
    conn, dense = indexed
    assert lexical_search(conn, _DENSE_ONLY_QUESTION, 50) == [], "precondition: no lexical hit"

    results = find_dataset(_DENSE_ONLY_QUESTION, conn=conn, model=model, dense=dense, top_k=4)

    assert results, "the dense leg returned nothing: is dense= actually searched?"
    assert results[0].id == "ds-traffic"


def test_find_dataset_searches_dense_with_pool_not_top_k(
    indexed: tuple[sqlite3.Connection, DenseIndex], model: SentenceTransformer
) -> None:
    """The dense leg is asked for ``pool`` (default 50) candidates, never ``top_k``."""
    conn, _ = indexed
    spy = SpyDense()

    find_dataset("πυρκαγιές", conn=conn, model=model, dense=spy, top_k=3)  # type: ignore[arg-type]

    assert len(spy.calls) == 1
    vector, k = spy.calls[0]
    assert k == 50
    assert len(vector) == 384, "the query is embedded with the e5 model before searching"


def test_find_dataset_passes_an_explicit_pool_to_dense(
    indexed: tuple[sqlite3.Connection, DenseIndex], model: SentenceTransformer
) -> None:
    """A caller-supplied ``pool`` reaches ``dense.search`` unchanged."""
    conn, _ = indexed
    spy = SpyDense()

    find_dataset(
        "πυρκαγιές", conn=conn, model=model, dense=spy, top_k=2, pool=7,  # type: ignore[arg-type]
    )

    assert [k for _, k in spy.calls] == [7]


def test_find_dataset_fuses_what_the_dense_leg_returns(
    indexed: tuple[sqlite3.Connection, DenseIndex], model: SentenceTransformer
) -> None:
    """A dataset only the dense leg returns (no lexical overlap) is fused and hydrated."""
    conn, _ = indexed
    assert lexical_search(conn, _DENSE_ONLY_QUESTION, 50) == []
    spy = SpyDense(hits=[("ds-vacc", 0.91), ("ds-fires", 0.40)])

    results = find_dataset(
        _DENSE_ONLY_QUESTION, conn=conn, model=model, dense=spy, top_k=4,  # type: ignore[arg-type]
    )

    assert [c.id for c in results] == ["ds-vacc", "ds-fires"]
    assert results[0].title == _TOPICS["ds-vacc"]


def test_find_dataset_with_an_empty_dense_index_is_lexical_only(
    indexed: tuple[sqlite3.Connection, DenseIndex], model: SentenceTransformer, tmp_path: Path
) -> None:
    """An empty snapshot (no rows for this model) degrades to lexical results, no raise."""
    conn, _ = indexed
    empty = load_dense_index(tmp_path / "catalog.sqlite", "some/other-model")
    assert len(empty.ids) == 0

    results = find_dataset("διανυκτερεύσεις", conn=conn, model=model, dense=empty, top_k=4)

    assert [c.id for c in results] == ["ds-tourism"]


def test_find_dataset_has_no_chroma_parameters() -> None:
    """``chroma_path`` and ``collection`` are gone from the signature (ADR-0010)."""
    params = inspect.signature(find_dataset).parameters
    assert "dense" in params
    assert "chroma_path" not in params and "collection" not in params


def test_candidate_is_frozen() -> None:
    """Candidate is an immutable value object."""
    c = Candidate(id="x", name="n", title="t", last_updated=None, rank=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.rank = 1  # type: ignore[misc]
