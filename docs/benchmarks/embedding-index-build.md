# Benchmark — full e5-large index build (21,806 datasets, CPU)

_Measured 2026-07-28. Purpose: give `make index` a real cost so future decisions
(re-embedding, model swaps, GPU) can be reasoned about instead of guessed._

## Machine

| | |
|---|---|
| Model | ASUS Zenbook 14 UX3405CA |
| CPU | Intel Core Ultra 9 285H — 16 cores / 16 threads, 2.9 GHz max |
| RAM | 31.4 GB LPDDR5X-8533 (8.7 GB free at build start) |
| GPU | Intel Arc 140T iGPU — **unused, see below** |
| OS | Windows 11 Pro 10.0.26200 (build 26200) |
| Python | 3.12.10 (AMD64) |

**The GPU sat idle.** The venv has `torch 2.12.0+cpu`, so `torch.cuda.is_available()`
is `False` and `sentence-transformers` reports "No device provided, using cpu". All
21,806 embeddings ran on the CPU across 16 threads. This is the single largest lever
on the numbers below.

| Library | Version |
|---|---|
| sentence-transformers | 5.5.1 |
| torch | 2.12.0+cpu |
| transformers | 5.9.0 |
| chromadb | 1.5.9 |
| numpy | 2.4.6 |

## Workload

- Model: `intfloat/multilingual-e5-large` (2.13 GB `model.safetensors`, 1024-dim output,
  512-token window).
- Input: `datasets.embed_text` for all **21,806** catalog rows, `passage:`-prefixed and
  L2-normalized (`retrieval/embed.py`).
- Batching: 256 datasets per Chroma upsert chunk (86 chunks), each encoded by
  sentence-transformers in 8 sub-batches of 32.
- Also rebuilt the SQLite FTS5 lexical index over the same 21,806 rows.
- Command: `uv run python -m pythia.retrieval.index` (i.e. `make index`).

## Result

**Total wall-clock: 97 min 40 s** (`index.start` 19:33:47 UTC → `index.done` 21:11:27 UTC),
producing `dense=21806, lexical=21806`.

| Metric | Value |
|---|---|
| Total wall-clock | **5,860 s (1h 37m 40s)** |
| Sum of embed chunks | 5,664 s (97%) |
| Non-embed overhead | ~196 s (model load, Chroma open, FTS5 rebuild) |
| Throughput | **~3.7 datasets/sec** (0.269 s/dataset) |
| Per 256-dataset chunk | mean **66.6 s**, median 64 s |
| Chunk spread | p25 56 s · p75 76 s · max 176 s |

### Throughput drifts downward over the run

| Segment | Mean per 256 |
|---|---|
| First third | 58.2 s |
| Last third | 71.2 s |

A **~22% slowdown** from start to finish. This is most likely thermal throttling in a
thin chassis after an hour at full load on all 16 threads — note the first third was
*faster* despite competing with two large downloads (see caveat), so the trend cannot be
explained by external load. **Do not extrapolate from a short sample**: a 5-minute
benchmark on a cool machine will overstate sustained throughput by roughly a fifth.

### Caveat on the first ~10 minutes

The first ~10 minutes overlapped with a 6.6 GB `ollama pull` and a 2.2 GB reranker
download competing for disk and CPU. Individual sub-batches degraded to ~18 s/it during
that window versus ~8 s/it clean. The effect on the total is small (the affected span is
a fraction of the run, and the early chunks were still the fastest overall), but a fully
idle build would likely land modestly under 97 minutes.

## Related cost: cross-encoder reranking (ADR-0002)

Measured on the same machine, same run of the Phase 4 eval gate:

| | |
|---|---|
| Model | `BAAI/bge-reranker-v2-m3` (XLM-R-large, ~560M params) |
| Work per query | `rerank_pool = 20` pairs, up to 512 tokens each |
| Wall-clock | **774 s for 26 questions** → **~28 s per query** |
| Utilization | ~10.8 of 16 cores, 7.5 GB resident |

~12 CPU-seconds per pair. The quality win is large (+0.137 MRR), but **28 s/query is not
viable for an interactive assistant** on CPU. Making it usable needs a GPU, a smaller
cross-encoder, or a much smaller `rerank_pool` — record whichever is chosen in ADR-0002.

## Rules of thumb

- Full CPU re-embed of the current catalog: **budget ~1h 40m**, plus headroom if the
  machine is doing anything else.
- Roughly **4.5 min per 1,000 datasets** sustained.
- e5-small (384-dim, 449 MB) is far cheaper and is what the test suite pins; the Phase 3
  baseline showed it also retrieves worse (MRR 0.48 vs 0.53). Cost/quality, not a free win.
- A CUDA GPU would cut this by roughly 10–50×. On this box that would mean a different
  torch build and a discrete GPU — the Arc iGPU is not usable via the current stack.

## Why the full re-embed was needed at all

The pre-existing index held **20,992** vectors against a 21,806-row catalog (814 missing)
and carried **no `sig` metadata**, so `build_chroma_index`'s content-signature check saw
every row as changed. Incremental indexing only pays off once signatures exist; this run
wrote them, so the next `make index` should re-embed ~0.

**Follow-up worth knowing:** because this run *upserted over* the existing collection
rather than rebuilding it, the HNSW graph ended up with 20,992 tombstones alongside
21,806 live vectors (`max seq_id` 42,798). That made ANN results **nondeterministic
across processes** — the same eval command returned MRR anywhere from 0.483 to 0.526,
noise larger than the effects the Phase 4 eval gate needed to measure. Copying the stored
vectors into a fresh collection (no re-embedding — the vectors are already persisted,
~1 min) restored exact reproducibility. Consider making `build_chroma_index` drop and
recreate the collection when every row is changed, rather than upserting in place.
