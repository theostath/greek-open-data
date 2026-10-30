# Intent: close the dense-recall gap

## Problem

The dense leg of hybrid retrieval returns the wrong neighbours far more often than an
approximate index should. On the 26-question golden set, Chroma's HNSW search returns only
**71% of the true top-10** nearest vectors for each query, even though the collection
reports `ef_search=100`. Ranking the *same stored vectors* exactly, with lexical BM25 and
RRF fusion unchanged, moves golden-set MRR from **0.544 to 0.700**, R@1 from 0.46 to 0.58
and R@10 from 0.69 to 0.92. The Greeklish slice's R@10 doubles (0.43 → 0.86).

Retrieval R@1 is the measured ceiling on end-to-end answers (CLAUDE.md §8: planner
MATCHED-correct is bounded by R@1), so recall lost in the ANN index is answers lost.

Nobody yet knows *why* recall is this low: index state left by past upserts and deletes,
HNSW parameters (`max_neighbors=16`, `ef_construction=100`) that are too weak for
1024-dim e5-large, or `ef_search` not being applied at query time.

## Proposed outcome

A question's dense candidates are the true nearest neighbours of its embedding, or so close
that the golden-set metrics cannot tell the difference. The eval reports dense recall
against exact so that this cannot silently regress again. Query latency stays in
milliseconds, and `make index` stays incremental.

## Affected users and systems

- Every question asked through the web app or `make answer`: retrieval feeds the planner.
- `src/pythia/retrieval/embed.py` (Chroma build + `dense_search`), `search.py`
  (`find_dataset`), `index.py` (`make index`), `eval/run_eval.py`.
- `data/chroma/` (gitignored local store) and whatever replaces or augments it.
- `api/dev.py` preflight and `/healthz`, which check that the index exists.
- ADR-0001 (names Chroma for the dense leg); CLAUDE.md §3 (names Chroma in the stack).
- The three `api/app.py::EXAMPLES` questions, which must be re-verified after any
  retrieval change (CLAUDE.md §10).

## Constraints

- Local-first: no managed service, runs on the laptop (CLAUDE.md §2.3).
- `make index` must stay incremental. A full e5-large re-embed costs ~98 min of CPU
  (`docs/benchmarks/embedding-index-build.md`), so the existing 21,806 vectors must be
  reusable without re-embedding.
- Must coexist in RAM with the 2.2 GB e5-large model and Ollama on the dev box.
- Eval-gated: `make eval` numbers in the PR, reported against the 0.544 baseline (CLAUDE.md §9).
- `make check` and CI stay green, including offline test runs on e5-small.
- **Chroma is not a fixed technology** (user, 2026-10-30): the fix may replace it with exact
  search over a plain vector store if the evidence supports that. Doing so supersedes the
  dense-store part of ADR-0001 and changes CLAUDE.md §3.
- Dependency licences: as of 2026-10-30 the user accepts copyleft (GPL/AGPL) dependencies;
  CLAUDE.md §1 still says otherwise and needs reconciling separately.

## Open questions

- **Diagnose before choosing:** is the gap Chroma's index *state* (fixed by a clean rebuild),
  its *parameters* (fixed by reconfiguring), or inherent to HNSW at this M/dim? A clean
  rebuild costs 98 min, so can it be tested by rebuilding from the stored vectors without
  re-embedding?
- Exact search, tuned Chroma, or both? Exact is 1.4 ms at 21,806 rows; at what corpus size
  does that stop being true, and does it matter for a ~22k catalogue that grows slowly?
- If exact: where do the vectors live (keep Chroma as the store and read them out, a `.npy`
  beside the catalogue, a BLOB column in `catalog.sqlite`)? What owns incrementality and
  deletions, and how is a half-written store detected?
- Load cost: reading ~89 MB into RAM at startup, and how it is shared between the web app's
  worker threads.
- If Chroma goes, does `chromadb` leave the dependency list in the same change, along with
  the tombstone logic in `build_chroma_index`? Replacing it is allowed; the scope of the
  removal is not yet decided.
- Does fixing dense recall change the reranker trade-off (ADR-0002) or the planner's score
  floors, which were tuned against the current, lossy dense leg?
- What happens to `datasets_tombstoned` (#14 item 2)?

## Origin

`review pattern`: a measurement finding. During the LEANN evaluation (#37,
`docs/benchmarks/leann-evaluation.md`) the dense leg was swapped between Chroma, LEANN and
exact search over identical vectors, with fusion fixed. Exact search beat Chroma by
+0.156 MRR, and Chroma's ANN recall@10 against exact was 0.712 (0.721 at 50). Tracked as #38.
