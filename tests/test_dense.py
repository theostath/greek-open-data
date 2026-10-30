"""Tests for exact dense search over vectors stored in the catalogue (``retrieval/dense.py``).

Contract source: ``specs/spec-exact-dense-search.md`` (Solution Approach 1-3, Steps 3-5,
Testing Strategy). Seams pinned here:

- ``build_dense_index`` embeds through the module-level name
  ``pythia.retrieval.dense.embed_passages(model, texts)``; tests monkeypatch it with a fake.
- ``import_vectors(..., embed_sample=...)`` takes a ``Callable[[list[str]], list[list[float]]]``
  receiving raw ``embed_text`` values (the caller binds the model, e.g.
  ``lambda texts: embed_passages(model, texts)``).
- Every refusal (load corruption, query dimension, ``import_vectors`` checks) is a
  ``ValueError``.

Persistence is always asserted on a FILE database through a FRESH connection, because a
connection sees its own uncommitted writes.
"""

from __future__ import annotations

import hashlib
import random
import re
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from pythia.ingest import db
from pythia.ingest.models import DatasetRow
from pythia.retrieval import dense

FAKE_MODEL = "fake/e5-test"
OTHER_MODEL = "fake/other-model"
FAKE_DIM = 8
TEST_MODEL = "intfloat/multilingual-e5-small"  # real model for the one end-to-end build test

FloatArray = NDArray[np.float32]


# --- fixtures and helpers ---------------------------------------------------------------


