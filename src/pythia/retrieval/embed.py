"""Dense embeddings for dataset retrieval (Phase 3).

Wraps a local ``sentence-transformers`` E5 model. E5 needs asymmetric prefixes —
``query:`` for questions and ``passage:`` for indexed documents — and embeddings are
L2-normalized so cosine similarity equals dot product. Vectors are stored and searched
by ``pythia.retrieval.dense``.
"""

from __future__ import annotations

import hashlib
from typing import cast

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
    """Embed documents with the E5 ``passage:`` prefix; return normalized vectors.

    E5 silently truncates input beyond ``model.max_seq_length`` (512 tokens). The
    ``embed_text`` fed here is ordered high-signal-first (see ``normalize._build_embed_text``)
    so truncation drops long descriptions rather than the bilingual title/tag signal.
    """
    prefixed = [f"passage: {t}" for t in texts]
    vectors = model.encode(prefixed, normalize_embeddings=True)
    return [[float(x) for x in row] for row in vectors]


def _signature(embed_text: str) -> str:
    """Content signature used to detect which datasets need re-embedding."""
    return hashlib.sha1(embed_text.encode("utf-8")).hexdigest()
