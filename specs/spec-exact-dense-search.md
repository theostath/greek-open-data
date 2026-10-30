# Plan: exact dense search over vectors stored in the catalogue

- **Issue:** #38 · **Branch:** `feat/exact-dense-search` · **Intent:** `specs/intent-exact-dense-search.md`
- **Type:** enhancement (retrieval quality) + refactor (storage) · **Complexity:** medium
- **Status:** implemented on `feat/exact-dense-search` (2026-10-30). Outcome per criterion at the end.
- **Prerequisite:** PR #39 (`docs/leann-evaluation`) is merged into `develop` and this branch
  is rebased onto it, so the evidence ADR-0010 cites is in the repository (P8).

## Task Description

Replace the Chroma HNSW index behind the dense leg of `find_dataset` with **exact cosine
search** over a float32 matrix held in memory. Persist the vectors in a new table in
`data/catalog.sqlite` instead of in `data/chroma/`, and remove `chromadb` from the project.
Keep `make index` incremental. Migrate the 21,806 existing e5-large vectors without
re-embedding them, since a full re-embed costs ~98 CPU-minutes. Add a reproducible planner
end-to-end eval, so the change is gated on grounded-or-silent rather than on retrieval
metrics alone.

## Objective

When this is done:

- the dense candidates for every question are its **true** top-k nearest stored vectors,
  with deterministic tie order;
- golden-set retrieval reproduces the measured exact-search run: MRR 0.700, R@1 15/26,
  R@10 24/26, against today's 0.544 / 12/26 / 18/26;
- the planner gives no answer on a wrong dataset that the same-day baseline did not already
  give;