def _sig(text: str) -> str:
    """Content signature as the spec defines it: sha1 of the UTF-8 embed_text."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _uid(rng: random.Random, *, hex32: bool = False) -> str:
    """A dataset id shaped like the real catalogue: 36-char UUID or 32-hex."""
    value = uuid.UUID(int=rng.getrandbits(128), version=4)
    return value.hex if hex32 else str(value)


def _ids(n: int, seed: int) -> list[str]:
    """``n`` distinct dataset ids, mixing both real id shapes (every 7th is 32-hex)."""
    rng = random.Random(seed)
    out: list[str] = []
    while len(out) < n:
        candidate = _uid(rng, hex32=len(out) % 7 == 0)
        if candidate not in out:
            out.append(candidate)
    return out


def _row(dataset_id: str, embed_text: str, *, title: str | None = None) -> DatasetRow:
    """Build a minimal DatasetRow carrying an id and embed_text."""
    return DatasetRow(
        id=dataset_id, name=f"name-{dataset_id[:8]}", title=title or embed_text,
        title_en=None, notes=None, notes_en=None, org_name=None, org_title=None,
        license_id=None, license_title=None, frequency=None, language_options=[], theme=[],
        num_resources=0, tags=[], temporal_start=None, temporal_end=None, spatial_text=None,
        metadata_created=None, last_updated="2026-01-01T00:00:00", state="active",
        harvested_at="2026-06-01T00:00:00", embed_text=embed_text,
    )


def _catalog(path: Path, texts: dict[str, str], *, schema: bool = True) -> None:
    """Create a file catalogue through the real ingest layer, optionally with the dense table."""
    conn = db.connect(path)
    db.init_db(conn)
    for dataset_id, text in texts.items():
        db.upsert_dataset(conn, _row(dataset_id, text))
    conn.commit()
    if schema:
        dense.ensure_dense_schema(conn)
        conn.commit()
    conn.close()


def _unit_rows(matrix: NDArray[Any]) -> FloatArray:
    """L2-normalise each row and return little-endian float32."""
    m = np.asarray(matrix, dtype=np.float64)
    m = m / np.linalg.norm(m, axis=1, keepdims=True)
    return m.astype("<f4")


def _insert_vectors(
    path: Path,
    ids: Sequence[str],
    matrix: NDArray[Any],
    *,
    model: str = FAKE_MODEL,
    embedded_at: str = "2026-10-30T12:00:00.000001+00:00",
    create_datasets: bool = True,
) -> None:
    """Write rows straight into ``dataset_embeddings`` (the spec-pinned DDL), in given order."""
    if create_datasets:
        _catalog(path, {i: f"κείμενο {i}" for i in ids})
    conn = db.connect(path)
    dense.ensure_dense_schema(conn)
    m = np.asarray(matrix, dtype="<f4")
    for dataset_id, vec in zip(ids, m, strict=True):
        conn.execute(
            "INSERT INTO dataset_embeddings (dataset_id, model, dim, sig, embedded_at, vector) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (dataset_id, model, int(vec.shape[0]), _sig(dataset_id), embedded_at, vec.tobytes()),
        )
    conn.commit()
    conn.close()


def _raw_insert(path: Path, dataset_id: str, model: str, dim: int, blob: bytes) -> None:
    """Insert one arbitrary (possibly corrupt) row through a plain connection."""
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO dataset_embeddings (dataset_id, model, dim, sig, embedded_at, vector) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (dataset_id, model, dim, _sig(dataset_id), "2026-10-30T12:00:00.000001+00:00", blob),
    )
    conn.commit()
    conn.close()


def _stored(path: Path) -> dict[str, tuple[str, int, str, str, bytes]]:
    """Read every dense row through a FRESH connection: id -> (model, dim, sig, at, blob)."""
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT dataset_id, model, dim, sig, embedded_at, vector FROM dataset_embeddings"
        ).fetchall()
    finally:
        conn.close()
    return {str(r[0]): (str(r[1]), int(r[2]), str(r[3]), str(r[4]), bytes(r[5])) for r in rows}


def _fake_vector(text: str, dim: int = FAKE_DIM) -> list[float]:
    """Deterministic unit vector derived from the text (stands in for an E5 passage vector)."""
    seed = int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:8], 16)
    v = np.random.default_rng(seed).standard_normal(dim)
    v = v / np.linalg.norm(v)
    return [float(x) for x in v]


class FakeEmbedder:
    """Stand-in for ``embed_passages(model, texts)`` that records every call."""

    def __init__(
        self,
        *,
        fail_on_call: int | None = None,
        on_call: Callable[[int], None] | None = None,
        dim: int = FAKE_DIM,
    ) -> None:
        """Optionally raise on the n-th call (1-based) or run a hook before each call."""
        self.calls: list[list[str]] = []
        self.models: list[object] = []
        self.fail_on_call = fail_on_call
        self.on_call = on_call
        self.dim = dim

    def __call__(self, model: object, texts: list[str]) -> list[list[float]]:
        """Record the call and return one deterministic unit vector per text."""
        self.calls.append(list(texts))
        self.models.append(model)
        n = len(self.calls)
        if self.on_call is not None:
            self.on_call(n)
        if self.fail_on_call == n:
            raise RuntimeError(f"embedder crashed on call {n}")
        return [_fake_vector(t, self.dim) for t in texts]

    @property
    def texts(self) -> list[str]:
        """Every text embedded, flattened across calls."""
        return [t for call in self.calls for t in call]


class FakeSample:
    """Stand-in for ``embed_sample(texts)``; optionally overrides vectors per text."""

    def __init__(
        self, *, dim: int = FAKE_DIM, override: dict[str, list[float]] | None = None
    ) -> None:
        """Return ``_fake_vector`` unless the text has an override."""
        self.calls: list[list[str]] = []
        self.dim = dim
        self.override = override or {}

    def __call__(self, texts: list[str]) -> list[list[float]]:
        """Record the call and return vectors for the texts."""
        self.calls.append(list(texts))
        return [self.override.get(t, _fake_vector(t, self.dim)) for t in texts]

    @property
    def texts(self) -> list[str]:
        """Every text sampled, flattened across calls."""
        return [t for call in self.calls for t in call]


_GREEK = [
    "Δασικές πυρκαγιές και καμένες εκτάσεις ανά έτος",
    "Εμβολιασμοί κατά της COVID-19 και δόσεις εμβολίων",
    "Τροχαία ατυχήματα και οδικές συγκρούσεις",
    "Αφίξεις τουριστών και διανυκτερεύσεις σε ξενοδοχεία",
    "Κίνηση λιμένων και ακτοπλοΐα",
]


def _texts(n: int, seed: int = 1) -> dict[str, str]:
    """``n`` datasets with distinct Greek embed_texts keyed by realistic ids."""
    return {i: f"{_GREEK[k % len(_GREEK)]} #{k}" for k, i in enumerate(_ids(n, seed))}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    """Install a recording fake as ``pythia.retrieval.dense.embed_passages``."""
    emb = FakeEmbedder()
    monkeypatch.setattr(dense, "embed_passages", emb)
    return emb


def _build(path: Path, *, model_name: str = FAKE_MODEL, batch_size: int = 256) -> int:
    """Run ``build_dense_index`` on a fresh FK-enabled connection, then close it."""
    conn = db.connect(path)
    try:
        return dense.build_dense_index(conn, object(), model_name=model_name, batch_size=batch_size)
    finally:
        conn.close()


# --- DenseIndex.search: exactness ------------------------------------------------------


def test_search_equals_brute_force_ids_and_scores(tmp_path: Path) -> None:
    """For k in {1,10,50,500,600} search == brute-force cosine top-k, ids AND scores."""
    rng = np.random.default_rng(0)
    ids = _ids(500, seed=0)
    matrix = _unit_rows(rng.standard_normal((500, 64)))
    path = tmp_path / "catalog.sqlite"
    _insert_vectors(path, ids, matrix)

    index = dense.load_dense_index(path, FAKE_MODEL)
    assert len(index.ids) == 500

    # Query seeds chosen so every adjacent score gap exceeds 8e-6: more than twice the
    # worst-case float32 accumulation error over 64 terms (64 * 2**-24 ~ 3.8e-6), so a
    # correct float32 implementation cannot legitimately order any pair differently.
    for qseed in (71, 249, 627):
        q = _unit_rows(np.random.default_rng(qseed).standard_normal((1, 64)))[0]
        scores = matrix.astype(np.float64) @ q.astype(np.float64)
        order = np.argsort(-scores, kind="stable")
        gaps = -np.diff(scores[order])
        assert gaps.min() > 8e-6, "precondition: no near-ties, so the order is unambiguous"

        for k in (1, 10, 50, 500, 600):
            expected = order[: min(k, 500)]
            for query in (q, q.tolist()):
                got = index.search(query, k)
                assert [i for i, _ in got] == [ids[j] for j in expected]
                assert all(isinstance(s, float) for _, s in got)
                np.testing.assert_allclose(
                    [s for _, s in got], scores[expected], rtol=0, atol=1e-5
                )


# --- DenseIndex.search: tie safety -----------------------------------------------------


def _tie_fixture(with_higher: bool) -> tuple[list[str], FloatArray, list[str], list[str]]:
    """Rows whose scores against q=e0 are EXACT: 3 higher, 120 tied at 0.5, 40 lower.

    Each row is ``[r, sqrt(1 - r^2), 0, ...]`` with ``r`` a dyadic rational, so the dot
    product with e0 is exactly ``r`` whatever the BLAS summation order. Returns
    (all_ids, matrix, higher_ids_best_first, tied_ids).
    """
    ids = _ids(163, seed=42)
    rng = random.Random(7)
    rng.shuffle(ids)  # tied ids interleave with the others in sort order
    higher_r = [0.875, 0.75, 0.625] if with_higher else []
    lower_r = [-0.5 + i / 128 for i in range(40)]
    rs = higher_r + [0.5] * 120 + lower_r
    ids = ids[: len(rs)]
    rows = np.zeros((len(rs), 8), dtype=np.float64)
    for n, r in enumerate(rs):
        rows[n, 0] = r
        rows[n, 1] = np.sqrt(1.0 - r * r)
    matrix = rows.astype("<f4")
    nh = len(higher_r)
    return ids, matrix, ids[:nh], ids[nh : nh + 120]


@pytest.mark.parametrize("with_higher", [False, True])
def test_search_tie_straddling_k_returns_lowest_ids(tmp_path: Path, with_higher: bool) -> None:
    """120 identical rows straddling k=10 resolve by lowest dataset_id, on every call."""
    ids, matrix, higher, tied = _tie_fixture(with_higher)
    path = tmp_path / "catalog.sqlite"
    _insert_vectors(path, ids, matrix)
    index = dense.load_dense_index(path, FAKE_MODEL)
    q = [1.0] + [0.0] * 7

    expected = higher + sorted(tied)[: 10 - len(higher)]
    first = index.search(q, 10)
    assert [i for i, _ in first] == expected
    tied_scores = [s for i, s in first if i in set(tied)]
    assert tied_scores and all(s == pytest.approx(0.5, abs=1e-6) for s in tied_scores)
    for _ in range(3):
        assert index.search(q, 10) == first

    # A k that ends inside the tied block of a larger window, and one past it.
    k = len(higher) + 60
    assert [i for i, _ in index.search(q, k)] == higher + sorted(tied)[:60]
    assert [i for i, _ in index.search(q, len(higher) + 120)] == higher + sorted(tied)


def test_search_ties_independent_of_insertion_order(tmp_path: Path) -> None:
    """The same rows inserted in two different orders give identical tie-broken results."""
    ids, matrix, _, tied = _tie_fixture(with_higher=False)
    order_a = sorted(range(len(ids)), key=lambda n: ids[n], reverse=True)
    order_b = list(range(len(ids)))
    random.Random(3).shuffle(order_b)

    results = []
    for name, order in (("a", order_a), ("b", order_b)):
        path = tmp_path / f"catalog_{name}.sqlite"
        _insert_vectors(path, [ids[n] for n in order], matrix[order])
        index = dense.load_dense_index(path, FAKE_MODEL)
        results.append(index.search([1.0] + [0.0] * 7, 10))

    assert results[0] == results[1]
    assert [i for i, _ in results[0]] == sorted(tied)[:10]


# --- load_dense_index: alignment, attributes, edge cases ------------------------------


def test_load_alignment_round_trip_per_id(tmp_path: Path) -> None:
    """Loaded rows are ordered by dataset_id; each id maps to its own vector and finds itself."""
    ids = _ids(200, seed=5)
    matrix = _unit_rows(np.random.default_rng(5).standard_normal((200, 64)))
    shuffled = list(range(200))
    random.Random(9).shuffle(shuffled)
    path = tmp_path / "catalog.sqlite"
    _insert_vectors(path, [ids[n] for n in shuffled], matrix[shuffled])

    index = dense.load_dense_index(path, FAKE_MODEL)
    assert list(index.ids) == sorted(ids)
    assert index.matrix.dtype == np.float32
    assert index.matrix.shape == (200, 64)
    assert index.model == FAKE_MODEL

    loaded = {i: index.matrix[n].tobytes() for n, i in enumerate(index.ids)}
    original = {i: matrix[n].astype("<f4").tobytes() for n, i in enumerate(ids)}
    assert loaded == original

    for n, dataset_id in enumerate(ids):
        top = index.search(matrix[n], 1)
        assert top[0][0] == dataset_id
        assert top[0][1] == pytest.approx(1.0, abs=1e-5)


def test_load_version_is_max_embedded_at(tmp_path: Path) -> None:
    """``version`` is the max ``embedded_at`` over the model's rows."""
    path = tmp_path / "catalog.sqlite"
    ids = _ids(3, seed=11)
    _catalog(path, {i: f"t {i}" for i in ids})
    m = _unit_rows(np.random.default_rng(11).standard_normal((3, 8)))
    stamps = [
        "2026-10-30T10:00:00.000001+00:00",
        "2026-10-30T12:30:00.123456+00:00",
        "2026-10-29T23:59:59.999999+00:00",
    ]
    for n, (dataset_id, at) in enumerate(zip(ids, stamps, strict=True)):
        _insert_vectors(path, [dataset_id], m[n : n + 1], embedded_at=at, create_datasets=False)

    assert dense.load_dense_index(path, FAKE_MODEL).version == stamps[1]
    assert dense.count_vectors(path, FAKE_MODEL) == (3, stamps[1])


