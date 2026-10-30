---
name: eval-gate
description: Run the golden-question retrieval eval gate that CLAUDE.md §9 makes mandatory before any retrieval or planning change can be called done, and report the metrics in the form a commit or PR needs. Use this skill whenever work touches src/pythia/retrieval/, src/pythia/planning/, the embed_text built in ingest/normalize.py, the golden question set, or any setting that changes ranking (embedding_model, rerank_enabled, rerank_pool, retrieval_top_k, planning_score_threshold) — and also whenever someone asks to "run the eval", "check retrieval quality", "did that improve MRR", "compare against the baseline", or is preparing a release branch or an ADR that needs eval numbers. Retrieval quality is the product here, so a change shipped without these numbers is untested work.
---

# The retrieval eval gate

`make eval` scores the hybrid retriever against 26 golden questions and prints MRR and
recall@k. Running it is the easy part. The reason this skill exists is that **the number is
only meaningful if you know what index produced it**, and there are three quiet ways to
produce a number that looks fine and means nothing.

Work through the steps in order. Don't skip step 1 — it is the one that silently invalidates
everything downstream.

## Step 1 — Establish that the index is worth measuring

```bash
uv run python .claude/skills/eval-gate/scripts/preflight.py
```

It exits non-zero and tells you what's wrong if the gate is unsafe. What it is protecting you
from:

**Stale vectors.** The eval scores the `dataset_embeddings` table, not the catalog. If the
catalog was re-harvested and `make index` hasn't run, you are measuring old vectors and
attributing the result to today's code. Preflight counts a vector as stale when its `sig` no
longer matches the dataset's `embed_text`, or it was made by another model.

**A missing or partial dense index.** On a checkout that predates ADR-0010, the vectors are
still in `data/chroma/`. Migrate them (minutes) rather than re-embedding (~98 CPU-minutes,
`docs/benchmarks/embedding-index-build.md`); `make index` refuses a full re-embed while that
directory exists unless given `--force`:

```bash
uv run --python <signed interpreter> --with chromadb==1.5.9 python scripts/migrate_chroma_vectors.py
uv run python -m pythia.retrieval.index    # must log dense=0
```

**A dead lexical half.** Retrieval is dense + BM25 fused with RRF (ADR-0001). If `datasets_fts`
is empty the eval still runs and still prints a number — a number for half a retriever.

## Step 2 — Run the eval

```bash
uv run python -m pythia.eval.run_eval          # make eval; normalization ON (default)
uv run python -m pythia.eval.run_eval --no-normalize   # raw Phase 3 baseline (ADR-0005 comparison)
```

Output is one `OVERALL` row plus one row per language. This is a real run of the shipped
configuration (2026-10-30, exact dense search, ADR-0010); the run takes about a minute:

```
OVERALL    n=26  MRR=0.700  R@1=0.58  R@3=0.81  R@5=0.88  R@10=0.92
el         n=12  MRR=0.709  R@1=0.58  R@3=0.83  R@5=0.92  R@10=1.00
en         n=7   MRR=0.786  R@1=0.71  R@3=0.86  R@5=0.86  R@10=0.86
greeklish  n=7   MRR=0.600  R@1=0.43  R@3=0.71  R@5=0.86  R@10=0.86
```

Dense search is exact and tie-safe, so an unchanged configuration reproduces bit-identically.
If the numbers move without a code change, stop: the vectors are stale, not improved.

**Planning changes also run the planner gate** (`make eval-planner`, needs Ollama, ~20 min).
It plans the 26 golden and 8 negative questions and buckets every outcome. It exits 1 on a
match to a wrong dataset, 3 if any plan degraded (the run is invalid, not passed), 2 if the
LLM is unreachable. Compare its JSON report question by question against a baseline run of
the parent commit: a question newly wrong in either shape (MATCHED or UNSUPPORTED on the
wrong dataset), or a negative question no longer refused, fails the change.