- dense search stays in single-digit milliseconds, and the snapshot loads in seconds;
- `data/chroma/` and `chromadb` are gone, along with the tombstone failure mode and the
  204 MB store (#14 item 2).

## Problem Statement

On the 26-question golden set, Chroma returns 71% of the exact top-10. A probe on
2026-10-30, run on a copy of the live store and on fresh in-memory rebuilds from the same
stored vectors, shows this is **structural, not index state**. Method, seed and raw numbers
are committed in `docs/benchmarks/dense-recall-probe.md`.

| Index | Golden-query recall@10 (n=26) | Perturbed-passage-query recall@10 (n=300) |
|---|---|---|
| live store (copy), `ef_search=100`, M=16 | 0.712 | 0.963 |
| same copy after `modify(ef_search=400)` | 0.712 (setting had **no effect**) | 0.963 |
| fresh rebuild, defaults | 0.631 | 0.956 |
| fresh, `ef_search=400` | 0.865 | 0.967 |
| fresh, M=32 / efc=200 / efs=200 | 0.823 | 0.967 |
| exact (numpy) | **1.000 · 1.9 ms/q** | **1.000 · 1.4 ms/q** |

"Perturbed-passage" queries are 300 stored passage vectors drawn with seed 0, plus Gaussian
noise (σ=0.02) and re-normalised. They are queries that look like passages. HNSW handles
those well and real E5 `query:` vectors badly, because the query vectors sit away from the
passages they should match: an out-of-distribution workload. No tested configuration reaches
0.9 on real queries. The golden-query column rests on only 26 queries; the gap to exact is
far outside that noise (one question ≈ 0.04).

## Solution Approach

1. **Storage: a `dataset_embeddings` table in `catalog.sqlite`** (grilling Q1). One row per
   dataset: a little-endian float32 BLOB, the model name, the dimension, the content `sig`
   (sha1 of `embed_text`), and `embedded_at` (UTC ISO-8601 with microseconds). The DDL is
   owned by `retrieval/dense.py` (`ensure_dense_schema`, `CREATE … IF NOT EXISTS`), the same
   convention `lexical.py` uses for `datasets_fts`: a retrieval table owned by its builder,
   not the ingest schema (P15).
   - A BLOB of 4,096 bytes overflows SQLite's 4,061-byte local payload, so each row takes its
     own overflow page. The catalogue grows from ~134 MB to **~235 MB** (P20).
   - Deleting `catalog.sqlite` to recover from a bad harvest now also deletes ~98 CPU-minutes
     of vectors. This is documented in the README data table and ADR-0010 (P21).

2. **Search: `DenseIndex`**, an immutable in-memory snapshot (`ids`, `matrix` float32 [n, d]
   with `writeable=False`, `model`, `version` = max `embedded_at`). It is loaded **ordered by
   `dataset_id`**, so row index equals id order on any database, migrated or rebuilt (P2).
   `search(q, k)` is exact and **tie-safe**: compute `s = matrix @ q`; if `k < n`, find the
   k-th largest score `t` with `np.partition`, keep **every** row with `s >= t`, then order
   those by `(-score, row_index)` with `np.lexsort` and take k. Rows tied across the k
   boundary are therefore resolved by `dataset_id`, never arbitrarily. This matters: 1,380
   datasets (6.3%) share their `embed_text` with another, in 282 groups of up to 118. No
   golden expected id is in one, so metrics are unaffected; citations otherwise would be.

3. **Staleness: restart-only** (grilling Q2), but **made visible** (P4). `/healthz` compares
   the table's `(count, version)` for the configured model with the running snapshot's.
   A re-index that rewrites rows in place keeps the count and changes the version.

4. **Planner end-to-end eval: `run_eval --planner`** (grilling Q12, P1). It lands *first*,
   on the Chroma path, so a same-day baseline exists before the swap. It gates on wrong
   answers in both shapes (MATCHED and UNSUPPORTED on the wrong dataset) and adds a small
   negative set, because all 26 golden questions are answerable and a planner that never
   refuses would pass a positive-only gate.

## Relevant Files

- `src/pythia/retrieval/embed.py`: loses all Chroma code (`_client`, `_collection`,
  `build_chroma_index`, `dense_search`). Keeps `load_model`, `embed_query`, `embed_passages`,
  `_signature`.
- `src/pythia/retrieval/search.py`: `find_dataset(..., chroma_path, collection)` becomes
  `find_dataset(..., dense: DenseIndex)`. The dense leg is
  `dense.search(embed_query(model, question), pool)`; lexical, RRF, rerank and hydrate are
  unchanged.
- `src/pythia/retrieval/index.py`: calls `build_dense_index`. It refuses a full build (zero
  rows for the model) while `data/chroma/` still exists unless `--force` is given, so nobody
  starts a 98-minute re-embed instead of a migration (P19). The guard is deleted with the
  migration script.
- `src/pythia/planning/planner.py`: `make_plan(..., chroma_path)` becomes `make_plan(..., dense)`.
- `src/pythia/api/service.py`: `Pipeline(..., dense=None, dense_loader=None)`, mirroring
  `model`/`model_loader`. A double-checked `_dense_lock` loads it lazily, and `warm()` loads
  it next to the model. `dense_info() -> tuple[int, str | None] | None` reports the snapshot
  `(count, version)` **without triggering a load** (P13). Its "MRR is 0.544" comment at
  :383 is updated.
- `src/pythia/api/app.py`: `health(settings, dense_info=None)` publishes `dense_index`
  (table count), `dense_version`, `dense_loaded` and `dense_loaded_version`. The `/healthz`
  route reads `getattr(request.app.state, "pipeline", None)`; route tests build a
  `TestClient` without running the lifespan, so the attribute may be absent (P13).
  `status` is `degraded` when any of these holds (P4):
  `datasets == 0`, `lexical == 0`, `dense_index == 0`, `dense_index < datasets`,
  `dense_loaded == 0`, or `dense_info` is not `None` and differs from the table's
  `(count, version)`. The `EXAMPLES` comment ("12/26 … first") is updated.
- `src/pythia/api/dev.py`: preflight prints `partial (N of M)` when `dense_index < datasets`,
  and keeps its one-argument `health(settings)` call.
- `src/pythia/eval/run_eval.py`: loads a `DenseIndex`. It gains `--planner` (P1, P5) and
  returns non-zero when a gate fails.
- `src/pythia/eval/golden_negative.yaml` (new): the negative set, kept separate so
  `make eval` metrics do not move.
- `config.py`: removes `chroma_path`. `extra="ignore"` keeps a leftover `CHROMA_PATH` env
  var harmless.
- `pyproject.toml` / `uv.lock`: `uv remove chromadb`; delete the `chromadb.*` entry in
  `[[tool.mypy.overrides]]` (`pyproject.toml:81`); `uv add "numpy>=2.0"`, since `dense.py`
  is the first direct numpy import in `src/`.
- `Makefile`: the `index` help text drops "(Chroma)"; add an `eval-planner` target.
- `.claude/skills/eval-gate/scripts/preflight.py`: imports `chromadb` today. Rewrite
  `index_state` against `dataset_embeddings`: indexed = rows for the model; stale = `sig` ≠
  `_signature(embed_text)` **or** a model mismatch (P24); orphaned = rows with no dataset.
  Keep its output shape and its root-anchoring `sys.path` stanza.
- `.claude/skills/eval-gate/SKILL.md`: replace the stale-vector and tombstone sections with
  the preflight/`dense=0` check. Update "Recorded baselines" and the sample output, and add
  `--planner` and `--no-normalize` to the ritual.
- `tests/test_embed.py`, `tests/test_search.py`, `tests/test_planner.py`,
  `tests/test_api_*.py`, `tests/api_harness.py`: ported. The Chroma tombstone tests go.
- Docs: `docs/adr/0001-hybrid-retrieval.md` (status line), `docs/adr/0005-greeklish-transliteration.md`
  (eval table re-measured on exact search; its "Chroma index" wording), `CLAUDE.md` §2
  Principle 3, §3, §4, §7, §8, §10, §11; `README.md` (stack table, data table incl. file
  growth and the deletion warning, retrieval numbers, restart-after-index note);
  `CHANGELOG.md` `[Unreleased]` (chromadb removed, the one-time migration step, restart after
  `make index`).

### New Files

- `src/pythia/retrieval/dense.py`: `ensure_dense_schema`, `DenseIndex`, `load_dense_index`,
  `build_dense_index`, `import_vectors`, `count_vectors`, and a `--bench` entry point
  (`python -m pythia.retrieval.dense --bench`). Pure numpy + sqlite3.
- `scripts/migrate_chroma_vectors.py`: a thin Chroma reader. It is untested and stays outside
  `src/`, because mypy cannot see `chromadb` any more. Ruff still checks it. Deleted in the
  next release.
- `scripts/probe_dense_recall.py`: the probe behind the Problem Statement, committed so the
  table is reproducible. It needs `--with chromadb` and is deleted alongside the migration
  script.
- `docs/benchmarks/dense-recall-probe.md`: probe method, seed, versions, M/efc/efs, the
  copied-store procedure, per-query recall, and the n=26 caveat.
- `docs/adr/0010-exact-dense-search.md`.
- `tests/test_dense.py`, `tests/test_run_eval_planner.py`.

## Implementation Phases

**Delivery:** one PR against `develop` closing #38 (grilling Q11), one commit per step below.
ADR-0010 and the ADR-0001 status line land **in the same commit that removes Chroma** (P27).

### Phase 0: Evidence and baseline (no behaviour change)

PR #39 is merged and the branch is rebased onto it. Commit the probe script and doc (Step 1).
Add `--planner` and the negative set on the Chroma code path (Step 2), then run the baseline
at that SHA.

### Phase 1: Foundation

`dense.py` with its tests (Steps 3–5). Nothing calls it yet.

### Phase 2: Core Implementation

Swap the call chain, health and preflight; remove Chroma; add the migration script, ADR-0010
and the ADR-0001 line (Step 6). Run the migration (Step 7).

### Phase 3: Integration & Polish

Run the eval gate and the planner gate, the benchmark and the EXAMPLES (Step 9). Update the
docs, CHANGELOG and ADR-0005 (Step 8). Delete `data/chroma/` locally only after criteria 1–7
pass, which closes #14 item 2.

## Step by Step Tasks

### 1. Commit the evidence

- Copy the scratch probe into `scripts/probe_dense_recall.py`. Arguments: `--chroma-copy`,
  `--catalog`, seed 0. It needs a *copy* of `data/chroma`, never the live store, because it
  calls `modify()`.
- Write `docs/benchmarks/dense-recall-probe.md` with the table, the method above,
  `chromadb==1.5.9`, numpy 2.4.6, the machine (as `embedding-index-build.md`), and the
  per-question golden recall.

### 2. `run_eval --planner` and the negative set (on Chroma, before the swap)

- `golden_negative.yaml`: 8 questions in el / en / greeklish about subjects data.gov.gr does
  not publish (e.g. other countries' statistics, private-company data, future events). Each
  entry records `why_uncovered`, checked by a lexical + dense search of the catalogue whose
  top-10 is noted in the PR. Expected outcome: `no_match`.
- A pure helper `bucket(plan, expected_id | None) -> Bucket`:
  - for a golden question: `MATCHED_CORRECT`, `MATCHED_WRONG`, `UNSUPPORTED_CORRECT`,
    `UNSUPPORTED_WRONG` or `NO_MATCH`;
  - for a negative question: `REFUSED` (`no_match`) or `ANSWERED_WRONGLY` (`matched` or
    `unsupported`);
  - any plan with `degraded=True` becomes `DEGRADED`, whatever its status.
- `main(["--planner"])`:
  - aborts with exit 2 if the LLM is unreachable (the same probe as `_llm_reachable`);
  - runs `make_plan` on all 26 + 8 questions and prints the buckets plus a per-question table
    (`id`, bucket, chosen dataset);
  - writes a JSON report with the git SHA, `settings.llm_model` and the Ollama model digest
    (`/api/show`);
  - exits 3 (INVALID) if any plan is `DEGRADED`, and 1 if any golden plan is
    `MATCHED_WRONG`; otherwise 0.
  - It always normalises, as `make_plan` does, so `--no-normalize` is rejected alongside
    `--planner`.
  - Expected run time: about 34 × 10–40 s, on the dev box only, never in CI.
- Run it at this commit and save the report as the baseline (path in the PR, not committed).
  The 2026-07-29 log already shows 2 `UNSUPPORTED_WRONG` (q01, q02) that CLAUDE.md §8's
  "0 wrong" summary does not count. Correct §8 in Step 8.

### 3. `dense.py`: schema, `DenseIndex`, load

- `ensure_dense_schema(conn)`:

  ```sql
  CREATE TABLE IF NOT EXISTS dataset_embeddings (
      dataset_id  TEXT PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
      model       TEXT NOT NULL,
      dim         INTEGER NOT NULL,
      sig         TEXT NOT NULL,
      embedded_at TEXT NOT NULL,           -- UTC ISO-8601, microseconds
      vector      BLOB NOT NULL            -- '<f4', L2-normalised, len = 4*dim
  );
  CREATE INDEX IF NOT EXISTS idx_dense_model ON dataset_embeddings(model);
  ```

- `load_dense_index(db_path, model_name) -> DenseIndex` opens the file **read-only**
  (`mode=ro` URI, like `api/app.py::_count`), so a missing file is never created.
  - A missing file or missing table loads as an **empty** index, logging one `dense.empty`
    warning; it never raises (P3). App startup must not depend on the table existing.
  - It reads `WHERE model = ? ORDER BY dataset_id` into a **preallocated** `np.empty((n, d),
    '<f4')`, decoding with `np.frombuffer(blob, '<f4')` row by row, so it never holds every
    `bytes` object at once.
  - It raises `ValueError` on a row whose `len(blob) != 4*dim`, a `dim` that differs from the
    first row's, or a non-finite value.
  - It logs `dense.partial` with both counts when there are fewer rows than `datasets`.
- `DenseIndex.search(q, k) -> list[tuple[str, float]]` returns cosine similarity (higher is
  better), best-first, using the tie-safe selection above.
  - `k <= 0` or an empty index returns `[]`.
  - A query dimension different from `matrix.shape[1]` raises `ValueError`.
- `count_vectors(db_path, model_name) -> tuple[int, str | None]` returns `(count, version)`,
  read-only, and `(0, None)` when the table is missing.

### 4. `dense.py`: build

- `build_dense_index(conn, model, *, model_name, batch_size=256) -> int` returns the number
  of rows embedded this run.
  - A dataset needs embedding when it has no row, its `sig` differs, or its `model` differs.
  - Each batch is **embedded first, then written** in its own short transaction
    (`INSERT … ON CONFLICT(dataset_id) DO UPDATE`, with a fresh `embedded_at`) and committed.
    No write lock is ever held while the model runs (P20).
  - The orphan sweep (`DELETE … WHERE dataset_id NOT IN (SELECT id FROM datasets)`) runs in
    its own short committed transaction.
  - **It owns its commits.** This is an explicit, docstring-documented exception to
    `ingest/db.py`'s "callers control transactions": crash-resume needs per-batch commits
    (P12). Rows embedded before a crash survive, and the rerun embeds only the rest.

### 5. `dense.py`: `import_vectors` and `--bench`

- `import_vectors(conn, ids, vectors, sigs, *, model_name, embed_sample) -> int` owns its
  single commit (P12). It writes nothing unless every check passes:
  1. The table holds **zero** rows for `model_name`; otherwise it refuses (P10). A re-run
     never overwrites newer embeddings with older ones.
  2. Ids are unique and every id exists in `datasets`. An unknown id aborts the import and
     names the id; the import is all or nothing.
  3. `vectors` is finite, unit-norm (±1e-3), and shaped `(len(ids), d)`. `d` is taken from
     `embed_sample`'s output shape.
  4. Sample check (P11): draw a seeded sample of `min(30, m)` ids from the `m` ids whose
     supplied `sig == _signature(current embed_text)`; stale rows are excluded. If `m == 0`,
     refuse. `embed_sample` is `embed_passages`, with the `passage:` prefix. Require min
     cosine ≥ 0.9999.
  5. Write every row, with the supplied `sig` and one `embedded_at`, then commit.
- `python -m pythia.retrieval.dense --bench`:
  - loads the full snapshot and prints the load time;
  - embeds the 26 golden queries **once** (excluded from timing), warms up, then times
    `DenseIndex.search(q, 50)` alone over 26 × 20 calls;
  - prints p50 / p95 (P16).

### 6. Swap, remove Chroma, record the decision (one commit)

- Port `find_dataset`, `make_plan`, `Pipeline`, `run_eval`, `index.py`, health, dev preflight
  and the eval-gate preflight, as listed in Relevant Files.
- `scripts/migrate_chroma_vectors.py` (P14):
  - puts the repo root on `sys.path` before `import config`, as `preflight.py:17-21` does;
  - reads `coll.get(include=["embeddings", "metadatas"])`;
  - calls `init_db` and `ensure_dense_schema`, then `import_vectors` with `embed_passages`
    as `embed_sample`;
  - prints counts and never touches `data/chroma/`;
  - its docstring says: stop `pythia-dev` first (P20).
- `uv remove chromadb`; delete the mypy override; edit the Makefile; `uv add "numpy>=2.0"`.
- Add ADR-0010 *Exact dense search over SQLite-stored vectors*:
  - context: the probe table;
  - decision;
  - rationale: out-of-distribution queries vs HNSW, 1.9 ms exact search at 21.8k rows,
    transactional storage, one heavy dependency fewer;
  - consequences: linear scan cost and the revisit threshold, restart after `make index`
    (visible in `/healthz`), catalogue growth to ~235 MB, deleting the catalogue now deletes
    the vectors.
- ADR-0001's status becomes "Accepted (dense store superseded by ADR-0010, 2026-10-30)"
  (grilling Q4). Hybrid search with RRF stands.

### 7. Migrate (dev box)

- Run the eval-gate preflight first; record stale and orphaned counts (0 / 0 expected).
- Run: `uv run --python "<signed 3.12>" --with chromadb==1.5.9 python
  scripts/migrate_chroma_vectors.py`. The WDAC policy blocks uv-managed interpreters, so the
  overlay must use the signed interpreter (P14).
- Then `make index` must log `dense=0`.

### 8. Docs

- Update CLAUDE.md (§2 Principle 3, §3, §4, §7 incl. `eval-planner` and restart-after-index,
  §8 Phase 3 numbers and the corrected Phase 4 buckets, §10, §11), README, CHANGELOG,
  eval-gate SKILL.md, and ADR-0005's eval table.
- Close the intent's "eval reports dense recall vs exact" item explicitly. Exactness is
  enforced by `test_dense.py` (recall 1.0 by construction) and wiring by the `find_dataset`
  spy test, so no per-run recall line is needed (P26, override).

### 9. Validate

Run the Validation Commands and record every number in the PR, including the per-question
reciprocal-rank table.

## Testing Strategy

Each behavioural test is committed and seen red before its implementation (test-first
dispatch). Every test that asserts persistence uses a **file** database and checks through a
**fresh connection**, because a connection always sees its own uncommitted writes (P12).

- **Exactness, scores included:** on a seeded random 500×64 unit matrix, `search` equals
  brute force for k ∈ {1, 10, 50, 500, 600}, comparing ids **and** scores (`allclose`), with
  the scores as similarities.
- **Tie safety (P2):** 120 identical rows plus distinct ones, with the tie straddling k=10.
  Returns the 10 lowest `dataset_id`s, the same on every call, and the same after the rows
  are inserted in two different orders.
- **Load alignment:** after a load from the table, searching each stored vector returns its
  own id first. The round trip compares a per-id map, not positions.
- **Edge cases:**
  - missing file, missing table and empty table all return an empty index with no raise;
  - `k = 0` returns `[]`;
  - a dimension-mismatched query raises;
  - a corrupt BLOB length, mixed dims or a NaN raises on load;
  - other-model rows are excluded;
  - `matrix` is not writeable.
- **Build:**
  - the first build embeds all;
  - a second build embeds 0;
  - one edited `embed_text` embeds 1 and changes the version;
  - a different `model_name` re-embeds all;
  - a fake embedder records every call, and one test uses real e5-small.
- **Crash resume:** an embedder raising on batch 2 leaves batch 1 visible through a new
  connection; the rerun embeds only the rest.
- **Orphans (grilling Q9):** a dataset deleted through `ingest.db.connect()` (foreign keys
  on) loses its vector via the cascade. A row inserted with foreign keys off is removed by the
  sweep.
- **`REPLACE` trap (grilling Q1):** through `ingest.db.connect()`, re-upserting an existing
  dataset with `upsert_dataset` keeps its vector. A **control** case runs `INSERT OR REPLACE`
  on the same fixture and asserts the vector *is* deleted, which proves the test can detect
  the trap (P25).
- **`import_vectors`:** byte-identical round trip; a following build embeds 0. Each of these
  raises with **zero** rows visible from a new connection: a sample shortfall, an unknown id,
  a duplicate id, a wrong dimension, a non-unit or non-finite row, a non-empty table, and no
  current-sig rows. Stale rows are excluded from the sample.
- **Wiring (P25):**
  - a spy `DenseIndex` asserts `find_dataset` passes `k == pool` (50), not `top_k`;
  - a fixture where only the dense leg can find the expected dataset (no lexical overlap)
    proves the dense leg is used;
  - the existing `test_search.py` cases, ported, use real e5-small.
- **Pipeline:**
  - `dense=` injection works;
  - two threads racing the first use load once (the loader counts calls);
  - `dense_info()` returns `None` before a load and never triggers one.
- **Health:**
  - `dense_loaded` is `None` when the pipeline is absent or not loaded;
  - status is `degraded` for each condition listed under `app.py`, including the same count
    with a different version;
  - the route test still passes with the lifespan not run;
  - an app on a catalogue without the table starts and serves `/healthz` (P3).
- **`--planner` (P5):**
  - `bucket()` covers every bucket, including both wrong shapes and `DEGRADED`;
  - `main` exit codes are 0, 1, 2 and 3, with `make_plan` and the LLM probe monkeypatched;
  - `--planner --no-normalize` is rejected.
- **`index.py` guard:** a full build with `data/chroma/` present refuses without `--force`.

## Acceptance Criteria

1. **Retrieval reproduces the reference exact run** (P6, revises grilling Q5). `make eval`
   (normalisation on, reranker off) reports MRR ≥ 0.700, R@1 ≥ 15/26, R@3 ≥ 21/26 and
   R@10 ≥ 24/26. No language slice falls below today's Chroma baseline: el MRR ≥ 0.595,
   en ≥ 0.571, greeklish ≥ 0.429. Any per-question rank that differs from the reference run
   is explained in the PR. Fusion and vectors are unchanged, so a shortfall is a bug.
2. The reranker comparison is run with normalisation on and reranker on, and compared with
   **0.644**, the matching row in the eval-gate baselines, not ADR-0002's normalisation-off
   0.652 (P7). It is informational; the reranker stays default-off.
3. `run_eval --no-normalize` is run and reported, and ADR-0005's table is updated (P17).
   Informational; if normalisation now lowers MRR, the PR says so and an issue is opened.
4. `--bench`: search p50 ≤ 5 ms and p95 ≤ 10 ms; snapshot load ≤ 3 s (P16).
5. After migration: the eval-gate preflight reports 0 stale and 0 orphaned; `make index`
   logs `dense=0`; `/healthz` shows `dense_index = dense_loaded = 21806`, matching versions,
   and `status=ok`. No re-embed happened.
6. **Planner gate** (P1, P5):
   - the after-run is valid: exit code not 2 or 3;
   - golden `MATCHED_WRONG` = 0;
   - no golden question is wrong (`MATCHED_WRONG` or `UNSUPPORTED_WRONG`) after the swap
     unless it was wrong in the same-day baseline, and likewise no negative question becomes
     `ANSWERED_WRONGLY`. A newly wrong question is re-run twice and counts only if it is
     wrong in 2 of 3 runs (LLM non-determinism);
   - both reports, with SHA and model digest, are summarised in the PR.
7. All three `api/app.py::EXAMPLES` return ANSWERED, checked via
   `make answer QUESTION="…"` for each. One that refuses blocks the PR until it is
   replaced per CLAUDE.md §10.
8. `git grep -niw -e chromadb -e chroma_path -e build_chroma_index -- src config.py
   Makefile pyproject.toml` matches nothing, and `chromadb` is absent from `uv.lock`. A
   word-boundary search on the library's names only: `tests/test_api_contrast.py` uses
   "chroma" as a colour-science term (P9). `tests/` is excluded because tests there assert
   the removal by name, e.g. `test_embed.py::test_chroma_helpers_are_removed` (amended in
   stage 4: the earlier pathspec could never pass).
9. `make check` is green locally, and CI is green on py3.11 and py3.12.
10. ADR-0010 and the ADR-0001 status line are in the Chroma-removal commit; the docs listed
    in Step 8 are in the PR.

## Validation Commands

The dev box runs offline. Prefix each with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` where
a model loads.

- `uv run python -m pythia.eval.run_eval --planner`: at the Step 2 commit (baseline), and
  after Step 7 (criterion 6).
- `uv run python .claude/skills/eval-gate/scripts/preflight.py`: before migration and before
  criterion 1.
- `uv run --python "<signed 3.12>" --with chromadb==1.5.9 python scripts/migrate_chroma_vectors.py`.
- `uv run python -m pythia.retrieval.index`: must log `dense=0`.
- `uv run python -m pythia.eval.run_eval`: criterion 1.
- `RERANK_ENABLED=true uv run python -m pythia.eval.run_eval`: criterion 2.
- `uv run python -m pythia.eval.run_eval --no-normalize`: criterion 3.
- `uv run python -m pythia.retrieval.dense --bench`: criterion 4.
- `uv run pythia-dev --no-browser`, then `curl -s 127.0.0.1:8000/healthz`: criterion 5.
- `uv run python -m pythia.synthesis.answer --question "…"` (== `make answer`) for each EXAMPLE: criterion 7.
- `git grep -niw -e chromadb -e chroma_path -e build_chroma_index -- src config.py Makefile pyproject.toml; grep -c chromadb uv.lock`: criterion 8.
- `uv run ruff check . && uv run mypy && uv run pytest -q`: criterion 9.

## Notes

- **Scaling limit.** Exact search is linear: ~4 KB of RAM per dataset at 1024 dims, and
  ~1.4 ms per 21.8k rows on this CPU. At 200k rows that is ~800 MB and ~15 ms. Revisit (IVF,
  float16, or an ANN index with an exact re-check) past ~500k rows, or past 1024 dims.
- **Score floor (P18).** "RRF is rank-based, so `confidence()` keeps its shape" was wrong. The
  real position: whenever the dense leg returns anything, the top fused candidate scores at
  least ≈ 1/61 against a maximum of 2/60, so `confidence ≥ ~0.49`. The 0.15 floor
  (`config.py:51`) therefore never binds, before or after this change, and the degraded-mode
  fallback always matches the top candidate. That is a pre-existing Principle-1 gap, out of
  scope here and filed as its own issue.
- **Memory.** +89 MB per process beside the 2.2 GB model; one snapshot.
- **Rollback.** Keep `data/chroma/` until the PR merges; reverting the PR plus that directory
  restores the old path.
- **`data/chroma/` deletion** closes #14 item 2, and is done by hand after criteria 1–7.

## Decisions

### Stage 2 grilling (2026-10-30)

| # | Question | Decision | Why |
|---|---|---|---|
| Q1 | Vectors in `catalog.sqlite` or a separate file | **Same file**, FK + cascade, `REPLACE` trap tested | Atomic with the catalogue; harvest only upserts |
| Q2 | Live reload or restart-only | **Restart-only**, made visible by `/healthz` version (P4) | Re-indexing is rare and manual |
| Q3 | Migrate or re-embed | **Migrate**, with the sample-cosine check | Minutes, not 98; proves the vectors are e5-large |
| Q4 | ADR-0001 status | **"Accepted (dense store superseded by ADR-0010)"** | Hybrid + RRF unchanged |
| Q5 | Acceptance bar | **Superseded by P6:** reproduce the reference run exactly, plus per-slice no-regression | The original 0.69/0.57 claimed slack it did not have |
| Q6 | float32 or float16 | **float32** | 89 MB is trivial; float16 reorders near-ties |
| Q7 | Empty table at query time | **Lexical-only degrade**, extended to a missing table/file (P3) | Matches today; startup never blocks |
| Q8 | Stale snapshot after a re-index | **Publish loaded `(count, version)`** beside the table's | Count alone misses in-place rewrites (P4) |
| Q9 | Orphan cleanup | **Cascade + explicit sweep, both tested** | Cheap; correct the day harvest prunes |
| Q10 | Migration testability | **`import_vectors` in `src/`**, thin reader in `scripts/` | The data-writing part is tested in CI |
| Q11 | PR shape | **One PR, a commit per step** | ADR beside the change |
| Q12 | Planner end-to-end check | **`--planner` first, both wrong shapes, negative set** (P1) | Principle 1 is the property at risk |

### Stage 3 judge panel (three independent judges, 2026-10-30)

Consolidated findings. "×n" is how many judges raised it independently.

| P | Finding | Sev. | Resolution |
|---|---|---|---|
| P1 | Planner gate blind to `UNSUPPORTED` on a wrong dataset (q01/q02 already were, in the 2026-07-29 log), and no refusal test | BLOCKER ×1 | **Fixed:** six buckets, both wrong shapes gated, 8-question negative set (Step 2, AC6) |
| P2 | Ties non-deterministic: 1,380 duplicate-text datasets; `argpartition` arbitrary at the boundary; no load order | MAJOR ×3 | **Fixed:** `ORDER BY dataset_id`, tie-safe selection, 120-row straddle test |
| P3 | Missing table crashes `warm()`, so the app never boots | MAJOR ×3 | **Fixed:** read-only load; missing file/table → empty; startup test |
| P4 | `/healthz` says `ok` while partial, empty-loaded, or stale; counts miss in-place rewrites | MAJOR ×3 | **Fixed:** `(count, version)` comparison and six degraded conditions |
| P5 | `--planner` baseline cannot run on `develop`; degraded plans scored; exit code untested | MAJOR ×3 | **Fixed:** lands first on Chroma; `DEGRADED` → exit 3; LLM probe → exit 2; exit-code tests |
| P6 | AC1 slack claim false (R@1 had none; MRR 0.01 < one swap) | MAJOR ×2 | **Fixed:** exact reproduction + per-slice no-regression (revises Q5) |
| P7 | Reranker compared with the normalisation-off 0.652 | MAJOR ×3 | **Fixed:** compare with 0.644 |
| P8 | ADR evidence not in the repo; probe uncommitted, "perturbed" undefined | MAJOR ×3 | **Fixed:** PR #39 prerequisite; probe script + doc committed |
| P9 | Chroma grep matches OKLCH `chroma` and `.pyc` files, so it can never pass | MAJOR ×2 | **Fixed:** `git grep -w` on library names |
| P10 | `import_vectors` on a non-empty table undefined | MAJOR ×2 | **Fixed:** refuses unless empty for the model |
| P11 | Sample check fails on stale rows; `model_dim` source; n < 30 | MINOR ×3 | **Fixed:** current-sig sample, `min(30, m)`, dim from `embed_sample` |
| P12 | Commit ownership unstated; tests pass with no commit | MAJOR ×2 | **Fixed:** documented exception; file DB + fresh-connection asserts |
| P13 | `/healthz` wiring breaks the lifespan-less route test; a health probe could trigger the 89 MB load | MAJOR ×1 | **Fixed:** `getattr(…, None)`, non-loading `dense_info()` |
| P14 | Migration: `import config` fails from `scripts/`; `get()` omits embeddings; WDAC | MAJOR ×3 | **Fixed:** root stanza, explicit `include`, `--python` signed interpreter |
| P15 | DDL in the ingest schema vs the builder-owned FTS convention | MINOR ×1 | **Fixed:** `ensure_dense_schema` in `dense.py` |
| P16 | Latency criterion has no command, ambiguous scope, load time unmeasured | MAJOR ×2 | **Fixed:** `--bench`, search-only timing, load ≤ 3 s |
| P17 | ADR-0005 rested on the lossy index and is not re-measured | MAJOR ×1 | **Fixed:** `--no-normalize` run; ADR-0005 table updated |
| P18 | Score-floor reasoning wrong; the floor never binds | MINOR ×2 | **Fixed:** corrected note; filed as a separate issue (out of scope) |
| P19 | `make index` before migration starts a 98-minute re-embed | MINOR ×1 | **Fixed:** guard with `--force` |
| P20 | Write locking during the ~100 MB import; file growth; load peak | MINOR ×2 | **Fixed:** embed-then-write short transactions; stop the server for migration; preallocated load; growth documented. **WAL not enabled:** a global journal-mode change is outside this spec's scope, and the only long write is the one-off migration, run with the server stopped |
| P21 | Deleting the catalogue now deletes the vectors | MINOR ×1 | **Fixed:** README + ADR-0010 consequences |
| P22 | Lazy-load race; no injection point | MINOR ×3 | **Fixed:** `_dense_lock`, `dense=` / `dense_loader=`, race test |
| P23 | Same-dimension model mismatch is undetectable | MINOR ×2 | **Overridden:** query model and index rows are both selected by `settings.embedding_model`, so they agree by construction; the one place a mislabel can enter, the migration, is guarded by the sample-cosine check |
| P24 | Preflight's new stale rule ignores the model | MINOR ×1 | **Fixed:** a model mismatch counts as stale |
| P25 | Test gaps: crash-resume, `REPLACE` trap with FKs off, exactness ids-only, alignment, wiring (`k=pool`, dense-only find) | MAJOR ×2 | **Fixed:** each test strengthened as listed |
| P26 | Intent's "eval reports dense recall" dropped silently | MINOR ×2 | **Overridden:** exactness is enforced by unit tests and wiring by the spy test; a per-run line would recompute the same brute force. Closed explicitly in Step 8 |
| P27 | ADR "same commit" rule vs phase order | MINOR ×1 | **Fixed:** ADRs in the Chroma-removal commit |
| P28 | Doc and config surfaces missing (mypy override, Makefile, SKILL baselines, CLAUDE §2/§10, CHANGELOG, comments) | MINOR ×2 | **Fixed:** all listed in Relevant Files |
| P29 | EXAMPLES check has no command or failure rule | MINOR ×1 | **Fixed:** `make answer` per example; a refusal blocks |
| P30 | Phase 4 "0 wrong" summary undercounted (6 vs 8 UNSUPPORTED) | MINOR ×1 | **Fixed:** CLAUDE.md §8 corrected in Step 8 |

## Outcome (2026-10-30)

| AC | Result |
|---|---|
| 1 | **Met.** MRR 0.700, R@1 15/26, R@3 21/26, R@10 24/26; el 0.709, en 0.786, greeklish 0.600. Identical to the reference run, per question |
| 2 | Ran: reranker + normalisation 0.817 MRR, R@1 20/26 (0.644 on Chroma). Informational; reranker stays off |
| 3 | Ran: normalisation off 0.625 MRR; ADR-0005 updated, decision stands |
| 4 | **Met.** Load 0.60 s; search p50 1.51 ms, p95 2.29 ms |
| 5 | **Met.** Migration 38 s; preflight 0 stale / 0 orphaned; `make index` `dense=0`; live `/healthz` ok, 21,806 stored = loaded, same version |
| 6 | **Met.** Planner gate exit 0; 0 MATCHED_WRONG; no question newly wrong; 8/8 negatives refused; MATCHED_CORRECT 6 → 8 |
| 7 | **Not met as written, unchanged by this work.** Two examples return ANSWERED; the asylum example returns PARTIAL, identically on the pre-swap code. Not a refusal, so it does not block; recorded in CLAUDE.md §10 |
| 8 | **Met** with the amended pathspec (6d678d8) |
| 9 | Local: 732 passed, ruff and mypy clean. CI: see the PR |
| 10 | **Met.** ADR-0010 and the ADR-0001 line are in ae05c06 |

Re-entries during stage 4, classified: the `/api/show` digest bug and the SHA-capture timing
were code wrong, spec right (fixed in place); the criterion 8 pathspec was spec wrong
(amended first, 6d678d8).
