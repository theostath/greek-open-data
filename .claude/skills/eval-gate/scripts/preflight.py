"""Preflight checks for the retrieval eval gate.

Answers one question before you spend time on `make eval`: are the indexes in a
state where the resulting numbers actually mean something? Reports catalog size,
lexical and dense index coverage, embedding drift, and the config that the
metrics must be recorded alongside.

Run from anywhere:  uv run python .claude/skills/eval-gate/scripts/preflight.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Running this by path puts the script's own directory on sys.path, not the repo
# root, so `config` and `pythia` would not import. Anchor to the repo root instead.
REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import get_settings  # noqa: E402

from pythia.retrieval.embed import _signature  # noqa: E402

# Imported rather than reimplemented: if the signature scheme ever changes, this
# check must change with it, otherwise it would report drift that does not exist.


def catalog_counts(db_path: str) -> tuple[int, int, dict[str, str]]:
    """Return (dataset count, FTS row count, {dataset_id: embed_text}) from the catalog."""
    conn = sqlite3.connect(db_path)
    try:
        datasets = {
            str(row[0]): str(row[1]) for row in conn.execute("SELECT id, embed_text FROM datasets")
        }
        try:
            fts_rows = int(conn.execute("SELECT count(*) FROM datasets_fts").fetchone()[0])
        except sqlite3.OperationalError:
            fts_rows = -1  # table absent: the lexical half was never built
        return len(datasets), fts_rows, datasets
    finally:
        conn.close()


def index_state(
    db_path: str, model_name: str, embed_texts: dict[str, str]
) -> tuple[int, int, int]:
    """Return (indexed vectors, datasets needing re-embed, vectors with no catalog row).

    Reads ``dataset_embeddings``. A dataset is stale when it has no row, its ``sig`` differs
    from the current ``embed_text``, or its row was written under another model. A catalogue
    without the table reads as nothing indexed and everything stale.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        try:
            rows = {
                str(ds_id): (str(model), str(sig))
                for ds_id, model, sig in conn.execute(
                    "SELECT dataset_id, model, sig FROM dataset_embeddings"
                )
            }
        except sqlite3.OperationalError:
            return 0, len(embed_texts), 0
    finally:
        conn.close()
    indexed = sum(1 for model, _ in rows.values() if model == model_name)
    stale = sum(
        1
        for ds_id, text in embed_texts.items()
        if rows.get(ds_id) != (model_name, _signature(text))
    )
    orphaned = sum(
        1 for ds_id, (model, _) in rows.items() if model == model_name and ds_id not in embed_texts
    )
    return indexed, stale, orphaned


def main() -> int:
    """Print index provenance and eval-relevant config; return 1 if the gate is unsafe."""
    settings = get_settings()
    problems: list[str] = []

    if not Path(settings.catalog_db_path).exists():
        print(f"FAIL  no catalog at {settings.catalog_db_path} - run `make harvest` first")
        return 1

    total, fts_rows, embed_texts = catalog_counts(settings.catalog_db_path)
    indexed, stale, orphaned = index_state(
        settings.catalog_db_path, settings.embedding_model, embed_texts
    )

    print(f"catalog        {total:>7,} datasets  ({settings.catalog_db_path})")
    print(f"lexical (FTS5) {fts_rows:>7,} rows")
    print(f"dense          {indexed:>7,} vectors  (dataset_embeddings, {settings.embedding_model})")
    print(f"  stale        {stale:>7,} datasets whose embed_text no longer matches the index")
    print(f"  orphaned     {orphaned:>7,} vectors with no catalog row")
    print()
    print("Config the metrics must be reported with:")
    print(f"  embedding_model   {settings.embedding_model}")
    print(f"  rerank_enabled    {settings.rerank_enabled}"
          + (f"  ({settings.rerank_model}, pool={settings.rerank_pool})"
             if settings.rerank_enabled else ""))
    print(f"  retrieval_top_k   {settings.retrieval_top_k}")
    print()

    if fts_rows <= 0:
        problems.append("lexical index missing or empty - half of the hybrid retriever is dead")
    elif fts_rows != total:
        problems.append(f"lexical index covers {fts_rows:,} of {total:,} datasets")
    if indexed == 0:
        problems.append("dense index empty - run `make index`")
    if orphaned:
        problems.append(f"{orphaned:,} orphaned vectors - the index has drifted from the catalog")

    if stale:
        problems.append(
            f"{stale:,} of {total:,} datasets are stale - the eval would score an index that "
            "does not match the catalog"
        )

    if problems:
        print("NOT SAFE TO GATE:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("OK - indexes are complete and current. Safe to run the eval.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
