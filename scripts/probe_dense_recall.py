"""Probe why Chroma's dense recall is low: live store vs fresh rebuilds vs exact search.

Evidence for ADR-0010 (see docs/benchmarks/dense-recall-probe.md). Needs chromadb, which the
project no longer depends on:

    uv run --with chromadb==1.5.9 python scripts/probe_dense_recall.py

The live store is copied to a temporary directory first, because the probe calls
``modify()`` on the collection. Delete this script with scripts/migrate_chroma_vectors.py.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # config.py lives at the root

import chromadb  # noqa: E402
import numpy as np  # noqa: E402
from chromadb.config import Settings as ChromaSettings  # noqa: E402

from pythia.eval.run_eval import load_golden  # noqa: E402
from pythia.planning.normalize import normalize_question  # noqa: E402
from pythia.retrieval.embed import embed_query, load_model  # noqa: E402

SEED = 0
N_PERTURBED = 300
NOISE_SIGMA = 0.02
CONFIGS: list[tuple[str, dict[str, Any]]] = [
    ("fresh, defaults", {"space": "cosine"}),
    ("fresh, ef_search=400", {"space": "cosine", "ef_search": 400}),
    ("fresh, M=32 efc=200 efs=200",
     {"space": "cosine", "max_neighbors": 32, "ef_construction": 200, "ef_search": 200}),
]


def recall(found: list[list[str]], exact: np.ndarray, ids: np.ndarray, k: int) -> list[float]:
    """Per-query overlap of the found top-k with the exact top-k."""
    return [len(set(f[:k]) & set(ids[e[:k]])) / k for f, e in zip(found, exact, strict=True)]


def measure(label: str, coll: Any, queries: dict[str, np.ndarray],
            exact: dict[str, np.ndarray], ids: np.ndarray, golden_ids: list[str]) -> None:
    """Print recall@10/@50 and ms/query per query set; per-question recall for golden."""
    for name, q in queries.items():
        t0 = time.perf_counter()
        found = coll.query(query_embeddings=q.tolist(), n_results=50)["ids"]
        ms = (time.perf_counter() - t0) / len(q) * 1000
        r10, r50 = recall(found, exact[name], ids, 10), recall(found, exact[name], ids, 50)
        print(f"{label:<34} {name:<10} recall@10={np.mean(r10):.3f} "
              f"@50={np.mean(r50):.3f} {ms:.1f} ms/q")
        if name == "golden":
            print("    per-question recall@10: "
                  + " ".join(f"{g}={r:.1f}" for g, r in zip(golden_ids, r10, strict=True)))


def main() -> int:
    """Run the probe and print the table rows."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--chroma", default="data/chroma")
    parser.add_argument("--collection", default="datasets")
    args = parser.parse_args()

    settings = ChromaSettings(anonymized_telemetry=False)
    # Chroma keeps its HNSW files open until process exit; Windows then refuses the delete.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "chroma"
        shutil.copytree(args.chroma, copy)
        live = chromadb.PersistentClient(path=str(copy), settings=settings)
        coll = live.get_collection(args.collection)
        got = coll.get(include=["embeddings"])
        ids = np.asarray(got["ids"])
        vecs = np.asarray(got["embeddings"], dtype=np.float32)

        model = load_model()
        golden = load_golden()
        gq = np.asarray([embed_query(model, normalize_question(g.question)[0])
                         for g in golden], dtype=np.float32)
        rng = np.random.default_rng(SEED)
        pv = vecs[rng.choice(len(vecs), N_PERTURBED, replace=False)]
        pv = pv + rng.normal(0, NOISE_SIGMA, pv.shape).astype(np.float32)
        pv /= np.linalg.norm(pv, axis=1, keepdims=True)
        queries = {"golden": gq, "perturbed": pv}
        exact = {k: np.argsort(-(q @ vecs.T), axis=1)[:, :50] for k, q in queries.items()}
        golden_ids = [g.id for g in golden]

        print(f"vectors={vecs.shape} chromadb={chromadb.__version__} numpy={np.__version__}")
        print(f"live config: {coll.configuration}")
        measure("live copy", coll, queries, exact, ids, golden_ids)
        coll.modify(configuration={"hnsw": {"ef_search": 400}})
        measure("live copy after modify(efs=400)", coll, queries, exact, ids, golden_ids)

        for label, cfg in CONFIGS:
            client = chromadb.EphemeralClient(settings=settings)
            fresh = client.create_collection(f"probe{abs(hash(label))}",
                                             configuration={"hnsw": cfg})
            for i in range(0, len(ids), 5000):
                fresh.add(ids=list(ids[i:i + 5000]), embeddings=vecs[i:i + 5000])
            measure(label, fresh, queries, exact, ids, golden_ids)

        for name, q in queries.items():
            t0 = time.perf_counter()
            for row in q:
                np.argpartition(-(vecs @ row), 50)[:50]
            ms = (time.perf_counter() - t0) / len(q) * 1000
            print(f"{'exact (numpy)':<34} {name:<10} recall@10=1.000 @50=1.000 {ms:.1f} ms/q")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