def test_load_missing_file_is_empty_and_not_created(tmp_path: Path) -> None:
    """A missing catalogue loads as an empty index, without raising and without creating it."""
    path = tmp_path / "absent.sqlite"
    index = dense.load_dense_index(path, FAKE_MODEL)
    assert len(index.ids) == 0
    assert index.version is None
    assert index.search([1.0] + [0.0] * 7, 5) == []
    assert not path.exists()


def test_load_missing_table_is_empty(tmp_path: Path) -> None:
    """A catalogue without ``dataset_embeddings`` loads as an empty index."""
    path = tmp_path / "catalog.sqlite"
    _catalog(path, _texts(3), schema=False)
    index = dense.load_dense_index(path, FAKE_MODEL)
    assert len(index.ids) == 0
    assert index.version is None
    assert index.search([1.0] + [0.0] * 7, 5) == []


def test_load_empty_table_is_empty(tmp_path: Path) -> None:
    """An existing but empty ``dataset_embeddings`` loads as an empty index."""
    path = tmp_path / "catalog.sqlite"
    _catalog(path, _texts(3))
    index = dense.load_dense_index(path, FAKE_MODEL)
    assert len(index.ids) == 0
    assert index.version is None
    assert index.search([1.0] + [0.0] * 7, 5) == []


