# Plan: exact dense search over vectors stored in the catalogue

- **Issue:** #38 · **Branch:** `feat/exact-dense-search` · **Intent:** `specs/intent-exact-dense-search.md`
- **Type:** enhancement (retrieval quality) + refactor (storage) · **Complexity:** medium
- **Status:** stage 2 grilled (Q1–Q12 folded in) — awaiting the judge panel

## Task Description

Replace the Chroma HNSW index behind the dense leg of `find_dataset` with **exact cosine
search** over a float32 matrix held in memory. Persist the vectors in a new table in
`data/catalog.sqlite` instead of in `data/chroma/`. Remove `chromadb` from the project. Keep
`make index` incremental, and migrate the 21,806 existing e5-large vectors without
re-embedding them, since a full re-embed costs ~98 CPU-minutes.

## Objective

When this is done:

- the dense candidates for every question are its **true** top-k nearest stored vectors
  (recall 1.0 by construction);
- golden-set MRR rises from the 0.544 baseline to about 0.700 (measured in the LEANN run:
  R@1 0.46 → 0.58, R@10 0.69 → 0.92);
- dense query latency stays in single-digit milliseconds;
- `data/chroma/` and the `chromadb` dependency are gone, along with the tombstone failure
  mode and the 204 MB store (#14 item 2).

## Problem Statement

On the 26-question golden set, Chroma returns 71% of the exact top-10. A throwaway probe on
2026-10-30, run on a copy of the store and on fresh in-memory rebuilds from the same
vectors, shows this is **structural, not index state**:

| Index | Golden-query recall@10 | Perturbed-passage-query recall@10 |
|---|---|---|
| live store (copy), `ef_search=100`, M=16 | 0.712 | 0.963 |
| same copy after `modify(ef_search=400)` | 0.712 (setting had **no effect**) | 0.963 |
| fresh rebuild, defaults | 0.631 | 0.956 |
| fresh, `ef_search=400` | 0.865 | 0.967 |
| fresh, M=32 / efc=200 / efs=200 | 0.823 | 0.967 |
| exact (numpy) | **1.000 · 1.9 ms/q** | **1.000 · 1.4 ms/q** |

HNSW does well when queries look like passages and badly on real E5 `query:` vectors, which
sit in a different region from the passages they should match: an out-of-distribution query
workload. No tested configuration reaches 0.9, and the one runtime setting that should help
cannot be applied to the persisted collection. At 21,806 rows, exact search is both perfect
and faster than the current per-call Chroma client (5–7 ms).

## Solution Approach

Three parts, each replaceable on its own:

1. **Storage: a `dataset_embeddings` table in `catalog.sqlite`.** One row per dataset:
   vector as a little-endian float32 BLOB, plus the model name, dimension, and the same
   content signature `build_chroma_index` uses today. This follows CLAUDE.md §3: SQLite as
   the one-file store, committed schema, plain-SQL DDL. Writes are transactional, so a
   crashed build leaves whole batches, never half-written vectors. Committing per batch also
   lets a 98-minute build resume where it died, which Chroma upserts never guaranteed.
   Rejected alternatives: a `.npy` + ids sidecar (two files to keep consistent, non-atomic
   writes), and Chroma as storage with numpy for search (keeps the dependency and the
   tombstone machinery, and slow full `get()` loads).

2. **Search: `DenseIndex`**, an immutable in-memory snapshot (`ids: list[str]`,
   `matrix: float32[n, d]`, `model: str`) loaded from the table. `search(query_vec, k)`
   computes `matrix @ q`, takes `argpartition` for the top-k, then sorts that slice by
   `(-score, row_index)`, so ties are deterministic. It is read-only and therefore safe to
   share across the web app's worker threads. `Pipeline` already serialises its work through
   two job slots, so at most two searches run at once. It is loaded once per process (Pipeline warm,
   eval, CLI) and passed explicitly down the call chain in place of today's `chroma_path`.

3. **Staleness: restart-only.** The snapshot is loaded once per process and never
   reloaded. A `make index` run while `pythia-dev` is serving takes effect on the next
   restart. This is documented beside `make index` in README and CLAUDE.md §7. Re-indexing
   is rare and done by hand, and today's per-call freshness was a side effect of Chroma,
   not a requirement (grilling Q2).

The migration is a one-shot script that copies vectors out of the existing Chroma store. It
runs with `chromadb` supplied ephemerally (`uv run --with chromadb==1.5.9 …`), so the
project no longer depends on it. Before writing anything, it re-embeds a random sample with
the configured model and refuses to write unless cosine is ≥ 0.9999. Chroma never recorded
which model produced its vectors, so this is the check that they are what we think they are.

## Relevant Files

- `src/pythia/retrieval/embed.py`: loses all Chroma code (`_client`, `_collection`,
  `build_chroma_index`, `dense_search`). Keeps `load_model`, `embed_query`, `embed_passages`
  and `_signature`.
- `src/pythia/retrieval/search.py`: `find_dataset(..., chroma_path, collection)` becomes
  `find_dataset(..., dense: DenseIndex)`. Module docstring updated.
- `src/pythia/retrieval/index.py`: calls `build_dense_index` instead of `build_chroma_index`.
- `src/pythia/planning/planner.py`: `make_plan(..., chroma_path)` becomes `make_plan(..., dense)`.
- `src/pythia/api/service.py`: `Pipeline` owns a `DenseIndex` (lazy, loaded in `warm()`
  next to the model) and passes it to `make_plan`.
- `src/pythia/api/app.py`: `_dense_count` counts `dataset_embeddings` rows for the
  configured model, read-only, like `_count`. `health(settings, dense_loaded: int | None = None)`
  also publishes `dense_loaded`, the row count of the snapshot the running `Pipeline` actually
  searches (`None` before it loads). `/healthz` passes it from `app.state.pipeline`, so a
  re-index without a restart shows as `dense_index != dense_loaded` (grilling Q8).
- `src/pythia/api/dev.py`: preflight wording; still reads `dense_index` from `health()`.
- `src/pythia/eval/run_eval.py`: loads a `DenseIndex` and passes it on. Gains a `--planner`
  mode (grilling Q12): runs `make_plan` on each golden question through the configured LLM and
  prints the four buckets: MATCHED-correct, MATCHED-wrong, UNSUPPORTED, NO_MATCH. Phase 4's
  end-to-end run was ad hoc and is not reproducible from the repo; this makes it a command.
  It exits non-zero if MATCHED-wrong > 0.
- `src/pythia/ingest/schema.sql`: adds the `dataset_embeddings` table.
- `config.py`: removes `chroma_path`.
- `pyproject.toml` / `uv.lock`: remove `chromadb`; declare `numpy` directly.
- `tests/test_embed.py`, `tests/test_search.py`, `tests/test_planner.py`,
  `tests/test_api_*.py`: ported to the new signatures; the Chroma tombstone tests are removed.
- `.claude/skills/eval-gate/scripts/preflight.py`: imports `chromadb` today and would break.
  Rewrite `index_state` against `dataset_embeddings`: indexed = rows for the model, stale =
  `sig` ≠ `_signature(embed_text)`, orphaned = rows with no dataset. Keep its output shape.
- `docs/adr/0001-hybrid-retrieval.md`: status note that its dense-store choice is superseded
  by ADR-0010; the hybrid + RRF decision stands.
- `CLAUDE.md` §3, §4, §7, §8, §11; `README.md` (stack table, data-location table, retrieval
  numbers); `.claude/skills/eval-gate/SKILL.md` (the stale-vector and tombstone sections
  describe Chroma).

### New Files

- `src/pythia/retrieval/dense.py`: `DenseIndex`, `build_dense_index`,
  `load_dense_index`, `count_vectors`. Pure numpy + sqlite3; no new dependency.
- `scripts/migrate_chroma_vectors.py`: a thin, untested Chroma reader (about ten lines of
  `coll.get()`) that hands arrays to `dense.import_vectors`. It lives outside `src/` because
  it imports `chromadb`, which is no longer installed for mypy to see. Ruff still checks it.
  Delete it in the release after this one (grilling Q10).
- `docs/adr/0010-exact-dense-search.md`: the decision, with the probe table as evidence.
- `tests/test_dense.py`: unit tests for the new module.

## Implementation Phases

**Delivery (grilling Q11):** one PR against `develop`, closing #38, with one commit per
phase or step. ADR-0010, the ADR-0001 note and the doc updates land in the same PR, so the
reviewer reads the decision beside the code. `develop` never carries two dense stores.

### Phase 1: Foundation

Schema, `DenseIndex`, `build_dense_index`, and tests. Nothing calls them yet.

### Phase 2: Core Implementation

Swap the call chain (`find_dataset` → `make_plan` → `Pipeline` / `run_eval` / `index.py`),
health and preflight, and remove Chroma from code and dependencies. Then write the migration
script and run it on the dev box.

### Phase 3: Integration & Polish

Run the eval gate (golden set; reranker on and off), re-verify the three `EXAMPLES`, write
ADR-0010, update the docs, delete `data/chroma/` locally (closes #14 item 2), and run
`make check`.

## Step by Step Tasks

### 1. Schema

- Append to `ingest/schema.sql`:

  ```sql
  CREATE TABLE IF NOT EXISTS dataset_embeddings (
      dataset_id  TEXT PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
      model       TEXT NOT NULL,
      dim         INTEGER NOT NULL,
      sig         TEXT NOT NULL,          -- sha1(embed_text), as _signature() today
      vector      BLOB NOT NULL           -- little-endian float32, L2-normalised, len = 4*dim
  );
  ```

- `init_db` already applies the file idempotently; no migration runner is needed.

### 2. `retrieval/dense.py`: the index

- `DenseIndex` (frozen dataclass): `ids`, `matrix` (C-contiguous float32), `model`. `search(vec, k) -> list[tuple[str, float]]` returns ids best-first with their
  cosine. Behaviour: `k <= 0` or empty index → `[]`. An empty index is not an error: hybrid search
  degrades to lexical-only, as it does today with an empty Chroma collection. The loader logs
  one `dense.empty` warning, and `/healthz` already reports `degraded` (grilling Q7); `k >= n` → full sort; query dimension ≠
  `matrix.shape[1]` → `ValueError` (a model mismatch must fail loudly in dev, Principle 6).
- `load_dense_index(conn, model) -> DenseIndex`: loads only rows whose `model` matches. A
  row whose BLOB length ≠ `4*dim` raises, since a corrupt row is not silently skipped.
- `count_vectors(db_path, model) -> int`: read-only count for `/healthz`.

### 3. `retrieval/dense.py`: the build

- `build_dense_index(conn, model, *, model_name, batch_size=256) -> int`. It has the same
  contract as `build_chroma_index`: it returns the number of rows embedded this run.
  - A dataset is **changed** if it has no row, its `sig` differs, or its `model` differs from
    `model_name`. A model swap therefore re-embeds everything, with no special case.
  - It embeds changed rows in batches via `embed_passages` and upserts each batch in its own
    transaction (`INSERT … ON CONFLICT(dataset_id) DO UPDATE`).
  - Orphan rows are removed by `ON DELETE CASCADE` when the harvester deletes a dataset.
    Belt and braces: an explicit
    `DELETE FROM dataset_embeddings WHERE dataset_id NOT IN (SELECT id FROM datasets)`.
- No full-rebuild special case is needed: SQLite rows have no tombstones.

### 4. Swap the call chain

- `find_dataset(question, *, conn, model, dense: DenseIndex, top_k, pool, reranker,
  rerank_pool)`. The dense leg becomes `dense.search(embed_query(model, question),
  pool)`, mapped to ids. Lexical, RRF, rerank and hydrate are unchanged.
- `make_plan(..., dense)` and `Pipeline` (load the index next to the model in `warm()`,
  lazily on first use otherwise), plus `run_eval.main` and `index.py`.
- `api/app.py::_dense_count` → `count_vectors(settings.catalog_db_path, settings.embedding_model)`.
- Delete the Chroma code from `embed.py` and `chroma_path` from `config.py`. Remove the
  dependency with `uv remove chromadb`. Grep for `chroma` across `src/`, `tests/`, `config.py`
  and `Makefile`; it must return nothing outside the migration script, the docs and the ADRs.

### 5. Migration

- **`dense.import_vectors(conn, ids, vectors, sigs, *, model_name, embed_sample)`** in
  `src/`, fully tested in CI. It is the part that can corrupt data, so it is the part under
  test:
  1. Checks that every id exists in `datasets`, that there are no duplicate ids, and that
     `vectors.shape == (len(ids), model_dim)`. Rows must be finite and unit-norm (±1e-3).
  2. Calls `embed_sample(texts) -> ndarray` on a seeded random sample of 30 ids' `embed_text`.
     It requires min cosine ≥ 0.9999 against the supplied vectors, else raises with nothing
     written.
  3. Writes every row in one transaction with `model=model_name` and the supplied `sig`, so
     `make index` sees them as up to date. Returns the count.
- **`scripts/migrate_chroma_vectors.py --chroma data/chroma --collection datasets`**: reads
  `ids` + `embeddings` + `metadatas.sig`, loads the configured model for `embed_sample`,
  calls `import_vectors`, and prints counts. It does not delete `data/chroma/`; that is a
  manual step after the eval passes.
- Run as `uv run --with chromadb==1.5.9 python scripts/migrate_chroma_vectors.py`.
- After migration, `make index` must report `dense=0` (nothing to re-embed).

### 6. Docs and ADR

- ADR-0010 *Exact dense search over SQLite-stored vectors*: context = the probe table;
  decision; rationale (OOD queries vs HNSW, 1.9 ms exact at 21.8k, transactional storage,
  one fewer heavy dependency); consequences (linear scan cost, revisit threshold below,
  a restart picks up a re-index).
- ADR-0001: add a status line, "Dense store superseded by ADR-0010 (2026-10-30); hybrid +
  RRF unchanged". CLAUDE.md requires the old ADR's status to change in the same commit that
  lands the change.
- CLAUDE.md §3 (vector store line), §4 (layout: drop `data/chroma/`, add `dense.py`, `scripts/`),
  §7 (`make index` comment), §8 Phase 3 note (new numbers), §11 (the "tombstone-free" caveat
  disappears). README stack table, data table and retrieval numbers. eval-gate skill: replace
  the Chroma stale-vector and tombstone sections with the `dense=0` check.

### 7. Planner end-to-end eval

- Add `--planner` to `run_eval` (pure bucketing helper, unit-tested with a `FakeLLM`; a
  `MATCHED` plan whose `dataset.id` ≠ `expected_id` counts as MATCHED-wrong). Run it on
  `develop` **before** the swap to get a same-day baseline, so the comparison isn't against
  July's numbers on a different Ollama build, then again after.

### 8. Validate

- Run the Validation Commands below, and record the numbers in the PR.

## Testing Strategy

Test-first per the dispatch rule: each behavioural test below is committed and seen red
before its implementation exists.

- **`DenseIndex.search` is exact:** on a seeded random 500×64 unit matrix, the top-k equals
  brute-force `argsort` for k ∈ {1, 10, 50, 500, 600}. It is the guard that recall cannot
  silently regress again.
- **Deterministic ties:** duplicate vectors return in row order on every call.
- **Edge cases:** empty index; k = 0; a dimension-mismatched query raises `ValueError`; a
  corrupt BLOB length raises on load; rows from another model are excluded on load.
- **Build:** the first build embeds all rows; a second build embeds 0; changing one
  `embed_text` embeds 1; changing `model_name` re-embeds all; a deleted dataset loses its
  row. Uses a fake embedder for speed;
  one test keeps real e5-small.
- **Crash resume:** an embedder that raises on batch 2 leaves batch 1 committed; the rerun
  embeds only the rest.
- **`REPLACE` trap guard (grilling Q1):** re-upserting an existing dataset through
  `ingest.db.upsert_dataset` leaves its `dataset_embeddings` row intact. It goes red if the
  upsert is ever changed to `INSERT OR REPLACE`, which would cascade-delete the vector.
- **Integration:** the existing `test_search.py` cases, ported to `DenseIndex`, with real
  e5-small (offline).
- **Health:** `/healthz` `dense_index` equals the row count for the configured model, and is
  0 for a missing table.
- **Migration (`import_vectors`, no chromadb needed):** known arrays round-trip
  byte-identical through `load_dense_index`; a sample-cosine shortfall, an unknown id, a
  duplicate id, a wrong dimension, or a non-finite or non-unit row each raises with **zero**
  rows written; supplied `sig` values make a following `build_dense_index` embed 0.
- **Orphans (grilling Q9):** a dataset deleted with `PRAGMA foreign_keys=ON` loses its vector
  via the cascade, and the explicit `NOT IN` sweep in `build_dense_index` removes a row
  inserted with foreign keys off.
- **Health:** `dense_loaded` is `None` before the pipeline loads and equals the snapshot size
  after it; `dense_index` reads the table.

## Acceptance Criteria

1. `make eval` (n=26, normalization on, reranker off) reports **MRR ≥ 0.69 and R@1 ≥ 0.57**.
   The LEANN-run figures were MRR 0.700 / R@1 0.58, so the threshold leaves one question's
   worth of slack, not more.
2. The same eval with `RERANK_ENABLED=true` is run and reported beside ADR-0002's 0.652. It is
   informational only; the reranker stays default-off unless a separate decision changes it.
3. Dense search p50 ≤ 5 ms over the full 21,806-row catalogue, measured in-process.
4. After migration, `make index` reports `dense=0`, `/healthz` reports
   `dense_index=21806`, `dense_loaded=21806` and `status=ok`, and no e5-large re-embed was needed.
5. `run_eval --planner` reports **0 MATCHED-wrong** on all 26 questions, and its four buckets are
   reported beside the same-day pre-swap baseline. MATCHED-correct is expected to rise but is
   not gated.
6. All three `api/app.py::EXAMPLES` questions still return ANSWERED live (CLAUDE.md §10).
7. `grep -rni chroma src tests config.py Makefile pyproject.toml` matches nothing (the
   migration reader in `scripts/` is outside this grep). `chromadb` is absent from `uv.lock`.
8. `make check` is green locally and CI is green on py3.11 + py3.12.
9. ADR-0010 is added; ADR-0001's status is updated in the same commit; the CLAUDE.md, README
   and eval-gate skill updates are in the PR.

## Validation Commands

- `uv run --with chromadb==1.5.9 python scripts/migrate_chroma_vectors.py`: migration
  (dev box only).
- `uv run python -m pythia.retrieval.index`: must log `dense=0`.
- `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run python -m pythia.eval.run_eval`: criterion 1.
- `RERANK_ENABLED=true HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run python -m pythia.eval.run_eval`: criterion 2.
- `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run pytest tests/test_dense.py -q`.
- `uv run ruff check . && uv run mypy && HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run pytest -q`:
  the `make check` equivalent.
- `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run python -m pythia.eval.run_eval --planner`:
  criterion 5 (needs Ollama; run on `develop` first for the baseline).
- `uv run pythia-dev --no-browser`, then `/healthz` and the three EXAMPLES: criteria 4 and 6.
- `grep -rni chroma src tests config.py Makefile pyproject.toml uv.lock`: criterion 7.

## Notes

- **Scaling limit, stated rather than discovered later.** Exact search is linear in the
  corpus. At 1024-dim float32 it costs ~4 KB of RAM per dataset and ~1.4 ms per 21.8k rows on
  this CPU. At 200k datasets that is ~800 MB and ~15 ms, still acceptable. Revisit (IVF,
  float16, or an ANN index with a re-check) past ~500k rows, or if `embedding_model`
  grows past 1024 dims. data.gov.gr is at 21.9k and grows slowly.
- **Not in scope:** changing RRF `k`, the pool size (50), score floors, or the reranker
  default. RRF is rank-based, so `confidence()` keeps its distribution shape. The eval in
  criterion 2 is there to *inform* a later reranker decision, not to make it.
- **Memory:** +89 MB beside the 2.2 GB model; one snapshot per process.
- **`datasets_tombstoned` / `data/chroma/`:** deleted by hand after criteria 1–5 pass, which
  closes #14 item 2. Keep a copy until the PR merges, as the rollback path.
- **Dependencies:** `dense.py` is the first module in `src/` to import numpy directly, so add
  it as a declared dependency (`uv add "numpy>=2.0"`; 2.4.6 is already installed through
  sentence-transformers). Relying on a transitive import is how a dependency disappears
  unnoticed. Removing `chromadb` drops its transitive tree (`onnxruntime`, `grpcio`,
  `kubernetes`, …).

## Decisions (stage 2 grilling, 2026-10-30)

| # | Question | Decision | Why |
|---|---|---|---|
| Q1 | Vectors in `catalog.sqlite` or a separate file | **Same file**, with a real FK + `ON DELETE CASCADE`, guarded by the `REPLACE` trap test | Atomic with the catalogue; harvest upserts with `ON CONFLICT DO UPDATE` and never deletes, so the cascade only fires for real removals |
| Q2 | Live reload or restart-only | **Restart-only**: no holder, no generation counter | Re-indexing is rare and done by hand; per-call freshness was a Chroma side effect |
| Q3 | Migrate from Chroma or re-embed | **Migrate**, with the sample-cosine check | Minutes instead of 98 min, and it proves the stored vectors are e5-large |
| Q4 | ADR-0001 status | **Stays Accepted**, with a note that its dense-store clause is superseded by ADR-0010 | Hybrid + RRF is unchanged; a full `Superseded` would misstate that |
| Q5 | Acceptance bar | **MRR ≥ 0.69, R@1 ≥ 0.57** | Same vectors and fusion as the measured run, so a shortfall means a bug, not noise |
| Q6 | float32 or float16 | **float32** | 89 MB is trivial beside the model; float16 can reorder near-ties |
| Q7 | Empty vector table at query time | **Lexical-only degrade** + one `dense.empty` warning; `/healthz` degraded | Matches today; preflight names the fix |
| Q8 | Stale snapshot after a re-index | **Publish `dense_loaded`** beside `dense_index` in `/healthz` | Makes a missed restart visible |
| Q9 | Orphan cleanup that never fires today | **Keep cascade + explicit sweep, both tested** | Cheap; correct the day harvest prunes |
| Q10 | Migration untested in CI | **Split:** `import_vectors` in `src/` (tested), thin Chroma reader in `scripts/` | The part that can corrupt data is tested everywhere |
| Q11 | PR shape | **One PR, a commit per phase** | ADR beside the change; no window with two stores |
| Q12 | Planner end-to-end check | **Add `run_eval --planner`; gate on 0 MATCHED-wrong** against a same-day baseline | Principle 1 is the property at risk; retrieval metrics alone can't see a wrong match |

Facts found during grilling: `eval-gate/scripts/preflight.py` imports `chromadb` (added to
Relevant Files); `config.py` uses `extra="ignore"`, so a leftover `CHROMA_PATH` env var is
harmless once `chroma_path` is removed.
