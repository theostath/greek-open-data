"""Tests for cross-encoder reranking (ADR-0002).

These run offline: ``rerank`` is exercised with a fake scorer, and the search-layer
embed_text fetch against an in-memory catalog. The real cross-encoder is not loaded.
"""

from __future__ import annotations

from pythia.ingest import db
from pythia.ingest.models import DatasetRow
from pythia.retrieval.rerank import rerank
from pythia.retrieval.search import _fetch_embed_texts


class FakeScorer:
    """Scorer stub returning a preset score per candidate passage text."""

    def __init__(self, scores: dict[str, float]) -> None:
        """Store the passage-text -> score mapping."""
        self._scores = scores

    def predict(self, sentences: list[tuple[str, str]]) -> list[float]:
        """Return the preset score for each (query, passage) pair by passage text."""
        return [self._scores[passage] for _, passage in sentences]


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


def test_rerank_orders_by_score_desc() -> None:
    """rerank reorders candidates by descending cross-encoder score."""
    candidates = [("a", "txt_a"), ("b", "txt_b"), ("c", "txt_c")]
    scorer = FakeScorer({"txt_a": 0.1, "txt_b": 0.9, "txt_c": 0.5})
    assert rerank("q", candidates, scorer, top_k=3) == ["b", "c", "a"]


def test_rerank_respects_top_k() -> None:
    """rerank returns only the top_k best-scoring ids."""
    candidates = [("a", "txt_a"), ("b", "txt_b"), ("c", "txt_c")]
    scorer = FakeScorer({"txt_a": 0.1, "txt_b": 0.9, "txt_c": 0.5})
    assert rerank("q", candidates, scorer, top_k=2) == ["b", "c"]


def test_rerank_stable_on_ties() -> None:
    """Equal scores keep the incoming (RRF) order via a stable sort."""
    candidates = [("a", "txt_a"), ("b", "txt_b"), ("c", "txt_c")]
    scorer = FakeScorer({"txt_a": 0.5, "txt_b": 0.5, "txt_c": 0.5})
    assert rerank("q", candidates, scorer, top_k=3) == ["a", "b", "c"]


def test_rerank_empty_candidates() -> None:
    """No candidates yields an empty result without calling the scorer."""
    assert rerank("q", [], FakeScorer({}), top_k=5) == []


def test_fetch_embed_texts_preserves_order_and_skips_missing() -> None:
    """_fetch_embed_texts returns (id, embed_text) in fused order, dropping unknown ids."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.upsert_dataset(conn, _row("a", "text a"))
    db.upsert_dataset(conn, _row("b", "text b"))
    conn.commit()

    result = _fetch_embed_texts(conn, ["b", "missing", "a"])
    assert result == [("b", "text b"), ("a", "text a")]
    assert _fetch_embed_texts(conn, []) == []
