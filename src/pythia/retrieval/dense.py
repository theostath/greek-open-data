"""Exact dense search over vectors stored in the catalogue (ADR-0010).

Passage vectors live in ``dataset_embeddings`` inside ``catalog.sqlite``, one little-endian
float32 BLOB per dataset. ``load_dense_index`` reads them into an immutable in-memory
``DenseIndex`` whose ``search`` is exact cosine similarity (vectors are L2-normalised, so it
is a dot product) with deterministic tie order. ``build_dense_index`` keeps the table
incremental; ``import_vectors`` loads vectors computed elsewhere (the one-off Chroma
migration) behind a re-embedding sample check.
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from pythia.logging_setup import get_logger, log_event
from pythia.retrieval.embed import _signature, embed_passages

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

LOGGER_NAME = "pythia.retrieval.dense"
_SAMPLE_SIZE = 30
_MIN_SAMPLE_COSINE = 0.9999
_UNIT_NORM_TOL = 1e-3

FloatArray = NDArray[np.float32]

_DDL = """
CREATE TABLE IF NOT EXISTS dataset_embeddings (
    dataset_id  TEXT PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    sig         TEXT NOT NULL,
    embedded_at TEXT NOT NULL,           -- UTC ISO-8601, microseconds
    vector      BLOB NOT NULL            -- '<f4', L2-normalised, len = 4*dim
);
CREATE INDEX IF NOT EXISTS idx_dense_model ON dataset_embeddings(model);
"""

_UPSERT = (
    "INSERT INTO dataset_embeddings (dataset_id, model, dim, sig, embedded_at, vector) "
    "VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(dataset_id) DO UPDATE SET model = excluded.model, dim = excluded.dim, "
    "sig = excluded.sig, embedded_at = excluded.embedded_at, vector = excluded.vector"
)


def ensure_dense_schema(conn: sqlite3.Connection) -> None:
    """Create ``dataset_embeddings`` if absent; owned here, like ``datasets_fts`` in lexical."""
    conn.executescript(_DDL)


def _now() -> str:
    """UTC ISO-8601 timestamp with microseconds, the format of ``embedded_at``."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class DenseIndex:
    """Immutable snapshot of a model's vectors; row ``n`` of ``matrix`` belongs to ``ids[n]``."""

    ids: tuple[str, ...]
    matrix: FloatArray
    model: str
    version: str | None

    def search(
        self, query: Sequence[float] | NDArray[np.floating], k: int
    ) -> list[tuple[str, float]]:
        """Return the exact top-k ``(dataset_id, cosine)`` pairs, best first.

        Rows tied across the k boundary are resolved by row index (== ``dataset_id`` order),
        never arbitrarily.
        """
        n = len(self.ids)
        if k <= 0 or n == 0:
            return []
        q = np.asarray(query, dtype=np.float32)
        if q.ndim != 1 or q.shape[0] != self.matrix.shape[1]:
            raise ValueError(
                f"query shape {q.shape} does not match index dimension {self.matrix.shape[1]}"
            )
        scores = self.matrix @ q
        take = min(k, n)
        if take < n:
            threshold = np.partition(scores, n - take)[n - take]
            candidates = np.flatnonzero(scores >= threshold)
        else:
            candidates = np.arange(n)
        order = np.lexsort((candidates, -scores[candidates]))[:take]
        return [(self.ids[int(candidates[j])], float(scores[candidates[j]])) for j in order]


def _empty_index(model_name: str) -> DenseIndex:
    """An index with no rows; searching it returns ``[]``."""
    matrix: FloatArray = np.empty((0, 0), dtype="<f4")
    matrix.flags.writeable = False
    return DenseIndex(ids=(), matrix=matrix, model=model_name, version=None)


