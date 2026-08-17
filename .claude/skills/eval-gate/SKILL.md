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

**Stale vectors.** The eval scores whatever is in Chroma, not what is in the catalog. If the
catalog was re-harvested and `make index` hasn't run, you are measuring an old index and
attributing the result to today's code.

**HNSW tombstones — the subtle one.** `build_chroma_index` re-embeds only datasets whose
`embed_text` changed. When *every* dataset changed it drops and recreates the collection; when
only *some* did, it upserts. HNSW has no true update, so each upserted vector leaves a
tombstone in the graph. Tombstones degrade recall *and* make ANN results vary between
processes — so the run is not reproducible, and a later comparison against it is measuring
graph damage as if it were a code change. CLAUDE.md §11 requires eval numbers to come from a
tombstone-free collection. If preflight reports a partial re-embed, delete the Chroma
directory and rebuild:

```bash
rm -rf data/chroma && uv run python -m pythia.retrieval.index
```

That is a **~98 minute CPU rebuild** for all 21,806 datasets (`docs/benchmarks/embedding-index-build.md`).
Plan around it; don't discover it at the end.

**A dead lexical half.** Retrieval is dense + BM25 fused with RRF (ADR-0001). If `datasets_fts`
is empty the eval still runs and still prints a number — a number for half a retriever.

## Step 2 — Run the eval

```bash
uv run python -m pythia.eval.run_eval          # make eval; normalization ON (default)
uv run python -m pythia.eval.run_eval --no-normalize   # raw Phase 3 baseline (ADR-0005 comparison)
```

Output is one `OVERALL` row plus one row per language. This is a real run of the shipped
configuration (2026-08-06), which reproduced the 2026-07-28 numbers exactly — the run takes
about a minute:

```
OVERALL    n=26  MRR=0.544  R@1=0.46  R@3=0.62  R@5=0.62  R@10=0.69
el         n=12  MRR=0.595  R@1=0.50  R@3=0.67  R@5=0.67  R@10=0.83
en         n=7   MRR=0.571  R@1=0.43  R@3=0.71  R@5=0.71  R@10=0.71
greeklish  n=7   MRR=0.429  R@1=0.43  R@3=0.43  R@5=0.43  R@10=0.43
```

Bit-identical reproduction is the signal that the index is sound. If you re-run an unchanged
configuration and the numbers move, stop — you have a tombstoned or stale index, not a
discovery.

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

- Index: <tombstone-free rebuild YYYY-MM-DD | unchanged since YYYY-MM-DD>, 21,806 datasets
- Model: intfloat/multilingual-e5-large · rerank: off · normalize: on
- Verdict: <beats the ~0.04 noise floor / within noise / regression in <slice>>
```

An eval result without its index provenance and config is not reproducible, which makes it
not evidence. That is also why a `release/*` branch re-runs this and records the numbers in
the tag message (§11).

## Recorded baselines

Measured 2026-07-28, n=26, e5-large, tombstone-free index. Compare against the row that
matches your configuration — comparing a rerank-on run to the hybrid-only baseline is the
third quiet way to produce a meaningless number.

| Config | MRR | R@1 | R@10 | el | en | greeklish |
|---|---|---|---|---|---|---|
| hybrid only, no normalization | 0.515 | 0.42 | 0.69 | 0.595 | 0.571 | 0.319 |
| **+ Greeklish normalization** (shipped, ADR-0005) | **0.544** | **0.46** | **0.69** | 0.595 | 0.571 | 0.429 |
| + reranker, normalization off (ADR-0002, default-off) | 0.652 | 0.62 | | 0.729 | 0.714 | 0.457 |
| + reranker **and** normalization | 0.644 | 0.62 | | 0.729 | 0.714 | 0.429 |

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

If you changed retrieval and cannot run the gate (no catalog, no index, no time for a
98-minute rebuild), say so explicitly in the PR rather than omitting the section. An
acknowledged gap is workable; a silent one reads as a passed gate.
