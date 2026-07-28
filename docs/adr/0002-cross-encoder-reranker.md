# ADR 0002 — Cross-encoder reranker (eval-gated)

- **Status:** **Accepted, default-off** · gate run 2026-07-28 (originally Proposed 2026-06-01)
- **Context:** A cross-encoder reranker over the top-k candidates is the largest single
  accuracy lever in 2026 RAG, and reliable dataset *selection* is core to Pythia's
  "deterministic selection, LLM only synthesizes" principle. Risk: Greek-language quality.
- **Decision:** Rerank ~20→5 hybrid candidates with a multilingual cross-encoder
  (e.g. BGE-reranker-v2-m3). Gate adoption on the golden set; if it does not beat hybrid-only
  on Greek questions, drop it. Adds one local model + a few hundred ms/query.
- **Implementation:** `pythia.retrieval.rerank` (`load_reranker`, `rerank`) scores
  `(question, embed_text)` pairs with `sentence_transformers.CrossEncoder`
  (`BAAI/bge-reranker-v2-m3`, no new dependency). `find_dataset` accepts an optional
  `reranker`: when supplied it reorders the top `rerank_pool` fused candidates down to
  `top_k`, otherwise fusion alone ranks. Config: `rerank_enabled` (default off),
  `rerank_model`, `rerank_pool`. Unit-tested offline with a fake scorer.
## Eval result (2026-07-28, n=26, e5-large, tombstone-free index)

| Config | OVERALL MRR | R@1 | el | en | greeklish |
|---|---|---|---|---|---|
| hybrid only | 0.515 | 0.42 | 0.595 | 0.571 | 0.319 |
| **+ reranker** | **0.652** | **0.62** | **0.729** | **0.714** | **0.457** |

**Quality gate: passed decisively.** +0.137 MRR overall, R@1 0.42→0.62, and every language
slice improves — including `el` (0.595→0.729), the regression risk this ADR was written to
guard against. Well outside the ~0.04 noise floor that one question represents at n=26.

**Cost gate: failed on current hardware.** The original estimate of "a few hundred ms/query"
was wrong by two orders of magnitude. Measured on the Phase 4 gate run (Core Ultra 9 285H,
`torch+cpu`, 16 threads): **774 s for 26 questions ≈ 28 s/query** — `rerank_pool=20` pairs
per query × ~12 CPU-seconds per pair for a 560M-param XLM-R cross-encoder at 512 tokens.
See `docs/benchmarks/embedding-index-build.md`.

## Decision (revised)

Accept the reranker as the correct quality lever, but keep **`rerank_enabled` default
false** until the latency is solved. 28 s/query is unusable for an interactive assistant.
Re-open with a GPU, a smaller cross-encoder, or a substantially reduced `rerank_pool`
(the quality/latency knee across pool sizes has **not** been measured — that is the next
experiment, and it may well recover most of the gain far more cheaply).