def _open_readonly(db_path: str | Path) -> sqlite3.Connection | None:
    """Open the catalogue read-only, or return None when the file does not exist."""
    path = Path(db_path)
    if not path.is_file():
        return None
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def load_dense_index(db_path: str | Path, model_name: str) -> DenseIndex:
    """Load the model's vectors, ordered by ``dataset_id``; a missing file or table is empty.

    Raises ``ValueError`` on a BLOB of the wrong length, a dimension that differs from the
    first row's, or a non-finite value.
    """
    logger = get_logger(LOGGER_NAME)
    conn = _open_readonly(db_path)
    if conn is None:
        log_event(logger, logging.WARNING, "dense.empty", reason="no_catalog", model=model_name)
        return _empty_index(model_name)
    try:
        try:
            n, version = conn.execute(
                "SELECT count(*), max(embedded_at) FROM dataset_embeddings WHERE model = ?",
                (model_name,),
            ).fetchone()
        except sqlite3.OperationalError:
            log_event(logger, logging.WARNING, "dense.empty", reason="no_table", model=model_name)
            return _empty_index(model_name)
        if n == 0:
            log_event(logger, logging.WARNING, "dense.empty", reason="no_rows", model=model_name)
            return _empty_index(model_name)

        rows = conn.execute(
            "SELECT dataset_id, dim, vector FROM dataset_embeddings "
            "WHERE model = ? ORDER BY dataset_id",
            (model_name,),
        )
        ids: list[str] = []
        matrix: FloatArray | None = None
        for dataset_id, dim, blob in rows:
            if matrix is None:
                matrix = np.empty((n, int(dim)), dtype="<f4")
            if int(dim) != matrix.shape[1]:
                raise ValueError(
                    f"{dataset_id}: dim {dim} differs from the first row's {matrix.shape[1]}"
                )
            if len(blob) != 4 * matrix.shape[1]:
                raise ValueError(
                    f"{dataset_id}: vector is {len(blob)} bytes, expected {4 * matrix.shape[1]}"
                )
            if len(ids) >= n:
                raise ValueError("rows changed while loading the dense index")
            row = np.frombuffer(blob, dtype="<f4")
            if not np.isfinite(row).all():
                raise ValueError(f"{dataset_id}: vector holds a non-finite value")
            matrix[len(ids)] = row
            ids.append(str(dataset_id))
        if matrix is None or len(ids) != n:
            raise ValueError("rows changed while loading the dense index")

        datasets = conn.execute("SELECT count(*) FROM datasets").fetchone()[0]
    finally:
        conn.close()

    if n < datasets:
        log_event(logger, logging.WARNING, "dense.partial", vectors=n, datasets=datasets)
    matrix.flags.writeable = False
    return DenseIndex(ids=tuple(ids), matrix=matrix, model=model_name, version=str(version))


