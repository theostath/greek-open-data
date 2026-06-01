# ADR 0001 — Hybrid retrieval (dense + BM25, RRF fusion)

- **Status:** Accepted (implement in Phase 3) · 2026-06-01
- **Context:** Pure dense retrieval blurs exact-match dataset ids, Greek named entities, and
  Greeklish/English tokens — the dominant failure mode over ~22k terse metadata records.
  BM25 + dense fused with Reciprocal Rank Fusion is the 2026 baseline.
- **Decision:** Retrieve dense candidates from Chroma and lexical candidates from SQLite FTS5
  (BM25), fuse with RRF. Zero new dependency (FTS5 ships with SQLite); stays local-first.
