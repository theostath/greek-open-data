"""SQLite FTS5 lexical search (BM25) and Reciprocal Rank Fusion (Phase 3 retrieval).

``build_fts_index`` mirrors ``datasets.embed_text`` into an FTS5 virtual table whose
unicode61 tokenizer folds Greek accents. ``lexical_search`` sanitizes free text into a
safe MATCH expression and ranks by BM25. ``rrf_fuse`` blends several rank lists (e.g.
lexical + dense) into one ordering without depending on raw scores.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _fold_diacritics(text: str) -> str:
    """Strip combining accents (incl. precomposed Greek tonos) via NFD decomposition.

    SQLite's ``remove_diacritics 2`` does not decompose precomposed Greek
    letters-with-tonos, so we fold them ourselves on both the indexed text and
    the query to make matching accent-insensitive.
    """
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")

_CREATE_FTS = (
    "CREATE VIRTUAL TABLE datasets_fts USING fts5("
    "dataset_id UNINDEXED, text, "
    "tokenize='unicode61 remove_diacritics 2')"
)


def build_fts_index(conn: sqlite3.Connection) -> int:
    """Rebuild the datasets_fts index from datasets.embed_text; return rows indexed."""
    conn.execute("DROP TABLE IF EXISTS datasets_fts")
    conn.execute(_CREATE_FTS)
    rows = conn.execute("SELECT id, embed_text FROM datasets").fetchall()
    conn.executemany(
        "INSERT INTO datasets_fts (dataset_id, text) VALUES (?, ?)",
        [(row[0], _fold_diacritics(row[1] or "")) for row in rows],
    )
    count: int = conn.execute("SELECT count(*) FROM datasets_fts").fetchone()[0]
    return count


def _build_match_expr(query: str) -> str | None:
    """Turn free text into a quoted OR MATCH expression, or None if no tokens."""
    tokens = _TOKEN_RE.findall(_fold_diacritics(query))
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens)


def lexical_search(conn: sqlite3.Connection, query: str, top_k: int) -> list[str]:
    """Return up to top_k dataset ids matching query, ranked best-first by BM25."""
    match_expr = _build_match_expr(query)
    if match_expr is None:
        return []
    rows = conn.execute(
        "SELECT dataset_id FROM datasets_fts WHERE datasets_fts MATCH ? "
        "ORDER BY bm25(datasets_fts) LIMIT ?",
        (match_expr, top_k),
    ).fetchall()
    return [row[0] for row in rows]


def rrf_fuse(
    rankings: list[list[str]], *, k: int = 60, top_k: int | None = None
) -> list[str]:
    """Fuse rank lists via Reciprocal Rank Fusion; stable tie-break by first seen."""
    scores: dict[str, float] = {}
    order: dict[str, int] = {}
    seen = 0
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
            if item not in order:
                order[item] = seen
                seen += 1
    fused = sorted(scores, key=lambda item: (-scores[item], order[item]))
    if top_k is not None:
        fused = fused[:top_k]
    return fused