def test_load_excludes_other_model_rows(tmp_path: Path) -> None:
    """Rows stored under another model name (even another dimension) are not loaded."""
    path = tmp_path / "catalog.sqlite"
    ids = _ids(6, seed=13)
    _catalog(path, {i: f"t {i}" for i in ids})
    m8 = _unit_rows(np.random.default_rng(1).standard_normal((3, 8)))
    m16 = _unit_rows(np.random.default_rng(2).standard_normal((3, 16)))
    _insert_vectors(path, ids[:3], m8, model=FAKE_MODEL, create_datasets=False)
    _insert_vectors(path, ids[3:], m16, model=OTHER_MODEL, create_datasets=False)

    index = dense.load_dense_index(path, FAKE_MODEL)
    assert list(index.ids) == sorted(ids[:3])
    assert index.matrix.shape == (3, 8)
    other = dense.load_dense_index(path, OTHER_MODEL)
    assert list(other.ids) == sorted(ids[3:])
    assert other.matrix.shape == (3, 16)


def test_matrix_is_not_writeable(tmp_path: Path) -> None:
    """The snapshot is immutable: ``matrix.flags.writeable`` is False and writes raise."""
    path = tmp_path / "catalog.sqlite"
    ids = _ids(4, seed=17)
    _insert_vectors(path, ids, _unit_rows(np.random.default_rng(3).standard_normal((4, 8))))
    index = dense.load_dense_index(path, FAKE_MODEL)
    assert index.matrix.flags.writeable is False
    with pytest.raises(ValueError):
        index.matrix[0, 0] = 0.0


def test_load_corrupt_blob_length_raises(tmp_path: Path) -> None:
    """A BLOB whose length is not 4*dim is rejected on load."""
    path = tmp_path / "catalog.sqlite"
    ids = _ids(2, seed=19)
    _insert_vectors(path, ids[:1], _unit_rows(np.ones((1, 8))))
    _catalog(path, {ids[1]: "δεύτερο"})
    _raw_insert(path, ids[1], FAKE_MODEL, 8, np.ones(7, dtype="<f4").tobytes())
    with pytest.raises(ValueError):
        dense.load_dense_index(path, FAKE_MODEL)


def test_load_mixed_dims_raises(tmp_path: Path) -> None:
    """Two rows of the same model with different (self-consistent) dims are rejected."""
    path = tmp_path / "catalog.sqlite"
    ids = _ids(2, seed=23)
    _insert_vectors(path, ids[:1], _unit_rows(np.ones((1, 8))))
    _catalog(path, {ids[1]: "δεύτερο"})
    _raw_insert(path, ids[1], FAKE_MODEL, 4, _unit_rows(np.ones((1, 4)))[0].tobytes())
    with pytest.raises(ValueError):
        dense.load_dense_index(path, FAKE_MODEL)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_load_non_finite_raises(tmp_path: Path, bad: float) -> None:
    """A stored vector containing NaN or inf is rejected on load."""
    path = tmp_path / "catalog.sqlite"
    ids = _ids(2, seed=29)
    vec = _unit_rows(np.ones((1, 8)))[0].copy()
    vec[3] = bad
    _insert_vectors(path, ids[:1], _unit_rows(np.ones((1, 8))))
    _catalog(path, {ids[1]: "δεύτερο"})
    _raw_insert(path, ids[1], FAKE_MODEL, 8, vec.astype("<f4").tobytes())
    with pytest.raises(ValueError):
        dense.load_dense_index(path, FAKE_MODEL)


