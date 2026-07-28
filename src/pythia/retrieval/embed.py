"""Dense embeddings + Chroma vector index for dataset retrieval (Phase 3).

Wraps a local ``sentence-transformers`` E5 model and a persistent Chroma
collection. E5 needs asymmetric prefixes — ``query:`` for questions and
``passage:`` for indexed documents — and embeddings are L2-normalized so cosine
similarity equals dot product. Embeddings are always passed to Chroma explicitly
so it never falls back to (and downloads) its default ONNX embedder.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import cast

import chromadb
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection
from chromadb.api.types import Embeddings
from chromadb.config import Settings
from config import get_settings
from sentence_transformers import SentenceTransformer

from pythia.net import use_system_trust_store


def load_model(name: str | None = None) -> SentenceTransformer:
    """Load the embedding model (defaults to the configured one) via the OS trust store."""
    use_system_trust_store()
    model_name = name or get_settings().embedding_model
    return cast(SentenceTransformer, SentenceTransformer(model_name))


def embed_query(model: SentenceTransformer, text: str) -> list[float]:
    """Embed a search query with the E5 ``query:`` prefix; return a normalized vector."""
    vector = model.encode(f"query: {text}", normalize_embeddings=True)
    return [float(x) for x in vector]


def embed_passages(model: SentenceTransformer, texts: list[str]) -> list[list[float]]:
    """Embed documents with the E5 ``passage:`` prefix; return normalized vectors."""
    prefixed = [f"passage: {t}" for t in texts]
    vectors = model.encode(prefixed, normalize_embeddings=True)
    return [[float(x) for x in row] for row in vectors]


def _client(chroma_path: str) -> ClientAPI:
    """Open a persistent Chroma client with telemetry disabled."""
    return chromadb.PersistentClient(
        path=chroma_path,
        settings=Settings(anonymized_telemetry=False),
    )


def _collection(chroma_path: str, collection: str) -> Collection:
    """Get or create the cosine-space collection at the given path."""
    return _client(chroma_path).get_or_create_collection(
        name=collection,
        metadata={"hnsw:space": "cosine"},
    )


def _signature(embed_text: str) -> str:
    """Content signature used to detect which datasets need re-embedding."""
    return hashlib.sha1(embed_text.encode("utf-8")).hexdigest()


def build_chroma_index(
    conn: sqlite3.Connection,
    model: SentenceTransformer,
    *,
    chroma_path: str,
    collection: str = "datasets",
    batch_size: int = 256,
) -> int:
    """Incrementally index the catalog into Chroma; return the number embedded this run.

    Only datasets whose ``embed_text`` changed since the last run are re-embedded
    (tracked via a per-vector content signature); datasets removed from the catalog
    are dropped from the index. A fresh index embeds everything.

    When *every* dataset needs re-embedding over an already-populated collection, the
    collection is dropped and recreated rather than upserted in place: HNSW has no true
    update, so upserting every vector leaves one tombstone per row in the graph, which
    degrades recall and makes ANN results vary between processes.
    """
    coll = _collection(chroma_path, collection)
    current = {
        str(row[0]): str(row[1])
        for row in conn.execute("SELECT id, embed_text FROM datasets")
    }

    existing = coll.get()
    metadatas = existing.get("metadatas") or []
    existing_sigs = {
        ex_id: (meta or {}).get("sig")
        for ex_id, meta in zip(existing["ids"], metadatas, strict=False)
    }

    changed = [
        (dataset_id, text)
        for dataset_id, text in current.items()
        if existing_sigs.get(dataset_id) != _signature(text)
    ]
    removed = [ex_id for ex_id in existing_sigs if ex_id not in current]

    if existing_sigs and current and len(changed) == len(current):
        _client(chroma_path).delete_collection(collection)
        coll = _collection(chroma_path, collection)
        removed = []  # the dropped collection took the stale vectors with it

    for start in range(0, len(changed), batch_size):
        chunk = changed[start : start + batch_size]
        embeddings = embed_passages(model, [text for _, text in chunk])
        coll.upsert(
            ids=[dataset_id for dataset_id, _ in chunk],
            embeddings=cast(Embeddings, embeddings),
            metadatas=[{"sig": _signature(text)} for _, text in chunk],
        )

    if removed:
        coll.delete(ids=removed)

    return len(changed)


def dense_search(
    query: str,
    top_k: int,
    *,
    model: SentenceTransformer,
    chroma_path: str,
    collection: str = "datasets",
) -> list[str]:
    """Embed the query and return the top-k matching dataset ids, best-first."""
    coll = _collection(chroma_path, collection)
    vector = embed_query(model, query)
    result = coll.query(query_embeddings=cast(Embeddings, [vector]), n_results=top_k)
    ids = result["ids"]
    return list(ids[0]) if ids else []
