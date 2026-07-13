"""Golden-question retrieval eval: MRR and recall@k over the hybrid retriever.

Scoring helpers are pure and unit-tested; ``main`` runs the live eval against the
built indexes and prints per-language metrics.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from config import get_settings

from pythia.ingest.db import connect
from pythia.logging_setup import configure_logging, get_logger, log_event
from pythia.planning.normalize import normalize_question
from pythia.retrieval.embed import load_model
from pythia.retrieval.rerank import Scorer, load_reranker
from pythia.retrieval.search import find_dataset

LOGGER_NAME = "pythia.eval.run_eval"
GOLDEN_PATH = Path(__file__).parent / "golden_questions.yaml"
K_VALUES = (1, 3, 5, 10)


@dataclass(frozen=True)
class GoldenQuestion:
    """One golden question mapped to its single correct dataset."""

    id: str
    question: str
    lang: str
    expected_name: str
    expected_id: str


def load_golden(path: Path | None = None) -> list[GoldenQuestion]:
    """Load and parse the golden-question set from YAML."""
    source = path or GOLDEN_PATH
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    return [
        GoldenQuestion(
            id=str(item["id"]),
            question=str(item["question"]),
            lang=str(item["lang"]),
            expected_name=str(item["expected_name"]),
            expected_id=str(item["expected_id"]),
        )
        for item in raw["questions"]
    ]


def reciprocal_rank(expected_id: str, ranked_ids: list[str]) -> float:
    """Return 1/position (1-based) of the expected id, or 0.0 if absent."""
    for index, ranked_id in enumerate(ranked_ids):
        if ranked_id == expected_id:
            return 1.0 / (index + 1)
    return 0.0


def hit_at_k(expected_id: str, ranked_ids: list[str], k: int) -> bool:
    """Return whether the expected id appears within the first ``k`` results."""
    return expected_id in ranked_ids[:k]


def _format_metrics(label: str, ranks: list[float], hits: dict[int, int]) -> str:
    """Format one metrics row (MRR + recall@k) for printing."""
    n = len(ranks)
    mrr = sum(ranks) / n if n else 0.0
    recalls = "  ".join(f"R@{k}={hits[k] / n:.2f}" if n else f"R@{k}=—" for k in K_VALUES)
    return f"{label:<10} n={n:<3} MRR={mrr:.3f}  {recalls}"


def main(argv: list[str] | None = None) -> int:
    """Run the golden-question eval against the built indexes; print metrics.

    ``--normalize`` (default) applies the Phase 4 query normalization (Greeklish→Greek)
    before retrieval; ``--no-normalize`` reproduces the raw Phase 3 baseline for the
    ADR-0005 off-vs-on comparison.
    """
    parser = argparse.ArgumentParser(description="Golden-question retrieval eval.")
    parser.add_argument("--normalize", action="store_true", default=True)
    parser.add_argument("--no-normalize", dest="normalize", action="store_false")
    args = parser.parse_args(argv)

    configure_logging()
    logger = get_logger(LOGGER_NAME)
    settings = get_settings()
    questions = load_golden()

    conn = connect(settings.catalog_db_path)
    model = load_model(settings.embedding_model)
    reranker: Scorer | None = (
        load_reranker(settings.rerank_model) if settings.rerank_enabled else None
    )
    log_event(
        logger, logging.INFO, "eval.config",
        rerank_enabled=settings.rerank_enabled, normalize=args.normalize,
    )

    ranks: list[float] = []
    hits = {k: 0 for k in K_VALUES}
    by_lang_ranks: dict[str, list[float]] = {}
    by_lang_hits: dict[str, dict[int, int]] = {}

    for question in questions:
        text = normalize_question(question.question)[0] if args.normalize else question.question
        candidates = find_dataset(
            text,
            conn=conn,
            model=model,
            chroma_path=settings.chroma_path,
            top_k=max(K_VALUES),
            reranker=reranker,
            rerank_pool=settings.rerank_pool,
        )
        ranked_ids = [c.id for c in candidates]
        rr = reciprocal_rank(question.expected_id, ranked_ids)
        ranks.append(rr)
        by_lang_ranks.setdefault(question.lang, []).append(rr)
        lang_hits = by_lang_hits.setdefault(question.lang, {k: 0 for k in K_VALUES})
        for k in K_VALUES:
            hit = hit_at_k(question.expected_id, ranked_ids, k)
            hits[k] += int(hit)
            lang_hits[k] += int(hit)

    conn.close()

    print(_format_metrics("OVERALL", ranks, hits))
    for lang in sorted(by_lang_ranks):
        print(_format_metrics(lang, by_lang_ranks[lang], by_lang_hits[lang]))

    n = len(ranks)
    log_event(
        logger,
        logging.INFO,
        "eval.done",
        n=n,
        mrr=round(sum(ranks) / n, 4) if n else 0.0,
        recall_at_1=round(hits[1] / n, 4) if n else 0.0,
        recall_at_5=round(hits[5] / n, 4) if n else 0.0,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