def test_search_k_zero_or_negative_returns_empty(tmp_path: Path) -> None:
    """``k <= 0`` returns [] on a populated index."""
    path = tmp_path / "catalog.sqlite"
    _insert_vectors(path, _ids(5, seed=31), _unit_rows(np.eye(5, 8)))
    index = dense.load_dense_index(path, FAKE_MODEL)
    assert index.search([1.0] + [0.0] * 7, 0) == []
    assert index.search([1.0] + [0.0] * 7, -3) == []


def test_search_dimension_mismatch_raises(tmp_path: Path) -> None:
    """A query whose length differs from ``matrix.shape[1]`` raises ValueError."""
    path = tmp_path / "catalog.sqlite"
    _insert_vectors(path, _ids(5, seed=37), _unit_rows(np.eye(5, 8)))
    index = dense.load_dense_index(path, FAKE_MODEL)
    with pytest.raises(ValueError):
        index.search([1.0] + [0.0] * 6, 3)
    with pytest.raises(ValueError):
        index.search(np.ones(9, dtype=np.float32), 3)


# --- count_vectors ----------------------------------------------------------------------


def test_count_vectors_missing_table_and_file(tmp_path: Path) -> None:
    """``(0, None)`` for a missing table and for a missing file (which is not created)."""
    path = tmp_path / "catalog.sqlite"
    _catalog(path, _texts(2), schema=False)
    assert dense.count_vectors(path, FAKE_MODEL) == (0, None)
    absent = tmp_path / "absent.sqlite"
    assert dense.count_vectors(absent, FAKE_MODEL) == (0, None)
    assert not absent.exists()


def test_count_vectors_counts_only_the_model(tmp_path: Path) -> None:
    """Counts rows for the named model only; an empty table is ``(0, None)``."""
    path = tmp_path / "catalog.sqlite"
    ids = _ids(5, seed=41)
    _catalog(path, {i: f"t {i}" for i in ids})
    assert dense.count_vectors(path, FAKE_MODEL) == (0, None)
    m = _unit_rows(np.random.default_rng(4).standard_normal((5, 8)))
    _insert_vectors(path, ids[:2], m[:2], model=FAKE_MODEL, create_datasets=False,
                    embedded_at="2026-10-30T01:00:00.000000+00:00")
    _insert_vectors(path, ids[2:], m[2:], model=OTHER_MODEL, create_datasets=False,
                    embedded_at="2026-10-31T01:00:00.000000+00:00")
    assert dense.count_vectors(path, FAKE_MODEL) == (2, "2026-10-30T01:00:00.000000+00:00")
    assert dense.count_vectors(path, OTHER_MODEL) == (3, "2026-10-31T01:00:00.000000+00:00")


# --- build_dense_index ------------------------------------------------------------------

_MICROS_UTC = re.compile(r"\.\d{6}")


def test_build_first_run_embeds_all_and_persists(tmp_path: Path, fake: FakeEmbedder) -> None:
    """A first build embeds every dataset in batches and commits rows a new connection sees."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(5)
    _catalog(path, texts)

    sentinel = object()
    conn = db.connect(path)
    assert dense.build_dense_index(conn, sentinel, model_name=FAKE_MODEL, batch_size=2) == 5
    conn.close()
    assert sorted(fake.texts) == sorted(texts.values())
    assert all(m is sentinel for m in fake.models)
    assert all(1 <= len(call) <= 2 for call in fake.calls)

    stored = _stored(path)
    assert set(stored) == set(texts)
    for dataset_id, text in texts.items():
        model, dim, sig, at, blob = stored[dataset_id]
        assert model == FAKE_MODEL
        assert dim == FAKE_DIM
        assert sig == _sig(text)
        assert blob == np.asarray(_fake_vector(text), dtype="<f4").tobytes()
        assert len(blob) == 4 * dim
        assert _MICROS_UTC.search(at), at
        assert datetime.fromisoformat(at).utcoffset() == timedelta(0)

    index = dense.load_dense_index(path, FAKE_MODEL)
    assert list(index.ids) == sorted(texts)


def test_build_second_run_embeds_nothing(tmp_path: Path, fake: FakeEmbedder) -> None:
    """An unchanged catalogue re-embeds nothing and leaves rows untouched."""
    path = tmp_path / "catalog.sqlite"
    _catalog(path, _texts(5))
    assert _build(path) == 5
    before = _stored(path)
    fake.calls.clear()

    assert _build(path) == 0
    assert fake.texts == []
    assert _stored(path) == before


def test_build_edited_text_embeds_one_and_changes_version(
    tmp_path: Path, fake: FakeEmbedder
) -> None:
    """One edited embed_text re-embeds exactly that dataset; count stays, version moves."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(5)
    _catalog(path, texts)
    assert _build(path) == 5
    count_before, version_before = dense.count_vectors(path, FAKE_MODEL)
    before = _stored(path)
    fake.calls.clear()

    edited_id = sorted(texts)[2]
    new_text = "Ποιότητα νερού κολύμβησης στις ακτές"
    conn = db.connect(path)
    db.upsert_dataset(conn, _row(edited_id, new_text))
    conn.commit()
    conn.close()

    assert _build(path) == 1
    assert fake.texts == [new_text]
    count_after, version_after = dense.count_vectors(path, FAKE_MODEL)
    assert count_after == count_before == 5
    assert version_after is not None and version_before is not None
    assert version_after != version_before
    assert version_after > version_before

    after = _stored(path)
    assert after[edited_id][2] == _sig(new_text)
    assert after[edited_id][4] == np.asarray(_fake_vector(new_text), dtype="<f4").tobytes()
    for dataset_id in texts:
        if dataset_id != edited_id:
            assert after[dataset_id] == before[dataset_id]


