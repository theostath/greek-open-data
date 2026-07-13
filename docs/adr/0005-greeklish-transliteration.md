# ADR 0005 — Query-side Greeklish→Greek transliteration (eval-gated)

## Status

Proposed · 2026-07-13 (adopt only if the golden-set eval confirms a gain with no `el`/`en`
regression — mirrors the ADR-0002 eval gate)

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

## Consequences

- **Eval-gated:** adoption is contingent on `make eval` (normalization off vs on) showing a
  **greeklish lift with no `el`/`en` regression**. If `en`/`el` regress, transliteration is
  kept behind a flag or dropped; the measured decision is recorded here when the eval runs.
- Transliteration is lossy (many Greeklish spellings map to one Greek form); it improves
  recall, not perfect reconstruction.
- Adds a small pure module + unit tests, including explicit English "must-not-transliterate"
  cases drawn from the golden set.
