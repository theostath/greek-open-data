# Benchmark — why Chroma's dense recall is low

_Measured 2026-10-30. Evidence for ADR-0010 (exact dense search). Reproduce with
`uv run --with chromadb==1.5.9 python scripts/probe_dense_recall.py`._

## Question

The LEANN evaluation ([`leann-evaluation.md`](leann-evaluation.md)) found that Chroma's dense
leg returns only 71% of the exact top-10 on the golden set, and that exact search over the
*same stored vectors* lifts MRR from 0.544 to 0.700. This probe asks whether that gap is
**index state** (fixed by a rebuild), **parameters** (fixed by tuning), or **structural**.

## Method

- **Vectors:** the 21,806 e5-large vectors of the live `datasets` collection, read from a
  temporary *copy* of `data/chroma`. The probe calls `modify()`, so it never opens the live
  store.
- **Golden queries (n=26):** each golden question, normalised as in production
  (`normalize_question`), embedded with the `query:` prefix.
- **Perturbed-passage queries (n=300):** stored vectors sampled with seed 0, plus Gaussian
  noise (σ=0.02), re-normalised. They are queries that *look like passages*.
- **Exact top-k:** brute-force cosine over all 21,806 vectors.
- **Recall@k:** the fraction of the exact top-k each index returns in its own top-k,
  averaged over the queries.
- **Indexes:** the live copy as built (`ef_search=100`, `max_neighbors`=M=16,
  `ef_construction`=100); the same copy after `modify(ef_search=400)`; three fresh in-memory
  rebuilds from the same vectors (defaults; `ef_search=400`; M=32, efc=200, efs=200);
  numpy exact.
- **Versions:** chromadb 1.5.9, numpy 2.4.6, Python 3.12.10. Machine as in
  [`embedding-index-build.md`](embedding-index-build.md).

## Results

Recall@10 on the golden queries. Fresh rebuilds are **not deterministic**: HNSW insertion
uses random level assignment. So two runs are shown.

| Index | Golden, run 1 | Golden, run 2 | Perturbed (run 2) | ms/query |
|---|---|---|---|---|
| live copy | 0.712 | 0.712 | 0.963 | 0.7–1.0 |
| live copy after `modify(ef_search=400)` | 0.712 | 0.712 | 0.963 | 0.7–0.9 |
| fresh, defaults | 0.631 | 0.712 | 0.950 | 0.6–0.8 |
| fresh, `ef_search=400` | 0.865 | 0.862 | 0.967 | 1.1–2.3 |
| fresh, M=32 / efc=200 / efs=200 | 0.823 | 0.700 | 0.966 | 1.0–2.0 |
| **exact (numpy)** | **1.000** | **1.000** | **1.000** | **1.1–1.9** |

Per-question golden recall@10 for the live copy (run 2). The misses are concentrated in a
few questions, not spread evenly:

```
q01=1.0 q02=1.0 q03=1.0 q04=0.9 q05=0.1 q06=0.6 q07=1.0 q08=1.0 q09=0.9 q10=1.0
q11=1.0 q12=1.0 q13=0.9 q14=0.9 q15=0.0 q16=1.0 q17=0.1 q18=0.1 q19=0.0 q20=0.8
q21=0.5 q22=0.9 q23=0.7 q24=1.0 q25=0.1 q26=1.0
```

## Findings

1. **It is not index state.** Fresh rebuilds from the same vectors score 0.63–0.71 with
   default settings, no better than the live store.
2. **It is not a setting we can turn.** `modify(ef_search=400)` on the persisted collection
   changes the reported configuration but not one result. The best fresh configuration,
   `ef_search=400`, reaches only 0.86, and it varies by run.
3. **It is structural.** The same indexes find 95–97% of neighbours for queries shaped like
   passages, but 63–86% for real E5 `query:` vectors. Those query vectors sit away from the
   passages they should match, an out-of-distribution workload where HNSW's greedy graph
   search is known to struggle. Seven questions (q05, q15, q17, q18, q19, q21, q25) lose half
   or more of their true neighbours.
4. **Exact search costs nothing here.** At 21,806 × 1024 float32 (~89 MB), a full scan is
   1.1–1.9 ms per query, no slower than any HNSW configuration.

**Caveat:** the golden column rests on 26 queries; one question is ~0.04 of recall. The
0.29 gap between the live index and exact search is far outside that.