def test_build_different_model_reembeds_all(tmp_path: Path, fake: FakeEmbedder) -> None:
    """A different model_name re-embeds every dataset, replacing the old model's rows."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(4)
    _catalog(path, texts)
    assert _build(path, model_name=FAKE_MODEL) == 4
    fake.calls.clear()

    assert _build(path, model_name=OTHER_MODEL) == 4
    assert sorted(fake.texts) == sorted(texts.values())
    assert dense.count_vectors(path, OTHER_MODEL)[0] == 4
    assert dense.count_vectors(path, FAKE_MODEL) == (0, None)
    assert {v[0] for v in _stored(path).values()} == {OTHER_MODEL}


def test_build_crash_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash on batch 2 leaves batch 1 committed; the rerun embeds only the rest."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(5)
    _catalog(path, texts)

    crashing = FakeEmbedder(fail_on_call=2)
    monkeypatch.setattr(dense, "embed_passages", crashing)
    conn = db.connect(path)
    with pytest.raises(RuntimeError):
        dense.build_dense_index(conn, object(), model_name=FAKE_MODEL, batch_size=2)
    conn.close()

    survived = _stored(path)
    assert len(survived) == 2
    assert {texts[i] for i in survived} == set(crashing.calls[0])

    healthy = FakeEmbedder()
    monkeypatch.setattr(dense, "embed_passages", healthy)
    assert _build(path, batch_size=2) == 3
    assert sorted(healthy.texts) == sorted(t for i, t in texts.items() if i not in survived)
    assert set(_stored(path)) == set(texts)


def test_build_commits_per_batch_and_holds_no_lock_while_embedding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While batch 2 is embedding, batch 1 is committed and no write lock is held."""
    path = tmp_path / "catalog.sqlite"
    _catalog(path, _texts(5))
    observed: list[tuple[int, int]] = []

    def probe(call: int) -> None:
        """Take a write lock from another connection and count committed rows."""
        other = sqlite3.connect(path, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")  # raises 'database is locked' if build holds one
            other.rollback()
            committed = other.execute("SELECT count(*) FROM dataset_embeddings").fetchone()[0]
            observed.append((call, int(committed)))
        finally:
            other.close()

    monkeypatch.setattr(dense, "embed_passages", FakeEmbedder(on_call=probe))
    assert _build(path, batch_size=2) == 5
    assert observed == [(1, 0), (2, 2), (3, 4)]


def test_build_with_real_e5_small(tmp_path: Path) -> None:
    """End to end with the real (cached, offline) e5-small: 384-dim rows, topical top-1."""
    from pythia.retrieval.embed import embed_query, load_model

    model = load_model(TEST_MODEL)
    ids = _ids(4, seed=101)
    topics = {
        ids[0]: "Δασικές πυρκαγιές και καμένες εκτάσεις ανά έτος",
        ids[1]: "Εμβολιασμοί κατά της COVID-19 και δόσεις εμβολίων",
        ids[2]: "Τροχαία ατυχήματα και οδικές συγκρούσεις",
        ids[3]: "Τουρισμός και αφίξεις επισκεπτών στην Ελλάδα",
    }
    path = tmp_path / "catalog.sqlite"
    _catalog(path, topics)

    conn = db.connect(path)
    assert dense.build_dense_index(conn, model, model_name=TEST_MODEL) == 4
    conn.close()

    index = dense.load_dense_index(path, TEST_MODEL)
    assert index.matrix.shape == (4, 384)
    assert np.allclose(np.linalg.norm(index.matrix, axis=1), 1.0, atol=1e-3)
    assert index.search(embed_query(model, "πόσα εμβόλια έγιναν;"), 4)[0][0] == ids[1]
    assert index.search(embed_query(model, "καμένες εκτάσεις από φωτιές"), 4)[0][0] == ids[0]


# --- orphans and the REPLACE trap -------------------------------------------------------


def test_dataset_delete_cascades_to_vector(tmp_path: Path, fake: FakeEmbedder) -> None:
    """Deleting a dataset through ``ingest.db.connect()`` (FKs on) cascades to its vector."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(3)
    _catalog(path, texts)
    assert _build(path) == 3
    victim = sorted(texts)[0]

    conn = db.connect(path)
    conn.execute("DELETE FROM datasets WHERE id = ?", (victim,))
    conn.commit()
    conn.close()

    assert set(_stored(path)) == set(texts) - {victim}


def test_build_sweeps_orphan_inserted_with_fks_off(tmp_path: Path, fake: FakeEmbedder) -> None:
    """A vector row whose dataset does not exist (inserted with FKs off) is swept by a build."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(3)
    _catalog(path, texts)
    assert _build(path) == 3
    before = _stored(path)

    orphan = "c11b37ca51bbba1e613ca9d2eff93f27"  # 32-hex shape, not in the catalogue
    _raw_insert(path, orphan, FAKE_MODEL, FAKE_DIM, _unit_rows(np.ones((1, 8)))[0].tobytes())
    assert orphan in _stored(path)  # precondition: FKs off let it in

    fake.calls.clear()
    assert _build(path) == 0
    assert fake.texts == []
    assert _stored(path) == before


def test_reupsert_keeps_vector_and_insert_or_replace_control_deletes_it(
    tmp_path: Path, fake: FakeEmbedder
) -> None:
    """``upsert_dataset`` keeps the vector; the INSERT OR REPLACE control proves detection."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(3)
    _catalog(path, texts)
    assert _build(path) == 3
    target = sorted(texts)[1]
    before = _stored(path)

    conn = db.connect(path)
    db.upsert_dataset(conn, _row(target, texts[target], title="νέος τίτλος"))
    conn.commit()
    conn.close()
    assert _stored(path) == before

    conn = db.connect(path)
    conn.execute(
        "INSERT OR REPLACE INTO datasets (id, name, num_resources, harvested_at, embed_text) "
        "VALUES (?, ?, 0, '2026-06-01T00:00:00', ?)",
        (target, "replaced", texts[target]),
    )
    conn.commit()
    conn.close()
    assert target not in _stored(path)
    assert set(_stored(path)) == set(texts) - {target}


# --- import_vectors ---------------------------------------------------------------------


def _import_inputs(texts: dict[str, str]) -> tuple[list[str], FloatArray, list[str]]:
    """ids, float32 vectors and current sigs for a catalogue, as a migration would supply."""
    ids = list(texts)
    vectors = np.asarray([_fake_vector(texts[i]) for i in ids], dtype="<f4")
    return ids, vectors, [_sig(texts[i]) for i in ids]


def _import(
    path: Path,
    ids: list[str],
    vectors: NDArray[Any],
    sigs: list[str],
    sample: Callable[[list[str]], list[list[float]]],
) -> int:
    """Run ``import_vectors`` on a fresh FK-enabled connection, then close it."""
    conn = db.connect(path)
    try:
        return dense.import_vectors(
            conn, ids, vectors, sigs, model_name=FAKE_MODEL, embed_sample=sample
        )
    finally:
        conn.close()


def test_import_round_trip_then_build_embeds_nothing(
    tmp_path: Path, fake: FakeEmbedder
) -> None:
    """Imported rows are byte-identical; a 30-text sample is checked; a build then embeds 0."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(40)
    _catalog(path, texts)
    ids, vectors, sigs = _import_inputs(texts)
    sample = FakeSample()

    assert _import(path, ids, vectors, sigs, sample) == 40

    stored = _stored(path)
    assert set(stored) == set(ids)
    for n, dataset_id in enumerate(ids):
        model, dim, sig, _, blob = stored[dataset_id]
        assert (model, dim, sig) == (FAKE_MODEL, FAKE_DIM, sigs[n])
        assert blob == vectors[n].astype("<f4").tobytes()
    assert len({v[3] for v in stored.values()}) == 1  # one embedded_at for the whole import

    assert len(sample.texts) == 30
    assert len(set(sample.texts)) == 30
    assert set(sample.texts) <= set(texts.values())

    assert _build(path) == 0
    assert fake.texts == []


def test_import_samples_all_when_fewer_than_30(tmp_path: Path) -> None:
    """With m < 30 current rows the sample is all m of them."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(6)
    _catalog(path, texts)
    ids, vectors, sigs = _import_inputs(texts)
    sample = FakeSample()
    assert _import(path, ids, vectors, sigs, sample) == 6
    assert sorted(sample.texts) == sorted(texts.values())


def test_import_excludes_stale_rows_from_sample(tmp_path: Path, fake: FakeEmbedder) -> None:
    """Rows whose supplied sig is stale are written but never sampled; a build re-embeds them."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(50)
    _catalog(path, texts)
    ids = list(texts)
    stale = set(ids[::5])  # 10 stale, 40 current: the 30-row sample must choose among them
    vectors_list: list[list[float]] = []
    sigs: list[str] = []
    for dataset_id in ids:
        source = f"παλιό κείμενο {dataset_id}" if dataset_id in stale else texts[dataset_id]
        vectors_list.append(_fake_vector(source))
        sigs.append(_sig(source))
    vectors = np.asarray(vectors_list, dtype="<f4")
    sample = FakeSample()

    assert _import(path, ids, vectors, sigs, sample) == 50
    stale_texts = {texts[i] for i in stale}
    assert len(sample.texts) == 30
    assert not stale_texts & set(sample.texts)
    stored = _stored(path)
    for n, dataset_id in enumerate(ids):
        assert stored[dataset_id][2] == sigs[n]

    assert _build(path) == len(stale)
    assert sorted(fake.texts) == sorted(stale_texts)


def _assert_refused(
    path: Path,
    ids: list[str],
    vectors: NDArray[Any],
    sigs: list[str],
    sample: Callable[[list[str]], list[list[float]]],
) -> pytest.ExceptionInfo[ValueError]:
    """Assert ``import_vectors`` raises ValueError and leaves ZERO rows (fresh connection)."""
    with pytest.raises(ValueError) as info:
        _import(path, ids, vectors, sigs, sample)
    assert _stored(path) == {}
    return info


def test_import_refuses_sample_shortfall(tmp_path: Path) -> None:
    """A sampled vector with cosine < 0.9999 to its re-embedding aborts the whole import."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(10)
    _catalog(path, texts)
    ids, vectors, sigs = _import_inputs(texts)
    target = texts[ids[4]]
    v = np.asarray(_fake_vector(target)) + 0.05 * np.asarray(_fake_vector("θόρυβος"))
    v = v / np.linalg.norm(v)
    assert float(v @ np.asarray(_fake_vector(target))) < 0.9999  # precondition
    _assert_refused(path, ids, vectors, sigs, FakeSample(override={target: list(v)}))


def test_import_refuses_unknown_id_and_names_it(tmp_path: Path) -> None:
    """An id absent from ``datasets`` aborts the import, naming that id."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(10)
    _catalog(path, texts)
    ids, vectors, sigs = _import_inputs(texts)
    unknown = "47ed4dbf-5e14-4ad9-8edd-3cef6373694d"
    ids[7] = unknown
    info = _assert_refused(path, ids, vectors, sigs, FakeSample())
    assert unknown in str(info.value)


def test_import_refuses_duplicate_id(tmp_path: Path) -> None:
    """A repeated id aborts the import."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(10)
    _catalog(path, texts)
    ids, vectors, sigs = _import_inputs(texts)
    ids = ids + [ids[0]]
    vectors = np.vstack([vectors, vectors[:1]])
    sigs = sigs + [sigs[0]]
    _assert_refused(path, ids, vectors, sigs, FakeSample())


def test_import_refuses_wrong_dimension(tmp_path: Path) -> None:
    """Vectors whose dimension differs from ``embed_sample``'s output are refused."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(10)
    _catalog(path, texts)
    ids, vectors, sigs = _import_inputs(texts)
    _assert_refused(path, ids, vectors, sigs, FakeSample(dim=16))


def test_import_refuses_row_count_mismatch(tmp_path: Path) -> None:
    """``vectors`` must be shaped ``(len(ids), d)``."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(10)
    _catalog(path, texts)
    ids, vectors, sigs = _import_inputs(texts)
    _assert_refused(path, ids, vectors[:-1], sigs, FakeSample())


@pytest.mark.parametrize("kind", ["non_unit", "nan", "inf"])
def test_import_refuses_non_unit_or_non_finite_row(tmp_path: Path, kind: str) -> None:
    """A row off unit norm (beyond 1e-3) or containing NaN/inf aborts the import."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(10)
    _catalog(path, texts)
    ids, vectors, sigs = _import_inputs(texts)
    vectors = vectors.copy()
    if kind == "non_unit":
        vectors[3] = vectors[3] * np.float32(1.01)
    else:
        vectors[3, 0] = np.float32("nan" if kind == "nan" else "inf")
    _assert_refused(path, ids, vectors, sigs, FakeSample())


def test_import_refuses_non_empty_table_for_model(tmp_path: Path) -> None:
    """Any existing row for the model refuses the import and leaves that row untouched."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(10)
    _catalog(path, texts)
    ids, vectors, sigs = _import_inputs(texts)
    existing = np.asarray([_fake_vector("νεότερη ενσωμάτωση")], dtype="<f4")
    _insert_vectors(path, ids[:1], existing, create_datasets=False,
                    embedded_at="2026-10-31T00:00:00.000000+00:00")
    before = _stored(path)
    assert len(before) == 1

    with pytest.raises(ValueError):
        _import(path, ids, vectors, sigs, FakeSample())
    assert _stored(path) == before


def test_import_refuses_when_no_current_sig_rows(tmp_path: Path) -> None:
    """If no supplied sig matches the current embed_text (m == 0), the import is refused."""
    path = tmp_path / "catalog.sqlite"
    texts = _texts(10)
    _catalog(path, texts)
    ids, vectors, _ = _import_inputs(texts)
    stale_sigs = [_sig(f"παλιό {i}") for i in ids]
    _assert_refused(path, ids, vectors, stale_sigs, FakeSample())
