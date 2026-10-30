# ADR 0010: Exact dense search over SQLite-stored vectors

## Status

Accepted · 2026-10-30

Supersedes the dense-store clause of ADR-0001 ("retrieve dense candidates from Chroma") and
the "Vector store: Chroma" line of `CLAUDE.md §3`. Does **not** change ADR-0001's decision to
fuse dense and BM25 candidates with RRF.

## Context

The dense leg of hybrid retrieval was missing most of its true neighbours. On the
26-question golden set, Chroma's HNSW index returned **71% of the exact top-10**. Ranking the
*same stored vectors* exactly, with BM25 and RRF unchanged, moved MRR from **0.544 to 0.700**,
R@1 from 12/26 to 15/26 and R@10 from 18/26 to 24/26
([`docs/benchmarks/leann-evaluation.md`](../benchmarks/leann-evaluation.md)).

A probe ([`docs/benchmarks/dense-recall-probe.md`](../benchmarks/dense-recall-probe.md)) asked
why:

- **Not index state.** Fresh rebuilds from the same vectors scored 0.63–0.71 with default
  settings.
- **Not a setting we can turn.** `modify(ef_search=400)` on the persisted collection changed
  no result. The best fresh configuration reached 0.86, and fresh builds vary between runs.
- **Structural.** The same indexes found 95–97% of neighbours for queries shaped like
  passages. E5 `query:` vectors sit away from the passages they should match, a workload
  HNSW's greedy search handles badly.

At 21,806 × 1024 float32 (~89 MB), a full scan costs 1.1–1.9 ms per query, no slower than
any HNSW configuration tested.

## Decision

**The dense leg is an exact cosine scan over an in-memory float32 matrix, persisted in a
`dataset_embeddings` table in `data/catalog.sqlite`. Chroma and `chromadb` are removed.**

- `retrieval/dense.py` owns the table (`ensure_dense_schema`), the build, a one-off import
  from the old Chroma store, and the `DenseIndex` snapshot.
- Rows carry the model name, dimension, a content `sig` and an `embedded_at` timestamp.
  `make index` stays incremental: it re-embeds only rows whose text or model changed.
- Search is exact and tie-safe. Rows load in `dataset_id` order, and ties at the k boundary
  resolve by id. 1,380 datasets share identical text with another, so this decides which
  twin gets cited.
- Each process loads one snapshot. A re-index takes effect on restart, and `/healthz`
  reports the loaded `(count, version)` beside the table's, so a missed restart shows as
  `degraded`.

## Rationale

- **Recall 1.0 by construction, at no latency cost.** The quality gain (+0.156 MRR) is
  about four times the golden set's one-question noise floor.
- **No ANN parameter could close the gap**, and the one runtime knob did not apply to the
  persisted store.
- **SQLite is already the project's store** (CLAUDE.md §3: one file, committed schema).
  Writes are transactional, so a crashed 98-minute build resumes from its last committed
  batch. Chroma upserts also left HNSW tombstones that made results drift between processes.
- **One heavy dependency fewer.** `chromadb` brought `onnxruntime`, `grpcio`, `kubernetes`
  and more.
- **Rejected:** tuning Chroma (best 0.86, not reproducible); a `.npy` sidecar (two files to
  keep consistent, non-atomic writes); Chroma for storage with numpy for search (keeps the
  dependency and slow full loads); LEANN (minutes per query on CPU, see the LEANN benchmark).

## Consequences

- **Linear cost.** About 4 KB of RAM per dataset and about 1.4 ms per 21.8k rows. At 200k
  datasets that is ~800 MB and ~15 ms. Revisit past ~500k rows or above 1024 dimensions,
  with IVF, float16, or an ANN index plus an exact re-check.
- **The catalogue grows from ~134 MB to ~235 MB.** Each 4 KB vector spills to its own
  overflow page.
- **Deleting `catalog.sqlite` now deletes the vectors**, about 98 CPU-minutes to rebuild.
  They used to survive in `data/chroma/`.
- **Restart after `make index`.** Live reload was judged unnecessary for a manual, rare
  operation; `/healthz` makes a missed restart visible.
- **One-time migration.** `scripts/migrate_chroma_vectors.py` copies the existing vectors
  across without re-embedding, after a sample re-embed check. It and
  `scripts/probe_dense_recall.py` are deleted in the following release.
- **Downstream calibration.** ADR-0002 (reranker) and ADR-0005 (normalisation) were measured
  on the lossy dense leg; both are re-measured in the PR that lands this decision.
