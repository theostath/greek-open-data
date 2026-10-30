"""Tests for the dense embedding layer (Phase 3).

Building and searching the dense index moved to ``retrieval/dense.py`` (ADR-0010) and is
covered by ``tests/test_dense.py``; this module keeps the model/embedding contract and pins
that the Chroma code is gone.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

import pytest
from sentence_transformers import SentenceTransformer

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


def test_embed_passages_and_query_agree_on_topic(model: SentenceTransformer) -> None:
    """A query lands nearest its own topic's passage (cosine == dot on unit vectors)."""
    passages = embed.embed_passages(
        model,
        ["Δασικές πυρκαγιές και καμένες εκτάσεις ανά έτος",
         "Εμβολιασμοί κατά της COVID-19 και δόσεις εμβολίων"],
    )
    query = embed.embed_query(model, "πόσα εμβόλια έγιναν;")
    scores = [sum(a * b for a, b in zip(query, p, strict=True)) for p in passages]
    assert scores[1] > scores[0]


def test_signature_is_sha1_of_utf8_embed_text() -> None:
    """``_signature`` survives the Chroma removal: the dense builder and preflight share it."""
    text = "Δασικές πυρκαγιές"
    assert embed._signature(text) == hashlib.sha1(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "name", ["build_chroma_index", "dense_search", "_collection", "_client"]
)
def test_chroma_helpers_are_removed(name: str) -> None:
    """The Chroma index layer is gone from ``embed`` (ADR-0010)."""
    assert not hasattr(embed, name), f"embed.{name} should have been removed"


def test_no_source_file_imports_chromadb() -> None:
    """``chromadb`` is no longer a dependency of anything under ``src/``."""
    src = Path(__file__).resolve().parents[1] / "src"
    pattern = re.compile(r"^\s*(import|from)\s+chromadb(\.|\s|$)", re.MULTILINE)
    offenders = [
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
