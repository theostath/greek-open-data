# ADR 0003 — Evaluation: local retrieval metrics + RAGAS for faithfulness

- **Status:** Accepted (lands with the Phase 3 golden set) · 2026-06-01
- **Context:** CLAUDE.md mandates eval-driven development and "grounded-or-silent" but names no
  framework. Retrieval quality and answer faithfulness need to be measurable.
- **Decision:** Score retrieval with local, LLM-free metrics (recall@k, MRR) over the golden
  set; add RAGAS for faithfulness/answer-relevancy. Note: RAGAS judges via an LLM, so it makes
  paid API calls **during dev eval runs only** (not at serve time) — acceptable, dev-loop only.
