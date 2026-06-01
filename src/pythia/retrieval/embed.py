"""Dense embeddings + Chroma vector index for dataset retrieval (Phase 3).

Wraps a local ``sentence-transformers`` E5 model and a persistent Chroma
collection. E5 needs asymmetric prefixes — ``query:`` for questions and
``passage:`` for indexed documents — and embeddings are L2-normalized so cosine
similarity equals dot product. Embeddings are always passed to Chroma explicitly
so it never falls back to (and downloads) its default ONNX embedder.
"""

from __future__ import annotations

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


def build_chroma_index(
    conn: sqlite3.Connection,
    model: SentenceTransformer,
    *,
    chroma_path: str,
    collection: str = "datasets",
    batch_size: int = 256,
) -> int:
    """Embed every dataset's ``embed_text`` in batches and upsert into Chroma; return count."""
    coll = _collection(chroma_path, collection)
    cursor = conn.execute("SELECT id, embed_text FROM datasets")
    total = 0
    batch: list[tuple[str, str]] = []

    def flush(items: list[tuple[str, str]]) -> None:
        """Embed and upsert one batch of (id, text) rows."""
        if not items:
            return
        ids = [item[0] for item in items]
        embeddings = embed_passages(model, [item[1] for item in items])
        coll.upsert(ids=ids, embeddings=cast(Embeddings, embeddings))

    for row in cursor:
        batch.append((str(row[0]), str(row[1])))
        if len(batch) >= batch_size:
            flush(batch)
            total += len(batch)
            batch = []
    flush(batch)
    total += len(batch)
    return total


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