def count_vectors(db_path: str | Path, model_name: str) -> tuple[int, str | None]:
    """Return ``(row count, max embedded_at)`` for the model; ``(0, None)`` if unavailable."""
    conn = _open_readonly(db_path)
    if conn is None:
        return 0, None
    try:
        count, version = conn.execute(
            "SELECT count(*), max(embedded_at) FROM dataset_embeddings WHERE model = ?",
            (model_name,),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0, None
    finally:
        conn.close()
    return int(count), (str(version) if version is not None else None)


def build_dense_index(
    conn: sqlite3.Connection,
    model: SentenceTransformer,
    *,
    model_name: str,
    batch_size: int = 256,
) -> int:
    """Incrementally embed the catalogue; return the number of datasets embedded this run.

    A dataset is embedded when it has no row, its ``sig`` differs, or its ``model`` differs.
    Orphaned rows (no dataset) are swept first.

    This function owns its commits, a deliberate exception to ``ingest/db.py``'s "callers
    control transactions": each batch is embedded *before* its write transaction opens and is
    committed on its own, so no write lock is held while the model runs and rows embedded
    before a crash survive for the rerun.
    """
    ensure_dense_schema(conn)
    conn.execute("DELETE FROM dataset_embeddings WHERE dataset_id NOT IN (SELECT id FROM datasets)")
    conn.commit()

    existing = {
        str(row[0]): (str(row[1]), str(row[2]))
        for row in conn.execute("SELECT dataset_id, model, sig FROM dataset_embeddings")
    }
    changed = [
        (str(row[0]), str(row[1]))
        for row in conn.execute("SELECT id, embed_text FROM datasets ORDER BY id")
        if existing.get(str(row[0])) != (model_name, _signature(str(row[1])))
    ]
    conn.commit()  # end the read snapshot: nothing is held open while the model runs

    for start in range(0, len(changed), batch_size):
        chunk = changed[start : start + batch_size]
        vectors = embed_passages(model, [text for _, text in chunk])
        embedded_at = _now()
        conn.executemany(
            _UPSERT,
            [
                (
                    dataset_id,
                    model_name,
                    len(vector),
                    _signature(text),
                    embedded_at,
                    np.asarray(vector, dtype="<f4").tobytes(),
                )
                for (dataset_id, text), vector in zip(chunk, vectors, strict=True)
            ],
        )
        conn.commit()
    return len(changed)


def import_vectors(
    conn: sqlite3.Connection,
    ids: Sequence[str],
    vectors: NDArray[np.floating],
    sigs: Sequence[str],
    *,
    model_name: str,
    embed_sample: Callable[[list[str]], list[list[float]]],
) -> int:
    """Load externally computed vectors; return the number written. All or nothing.

    Raises ``ValueError``, writing nothing, unless: the model has no rows yet; ids are unique
    and all exist in ``datasets``; ``vectors`` is finite, unit-norm and shaped
    ``(len(ids), d)`` with ``d`` matching ``embed_sample``; and a seeded sample of rows whose
    ``sig`` is current re-embeds to cosine >= 0.9999. ``embed_sample`` receives raw
    ``embed_text`` values.

    This function owns its single commit, a deliberate exception to ``ingest/db.py``'s
    "callers control transactions": the import is one atomic write.
    """
    ensure_dense_schema(conn)
    conn.commit()
    present = conn.execute(
        "SELECT count(*) FROM dataset_embeddings WHERE model = ?", (model_name,)
    ).fetchone()[0]
    if present:
        raise ValueError(f"{present} vectors already stored for {model_name}; refusing to import")

    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or not (len(ids) == len(sigs) == matrix.shape[0]):
        raise ValueError(
            f"shape mismatch: {len(ids)} ids, {len(sigs)} sigs, vectors {matrix.shape}"
        )
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate ids in import")
    texts = {str(r[0]): str(r[1]) for r in conn.execute("SELECT id, embed_text FROM datasets")}
    unknown = [i for i in ids if i not in texts]
    if unknown:
        raise ValueError(f"{len(unknown)} ids not in datasets, first: {unknown[0]}")
    if not np.isfinite(matrix).all():
        raise ValueError("vectors contain non-finite values")
    norms = np.linalg.norm(matrix.astype(np.float64), axis=1)
    if np.abs(norms - 1.0).max(initial=0.0) > _UNIT_NORM_TOL:
        raise ValueError("vectors are not unit-norm within 1e-3")

    current = [
        n for n, (i, s) in enumerate(zip(ids, sigs, strict=True)) if s == _signature(texts[i])
    ]
    if not current:
        raise ValueError("no row carries a current sig; nothing to verify the vectors against")
    picked = sorted(random.Random(0).sample(current, min(_SAMPLE_SIZE, len(current))))
    fresh = np.asarray(embed_sample([texts[ids[n]] for n in picked]), dtype=np.float64)
    if fresh.ndim != 2 or fresh.shape[1] != matrix.shape[1] or fresh.shape[0] != len(picked):
        raise ValueError(f"sample embeddings {fresh.shape} do not match vectors {matrix.shape}")
    fresh /= np.linalg.norm(fresh, axis=1, keepdims=True)
    cosines = np.einsum("ij,ij->i", fresh, matrix[picked].astype(np.float64) / norms[picked, None])
    if cosines.min() < _MIN_SAMPLE_COSINE:
        raise ValueError(
            f"sample check failed: min cosine {cosines.min():.6f} < {_MIN_SAMPLE_COSINE}"
        )

    embedded_at = _now()
    dim = int(matrix.shape[1])
    try:
        conn.executemany(
            _UPSERT,
            [
                (i, model_name, dim, s, embedded_at, matrix[n].astype("<f4").tobytes())
                for n, (i, s) in enumerate(zip(ids, sigs, strict=True))
            ],
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return len(ids)


def _percentile(sorted_ms: list[float], pct: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    return sorted_ms[min(len(sorted_ms) - 1, int(pct / 100 * len(sorted_ms)))]


def _bench() -> int:
    """Time snapshot load and ``search(q, 50)`` over the golden questions; print the result."""
    from config import get_settings

    settings = get_settings()
    start = time.perf_counter()
    index = load_dense_index(settings.catalog_db_path, settings.embedding_model)
    print(f"load: {time.perf_counter() - start:.2f} s, {len(index.ids)} vectors")
    if not index.ids:
        print("index is empty; run the migration or `make index` first")
        return 0

    from pythia.eval.run_eval import load_golden
    from pythia.planning.normalize import normalize_question
    from pythia.retrieval.embed import embed_query, load_model

    model = load_model(settings.embedding_model)
    queries = [
        embed_query(model, normalize_question(g.question)[0]) for g in load_golden()
    ]
    for q in queries:  # warm-up
        index.search(q, 50)
    timings: list[float] = []
    for _ in range(20):
        for q in queries:
            t0 = time.perf_counter()
            index.search(q, 50)
            timings.append((time.perf_counter() - t0) * 1000)
    timings.sort()
    print(
        f"search(q, 50): p50 {_percentile(timings, 50):.2f} ms, "
        f"p95 {_percentile(timings, 95):.2f} ms over {len(timings)} calls"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: ``python -m pythia.retrieval.dense --bench``."""
    parser = argparse.ArgumentParser(description="Exact dense index tools.")
    parser.add_argument("--bench", action="store_true", help="time load and search")
    args = parser.parse_args(argv)
    if args.bench:
        return _bench()
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