To measure the reranker, set `RERANK_ENABLED=true` for the run. Budget for it: ~28 s/query on
CPU, so **~12 minutes** for the set (ADR-0002). It ships default-off for exactly that reason,
so a rerank-on number is an experiment, never the shipped configuration — label it as such.

## Step 3 — Interpret against the noise floor, not against zero

n=26. One question moving is worth **~0.04 MRR**, and ADR-0002 treats that as the noise floor.
A delta below it is not an improvement, however much you want it to be — say "no measurable
change" and mean it.

The per-language slices are far smaller and correspondingly noisier:

| Slice | n | One question is worth |
|---|---|---|
| `el` | 12 | ~0.08 MRR |
| `en` | 7 | ~0.14 MRR |
| `greeklish` | 7 | ~0.14 MRR |

So a greeklish slice that jumps 0.10 is one question changing its mind. Report slice movement
as a direction to investigate, never as a result on its own. A change is convincing when
OVERALL moves beyond the noise floor **and** no language slice regresses — that combination is
what got ADR-0005 accepted and what exposed the reranker's `el` risk as unfounded.

## Step 4 — Record the numbers where the decision lives

§9 makes the metrics part of the change, not a side note. Put this in the commit body or PR
description — and in the ADR too if the change is one:

```markdown
### Eval gate (make eval)

| Config | n | MRR | R@1 | R@5 | R@10 | el | en | greeklish |
|---|---|---|---|---|---|---|---|---|
| baseline (<what it was>) | 26 | | | | | | | |
| **this change** | 26 | | | | | | | |

- Index: preflight 0 stale / 0 orphaned on YYYY-MM-DD, 21,806 datasets
- Model: intfloat/multilingual-e5-large · rerank: off · normalize: on
- Verdict: <beats the ~0.04 noise floor / within noise / regression in <slice>>
```

An eval result without its index provenance and config is not reproducible, which makes it
not evidence. That is also why a `release/*` branch re-runs this and records the numbers in
the tag message (§11).

## Recorded baselines

n=26, e5-large. Compare against the row that matches your configuration — comparing a
rerank-on run to the hybrid-only baseline is the third quiet way to produce a meaningless
number. Rows marked *Chroma* are history: the dense leg was approximate until ADR-0010.

| Config | MRR | R@1 | R@10 | el | en | greeklish |
|---|---|---|---|---|---|---|
| **exact dense + normalization** (shipped, 2026-10-30) | **0.700** | **0.58** | **0.92** | 0.709 | 0.786 | 0.600 |
| exact dense, no normalization | 0.625 | 0.54 | 0.85 | 0.709 | 0.786 | 0.319 |
| *Chroma*, no normalization | 0.515 | 0.42 | 0.69 | 0.595 | 0.571 | 0.319 |
| *Chroma* + normalization (ADR-0005) | 0.544 | 0.46 | 0.69 | 0.595 | 0.571 | 0.429 |
| *Chroma* + reranker, normalization off (ADR-0002) | 0.652 | 0.62 | | 0.729 | 0.714 | 0.457 |
| *Chroma* + reranker **and** normalization | 0.644 | 0.62 | | 0.729 | 0.714 | 0.429 |

Note the last two rows: normalization and the reranker **do not stack** — they fix the same
weakness, and combining them is slightly worse than the reranker alone. Assuming two wins add
up is an easy way to report a gain that isn't there.

Greeklish is the weakest slice and the largest remaining product lever. The known open
experiment is the reranker's quality/latency knee across `rerank_pool` sizes — never measured,
and it may recover most of the gain far more cheaply than pool=20's 28 s/query.

## When the gate does not apply

Synthesis, access and ingestion changes don't move retrieval, so they don't need this — they
need `make check` and their own tests. Running the eval anyway costs ~2 minutes and proves
nothing, and quoting an unchanged MRR on a synthesis PR implies a link that isn't there.

If you changed retrieval and cannot run the gate (no catalog, no vectors, no Ollama for
the planner gate), say so explicitly in the PR rather than omitting the section. An
acknowledged gap is workable; a silent one reads as a passed gate.
