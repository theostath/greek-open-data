"""Cross-encoder reranking of hybrid candidates (Phase 3 follow-up, ADR-0002).

A cross-encoder scores each ``(question, embed_text)`` pair jointly — more accurate
than the bi-encoder cosine used for dense retrieval, at the cost of one local model
and a few hundred ms/query. It runs over the small fused candidate pool, not the whole
catalog. Opt-in and eval-gated: enable via ``rerank_enabled`` in config.
"""

from __future__ import annotations

from typing import Any, Protocol, cast

from config import get_settings
from sentence_transformers import CrossEncoder

from pythia.net import use_system_trust_store


class Scorer(Protocol):
    """Minimal cross-encoder interface: jointly score ``(query, passage)`` pairs."""

    def predict(self, sentences: list[tuple[str, str]]) -> Any:
        """Return one relevance score per pair (array-like of floats)."""
        ...


def load_reranker(name: str | None = None) -> Scorer:
    """Load the cross-encoder reranker (defaults to the configured one) via the OS trust store."""
    use_system_trust_store()
    model_name = name or get_settings().rerank_model
    return cast(Scorer, CrossEncoder(model_name))


def rerank(
    question: str,
    candidates: list[tuple[str, str]],
    scorer: Scorer,
    *,
    top_k: int,
) -> list[str]:
    """Reorder ``(id, embed_text)`` candidates by cross-encoder relevance; return top_k ids.

    Ties keep the incoming (RRF) order via a stable sort, so the reranker only ever
    reorders on a decisive score difference.
    """
    if not candidates:
        return []
    scores = [float(s) for s in scorer.predict([(question, text) for _, text in candidates])]
    order = sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)
    return [candidates[i][0] for i in order[:top_k]]
