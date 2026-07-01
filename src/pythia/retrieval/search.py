"""Hybrid dataset retrieval: dense (Chroma) + lexical (FTS5), fused with RRF.

``find_dataset`` is the Phase 3 entry point: it returns ranked candidate datasets
for a natural-language question, hydrated with catalog metadata for provenance.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from sentence_transformers import SentenceTransformer

from pythia.retrieval.embed import dense_search
from pythia.retrieval.lexical import lexical_search, rrf_fuse
from pythia.retrieval.rerank import Scorer, rerank


@dataclass(frozen=True)
class Candidate:
    """A ranked dataset candidate with the metadata needed for citation."""

    id: str
    name: str
    title: str | None
    last_updated: str | None
    rank: int


def find_dataset(
    question: str,
    *,
    conn: sqlite3.Connection,
    model: SentenceTransformer,
    chroma_path: str,
    top_k: int = 10,
    pool: int = 50,
    collection: str = "datasets",
    reranker: Scorer | None = None,
    rerank_pool: int = 20,
) -> list[Candidate]:
    """Return up to ``top_k`` datasets for ``question`` via RRF-fused hybrid search.

    When ``reranker`` is given, the top ``rerank_pool`` fused candidates are re-scored by
    the cross-encoder and reordered before ``top_k`` is taken; otherwise fusion alone ranks.
    """
    dense = dense_search(
        question, pool, model=model, chroma_path=chroma_path, collection=collection
    )
    lexical = lexical_search(conn, question, pool)
    if reranker is not None:
        fused = rrf_fuse([dense, lexical], top_k=rerank_pool)
        fused = rerank(question, _fetch_embed_texts(conn, fused), reranker, top_k=top_k)
    else:
        fused = rrf_fuse([dense, lexical], top_k=top_k)
    return _hydrate(conn, fused)


def _fetch_embed_texts(conn: sqlite3.Connection, ids: list[str]) -> list[tuple[str, str]]:
    """Return ``(id, embed_text)`` for ids, preserving fused order and skipping missing ones."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    texts = {
        row[0]: str(row[1])
        for row in conn.execute(
            f"SELECT id, embed_text FROM datasets WHERE id IN ({placeholders})", ids
        )
    }
    return [(dataset_id, texts[dataset_id]) for dataset_id in ids if dataset_id in texts]


def _hydrate(conn: sqlite3.Connection, ids: list[str]) -> list[Candidate]:
    """Attach catalog metadata to fused ids, preserving fused rank order."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = {
        row[0]: row
        for row in conn.execute(
            f"SELECT id, name, title, last_updated FROM datasets WHERE id IN ({placeholders})",
            ids,
        )
    }
    candidates: list[Candidate] = []
    for rank, dataset_id in enumerate(ids):
        row = rows.get(dataset_id)
        if row is None:
            continue
        candidates.append(
            Candidate(id=row[0], name=row[1], title=row[2], last_updated=row[3], rank=rank)
        )
    return candidates
