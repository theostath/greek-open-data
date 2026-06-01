# ADR 0002 — Cross-encoder reranker (eval-gated)

- **Status:** Proposed (adopt only if the Phase 3 golden-set eval confirms a gain) · 2026-06-01
- **Context:** A cross-encoder reranker over the top-k candidates is the largest single
  accuracy lever in 2026 RAG, and reliable dataset *selection* is core to Pythia's
  "deterministic selection, LLM only synthesizes" principle. Risk: Greek-language quality.
- **Decision:** Rerank ~20→5 hybrid candidates with a multilingual cross-encoder
  (e.g. BGE-reranker-v2-m3). Gate adoption on the golden set; if it does not beat hybrid-only
  on Greek questions, drop it. Adds one local model + a few hundred ms/query.
