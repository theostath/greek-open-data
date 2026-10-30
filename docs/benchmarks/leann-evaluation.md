# Benchmark — LEANN as a replacement for the Chroma dense index

_Measured 2026-10-30 on the full catalogue (21,806 datasets, `intfloat/multilingual-e5-large`)
and the 26-question golden set. Verdict: **not adopted**. The run also exposed a dense-recall
gap in our current Chroma index that is worth far more than LEANN's storage saving (#38)._

## What LEANN does

[LEANN](https://github.com/StarTrail-org/LEANN) (`leann` 0.3.8, MIT) does not store embedding
vectors. It keeps a pruned, CSR-compacted HNSW graph plus the raw passage text, and at query
time **recomputes the embedding of every graph node the search visits** by running the
embedding model. A separate embedding-server process does the recomputation over ZMQ. With
`use_daemon=True` (the default) it runs detached, with a 900 s idle TTL.

## Method

The comparison isolates the index, holding everything else fixed:

1. Exported the 21,806 stored vectors from the live Chroma `datasets` collection, together
   with each dataset's `embed_text`.
2. Built a LEANN HNSW index from **those exact vectors**
   (`LeannBuilder.build_index_from_arrays`, `distance_metric="cosine"`, recompute + compact
   defaults), storing `passage: <embed_text>` so recomputation reproduces our E5 input.
3. Ran each golden question through the production query normalization and searched with
   `query: <text>`, `top_k=50`, at `complexity` 50, 64 and 128.
4. Replaced the dense leg of `find_dataset` with each source's top-50 and ran the real
   `run_eval` scoring, so lexical BM25 + RRF fusion is unchanged. Sources: Chroma, LEANN,
   and exact brute-force cosine.

**Why the recomputation was served from stored vectors.** Real e5-large recomputation on this
CPU failed on the first query (see Latency). To still measure LEANN's *ranking*, its
`compute_embeddings` was replaced, benchmark-only, with a lookup into the stored vectors.
This is faithful: re-embedding 30 random passages with e5-large reproduced the stored
vectors at **cosine 1.000000** (min and mean). LEANN's graph and search code ran unmodified,
and the lookup logged every node it was asked to recompute.

Machine: as in [`embedding-index-build.md`](embedding-index-build.md) (Core Ultra 9 285H,
CPU-only torch). LEANN ran in an isolated venv (`torch` 2.15.0, `transformers` 4.57.6) so the
project environment was not touched.

## Storage

| Index | On disk |
|---|---|
| Chroma `data/chroma` today | **204 MB** |
| — live `datasets` collection | ~104 MB (91 MB HNSW `data_level0.bin` + its share of the 26 MB SQLite) |
| — leftover `datasets_tombstoned` collection | 87 MB + its SQLite rows (dead weight, #14 item 2) |
| **LEANN, same 21,806 vectors** | **20.4 MB** |
| — pruned graph (`.index`) | 2.2 MB |
| — passage text (`.passages.jsonl` + `.idx`) | 17.4 MB, a duplicate of `embed_text` already in `catalog.sqlite` |
| — id map (`.ids.txt`) | 0.8 MB |

LEANN saves ~84 MB against the live index. Deleting the stale collection saves about as much,
with no code change.

## Accuracy

Golden set, n=26, Greeklish normalization on, reranker off. "ANN recall@10" is the overlap of
each source's dense top-10 with the exact top-10, averaged over the questions.

| Dense source | MRR | R@1 | R@3 | R@10 | ANN recall@10 |
|---|---|---|---|---|---|
| Chroma (production) | 0.544 | 0.46 | 0.62 | 0.69 | 0.71 |
| LEANN, complexity 50 | 0.570 | 0.46 | 0.65 | 0.73 | 0.68 |
| LEANN, complexity 64 | 0.607 | 0.50 | 0.69 | 0.77 | 0.78 |
| LEANN, complexity 128 | 0.601 | 0.50 | 0.69 | 0.77 | 0.80 |
| **Exact brute force** | **0.700** | **0.58** | **0.81** | **0.92** | 1.00 |

The Chroma row reproduces the recorded production baseline (MRR 0.544), which validates the
harness. LEANN's ranking is somewhat better than Chroma's, but both are approximate. On these
vectors, exact search beats both by a wide margin: the Greeklish slice goes from R@10 0.43
(Chroma) to 0.86 (exact).

## Latency

| | Per query |
|---|---|
| Chroma HNSW | 7 ms median |
| Exact brute force (numpy, 21,806 × 1024 float32 in RAM) | 1.4 ms |
| LEANN graph traversal + IPC only (recompute served from lookup) | 417 ms median, 603 ms p90 (complexity 64) |
| **LEANN with real e5-large recomputation** | **failed**; estimated at minutes |

LEANN asked for **79,067 node recomputations across 79 searches, about 1,000 per query**.
e5-large on this CPU runs at 0.269 s per passage
([`embedding-index-build.md`](embedding-index-build.md)). That puts a query at roughly 270 s
before any batching gains, against a planner budget measured in seconds. The real run
confirmed this: the first 22-node recompute batch exceeded the C++ client's ZMQ receive
timeout (`zmq_msg_recv failed: 11`), and the search process died. LEANN is designed for
GPU or Apple-silicon embedding and for corpora where the vectors do not fit; ours fit in
89 MB of RAM.

## Dependencies

- Installs natively on Windows from prebuilt `win_amd64` wheels; no compiler needed.
- `leann-core` pins `transformers<4.58`, and this project runs `transformers` 5.9.0.
  Adopting LEANN would force a major-version downgrade under the production embedder.
- The `leann` metapackage pulls 173 packages (~1.6 GB of site-packages), including
  `llama-index-core`, `kubernetes`, `onnxruntime` and `faiss-cpu`, plus all four backends.
- It adds a second long-lived process holding its own copy of the 2.2 GB model, beside the
  one `pythia-dev` already loads.

## Verdict

**Not adopted.** Query-time recomputation with e5-large on CPU is two to three orders of
magnitude too slow, and the storage it saves (~84 MB) is matched by deleting a stale
collection. The finding that matters is that **exact dense search over our current vectors
lifts MRR from 0.544 to 0.700 at 1.4 ms per query**. That is tracked as #38. Revisit LEANN
only if the corpus grows past what fits in memory *and* embedding moves to a GPU.
