# ADR 0005 — Query-side Greeklish→Greek transliteration (eval-gated)

## Status

**Accepted for the no-reranker configuration** · gate run 2026-07-28 (Proposed 2026-07-13)

## Context

The Phase 3 retrieval eval shows Greeklish is the weak spot: **greeklish MRR ~0.30** vs
**el ~0.55 / en ~0.71** (`plan.md`). Greeklish (Greek written in Latin letters, e.g.
*"posa kroysmata"*) neither matches the Greek dense embeddings nor the Greek FTS tokens.
Phase 4 introduces a query-normalization step before retrieval, which is the natural place
to fix this.

## Decision

Add a pure, query-side normalization step in `planning/normalize.py`:

1. `detect_language(q)` → `el | en | greeklish` using a **scoring margin** (Greeklish-only
   cluster density vs common-English signal), defaulting to `en` on ambiguity so English is
   **never** transliterated.
2. `transliterate_greeklish(q)` (greeklish path only) → longest-match-first digraph table
   then single chars, with word-final sigma handling. Lossy and best-effort.

Only the **greeklish** path is transliterated; `el` and `en` questions pass through
unchanged. We deliberately do **not** accent-fold the query for the dense arm, because the
Chroma index was built on raw accented `passage:` text and folding one side only is a
train/serve mismatch (the FTS arm already folds symmetrically via `unicode61
remove_diacritics 2`).

## Rationale

- Transliteration is the highest-value lever for the worst-performing language slice, and it
  is deterministic/testable — no LLM, no cost.
- Defaulting ambiguous ASCII to `en` protects the strong `en` slice from being corrupted by
  a false-positive transliteration (real English is dense with cues like `th`, `x`, `-is`).

## Eval result (2026-07-28, n=26, e5-large, tombstone-free index)

| Config | OVERALL MRR | el | en | greeklish MRR | greeklish R@1 |
|---|---|---|---|---|---|
| reranker OFF, norm OFF | 0.515 | 0.595 | 0.571 | 0.319 | 0.29 |
| **reranker OFF, norm ON** | **0.544** | 0.595 | 0.571 | **0.429** | **0.43** |
| reranker ON, norm OFF | **0.652** | 0.729 | 0.714 | **0.457** | 0.43 |
| reranker ON, norm ON | 0.644 | 0.729 | 0.714 | 0.429 | 0.43 |

**Gate passed without a reranker:** greeklish MRR +0.110 (+34% relative), and `el`/`en` are
**bit-identical** with normalization on and off — direct confirmation that the `en`-safe
margin rule never transliterates Greek or English questions, which was the main risk here.

**But it does not stack with the reranker.** With reranking on, normalization is neutral-to-
slightly-negative (0.652 → 0.644; greeklish 0.457 → 0.429). The two fix the same weakness:
the cross-encoder already reads Greeklish well enough to recover the right dataset, and
lossy transliteration then discards signal it could have used. Note the 0.008 overall gap is
*within* the noise floor (one question ≈ 0.04 MRR at n=26), so treat the two reranker-on
arms as tied rather than ranked; the greeklish-slice drop is the more meaningful signal.

One honest caveat: greeklish **R@10 fell 0.57 → 0.43** with normalization even in the
winning no-reranker arm. Transliteration pulls correct answers much higher when it finds
them, but one question dropped out of the top 10 entirely. At n=7 greeklish questions that
is a single item — the golden set is too small to resolve this. **Expanding the golden set
is a precondition for trusting any refinement of this decision.**

## Consequences

- **Adopted in the shipping configuration** (`rerank_enabled=false`, ADR-0002), where it is
  a clear win. Kept as a normalization step that is trivially skippable
  (`run_eval --no-normalize`) so the pairing can be re-measured if reranking is ever enabled.
- **If ADR-0002's reranker is enabled**, re-measure before assuming normalization still
  helps; current evidence says it does not.
- Transliteration is lossy (many Greeklish spellings map to one Greek form); it improves
  recall, not perfect reconstruction.
- Adds a small pure module + unit tests, including explicit English "must-not-transliterate"
  cases drawn from the golden set.
