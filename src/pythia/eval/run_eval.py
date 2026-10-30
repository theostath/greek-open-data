"""Golden-question retrieval eval: MRR and recall@k over the hybrid retriever.

Scoring helpers are pure and unit-tested; ``main`` runs the live eval against the
built indexes and prints per-language metrics. ``main(["--planner"])`` instead runs the
planner end to end over the golden and negative sets and buckets each outcome.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from config import Settings, get_settings

from pythia.ingest.db import connect
from pythia.llm import load_llm
from pythia.logging_setup import configure_logging, get_logger, log_event
from pythia.planning.models import PlanStatus, QueryPlan
from pythia.planning.normalize import normalize_question
from pythia.planning.planner import make_plan
from pythia.retrieval.embed import load_model
from pythia.retrieval.rerank import Scorer, load_reranker
from pythia.retrieval.search import find_dataset

LOGGER_NAME = "pythia.eval.run_eval"
GOLDEN_PATH = Path(__file__).parent / "golden_questions.yaml"
NEGATIVE_PATH = Path(__file__).parent / "golden_negative.yaml"
PLANNER_REPORT_DIR = Path("data") / "eval"
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


@dataclass(frozen=True)
class NegativeQuestion:
    """One question the catalogue cannot answer; the correct outcome is ``no_match``."""

    id: str
    question: str
    lang: str
    why_uncovered: str


def load_negative(path: Path | None = None) -> list[NegativeQuestion]:
    """Load and parse the negative-question set from YAML."""
    source = path or NEGATIVE_PATH
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    return [
        NegativeQuestion(
            id=str(item["id"]),
            question=str(item["question"]),
            lang=str(item["lang"]),
            why_uncovered=str(item["why_uncovered"]),
        )
        for item in raw["questions"]
    ]


class Bucket(StrEnum):
    """Outcome class of one planned question (planner gate)."""

    MATCHED_CORRECT = "MATCHED_CORRECT"
    MATCHED_WRONG = "MATCHED_WRONG"
    UNSUPPORTED_CORRECT = "UNSUPPORTED_CORRECT"
    UNSUPPORTED_WRONG = "UNSUPPORTED_WRONG"
    NO_MATCH = "NO_MATCH"
    REFUSED = "REFUSED"
    ANSWERED_WRONGLY = "ANSWERED_WRONGLY"
    DEGRADED = "DEGRADED"


def bucket(plan: QueryPlan, expected_id: str | None) -> Bucket:
    """Classify a plan; ``expected_id=None`` marks a negative question."""
    if plan.degraded:
        return Bucket.DEGRADED
    if expected_id is None:
        return Bucket.REFUSED if plan.status is PlanStatus.NO_MATCH else Bucket.ANSWERED_WRONGLY
    if plan.status is PlanStatus.NO_MATCH:
        return Bucket.NO_MATCH
    correct = plan.dataset is not None and plan.dataset.id == expected_id
    if plan.status is PlanStatus.MATCHED:
        return Bucket.MATCHED_CORRECT if correct else Bucket.MATCHED_WRONG
    return Bucket.UNSUPPORTED_CORRECT if correct else Bucket.UNSUPPORTED_WRONG


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


def _llm_base(settings: Settings) -> str:
    """Return the Ollama root URL (the OpenAI-compat ``/v1`` suffix stripped)."""
    return settings.llm_base_url.rstrip("/").removesuffix("/v1")


def _llm_reachable(settings: Settings) -> bool:
    """One short probe of the Ollama endpoint (same check as the web app's healthz)."""
    with suppress(Exception):
        import httpx

        return bool(httpx.get(f"{_llm_base(settings)}/api/tags", timeout=2.0).status_code == 200)
    return False


def _ollama_digest(settings: Settings) -> str | None:
    """Return the configured model's digest from Ollama ``/api/tags``, or None on any failure.

    ``/api/show`` carries no digest; ``/api/tags`` lists one per installed model.
    """
    with suppress(Exception):
        import httpx

        resp = httpx.get(f"{_llm_base(settings)}/api/tags", timeout=5.0)
        for entry in resp.json().get("models", []):
            if settings.llm_model in (entry.get("name"), entry.get("model")):
                digest = entry.get("digest")
                return str(digest) if digest else None
    return None


def _git_sha() -> str:
    """Return the current commit SHA, or ``unknown`` outside a git checkout."""
    with suppress(Exception):
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        )
        return out.stdout.strip() or "unknown"
    return "unknown"


def _run_planner(settings: Settings, report_path: Path | None) -> int:
    """Plan every golden and negative question; print buckets, write a report, return exit code."""
    logger = get_logger(LOGGER_NAME)
    if not _llm_reachable(settings):
        print(f"LLM unreachable at {settings.llm_base_url}; start Ollama first.")
        return 2

    conn = connect(settings.catalog_db_path)
    model = load_model(settings.embedding_model)
    llm = load_llm(settings)
    items: list[tuple[str, str, str | None]] = [
        (q.id, q.question, q.expected_id) for q in load_golden()
    ]
    items += [(q.id, q.question, None) for q in load_negative()]

    rows: list[dict[str, Any]] = []
    counts = {b.value: 0 for b in Bucket}
    for qid, text, expected_id in items:
        plan = make_plan(
            text, conn=conn, model=model, chroma_path=settings.chroma_path, llm=llm,
            settings=settings,
        )
        result = bucket(plan, expected_id)
        counts[result.value] += 1
        dataset_id = plan.dataset.id if plan.dataset is not None else None
        rows.append({"id": qid, "bucket": result.value, "dataset_id": dataset_id})
        print(f"{qid:<5} {result.value:<20} {dataset_id or '-'}")
    conn.close()

    print()
    for name, count in counts.items():
        print(f"{name:<20} {count}")

    path = report_path or (
        PLANNER_REPORT_DIR / f"planner-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "git_sha": _git_sha(),
        "llm_model": settings.llm_model,
        "llm_digest": _ollama_digest(settings),
        "counts": counts,
        "questions": rows,
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {path}")
    log_event(logger, logging.INFO, "eval.planner.done", **counts)

    if counts[Bucket.DEGRADED.value]:
        return 3
    if counts[Bucket.MATCHED_WRONG.value]:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the golden-question eval against the built indexes; print metrics.

    ``--normalize`` (default) applies the Phase 4 query normalization (Greeklish→Greek)
    before retrieval; ``--no-normalize`` reproduces the raw Phase 3 baseline for the
    ADR-0005 off-vs-on comparison.
    """
    parser = argparse.ArgumentParser(description="Golden-question retrieval eval.")
    parser.add_argument("--normalize", action="store_true", default=True)
    parser.add_argument("--no-normalize", dest="normalize", action="store_false")
    parser.add_argument("--planner", action="store_true", help="run the planner end to end")
    parser.add_argument("--report", type=Path, default=None, help="planner JSON report path")
    args = parser.parse_args(argv)
    if args.planner and not args.normalize:
        parser.error("--planner always normalises; it cannot be combined with --no-normalize")

    configure_logging()
    logger = get_logger(LOGGER_NAME)
    settings = get_settings()
    if args.planner:
        return _run_planner(settings, args.report)
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
