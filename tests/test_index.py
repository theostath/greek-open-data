"""Tests for ``python -m pythia.retrieval.index`` (``make index``) after the Chroma removal.

Contract pinned here (spec Step 6, Relevant Files ``index.py``, P19):

- ``main(argv: list[str] | None = None) -> int`` accepts ``--force``.
- It refuses a FULL build -- zero ``dataset_embeddings`` rows for ``settings.embedding_model``
  -- while the legacy Chroma directory exists, unless ``--force`` is given, so nobody starts a
  ~98-minute re-embed instead of running the migration. Refusal is a non-zero return (or a
  non-zero ``SystemExit``) that happens BEFORE the embedding model is loaded.
- Seam: the legacy directory is the module-level ``pythia.retrieval.index.CHROMA_DIR`` (a path,
  default ``data/chroma``), read at call time so tests can point it at ``tmp_path``.
- Other seams, all module attributes of ``pythia.retrieval.index``: ``get_settings``,
  ``load_model``, ``build_dense_index`` (called as ``(conn, model, model_name=..., ...)``),
  ``build_fts_index`` (called as ``(conn)``), ``configure_logging``.

No real model loads here.
"""

from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from config import Settings

from pythia.ingest import db
from pythia.ingest.models import DatasetRow
from pythia.retrieval import index

MODEL = "fake/e5-test"
OTHER_MODEL = "fake/other-model"

#: The DDL the spec pins for ``dataset_embeddings`` (Step 3), written verbatim so this file
#: does not depend on the dense module to set up its fixtures.
_DENSE_DDL = """
CREATE TABLE IF NOT EXISTS dataset_embeddings (
    dataset_id  TEXT PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    sig         TEXT NOT NULL,
    embedded_at TEXT NOT NULL,
    vector      BLOB NOT NULL
);
"""


def _row(dataset_id: str) -> DatasetRow:
    """A minimal dataset row."""
    return DatasetRow(
        id=dataset_id, name=f"name-{dataset_id}", title="Δασικές πυρκαγιές", title_en=None,
        notes=None, notes_en=None, org_name=None, org_title=None, license_id=None,
        license_title=None, frequency=None, language_options=[], theme=[], num_resources=0,
        tags=[], temporal_start=None, temporal_end=None, spatial_text=None,
        metadata_created=None, last_updated="2026-01-01T00:00:00", state="active",
        harvested_at="2026-06-01T00:00:00", embed_text=f"Δασικές πυρκαγιές {dataset_id}",
    )


def _catalogue(path: Path, *, vectors_for: str | None = None) -> None:
    """Two datasets; optionally one dense row for each, written under ``vectors_for``."""
    conn = db.connect(path)
    db.init_db(conn)
    for dataset_id in ("ds-a", "ds-b"):
        db.upsert_dataset(conn, _row(dataset_id))
    if vectors_for is not None:
        conn.executescript(_DENSE_DDL)
        for dataset_id in ("ds-a", "ds-b"):
            conn.execute(
                "INSERT INTO dataset_embeddings (dataset_id, model, dim, sig, embedded_at, vector)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (dataset_id, vectors_for, 2, "sig", "2026-10-30T12:00:00.000001+00:00",
                 struct.pack("<2f", 1.0, 0.0)),
            )
    conn.commit()
    conn.close()


@dataclass
class Recorder:
    """What ``main`` did with the faked heavy seams."""

    model_loads: list[str] = field(default_factory=list)
    dense_calls: list[tuple[Any, Any, dict[str, Any]]] = field(default_factory=list)
    fts_calls: list[Any] = field(default_factory=list)
    model: object = field(default_factory=object)


@pytest.fixture
def rec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Point settings at a tmp catalogue, CHROMA_DIR at tmp, and fake every heavy seam."""
    r = Recorder()
    settings = Settings(catalog_db_path=str(tmp_path / "catalog.sqlite"), embedding_model=MODEL)

    def fake_load_model(name: str | None = None) -> object:
        """Record the load instead of reading 2.2 GB of weights."""
        r.model_loads.append(str(name))
        return r.model

    def fake_build_dense(conn: Any, model: Any, **kw: Any) -> int:
        """Record the build call."""
        r.dense_calls.append((conn, model, kw))
        return 0

    def fake_build_fts(conn: Any) -> int:
        """Record the FTS rebuild."""
        r.fts_calls.append(conn)
        return 2

    monkeypatch.setattr(index, "CHROMA_DIR", tmp_path / "chroma")
    monkeypatch.setattr(index, "get_settings", lambda: settings)
    monkeypatch.setattr(index, "load_model", fake_load_model)
    monkeypatch.setattr(index, "build_dense_index", fake_build_dense)
    monkeypatch.setattr(index, "build_fts_index", fake_build_fts)
    monkeypatch.setattr(index, "configure_logging", lambda *_a, **_k: None)
    return r


def _main(argv: list[str]) -> int | str | None:
    """Run ``index.main`` and normalise a ``SystemExit`` into its code."""
    try:
        return index.main(argv)
    except SystemExit as exc:
        return exc.code


def _refused(code: int | str | None) -> bool:
    """A refusal is any non-zero, non-None exit code."""
    return code not in (0, None)


def test_full_build_is_refused_while_the_chroma_dir_exists(tmp_path: Path, rec: Recorder) -> None:
    """Zero rows for the model + ``data/chroma`` present: refuse, and load nothing first."""
    _catalogue(tmp_path / "catalog.sqlite")
    (tmp_path / "chroma").mkdir()

    assert _refused(_main([]))
    assert rec.model_loads == [], "the refusal must come before the 2.2 GB model load"
    assert rec.dense_calls == []


def test_rows_for_another_model_still_count_as_a_full_build(
    tmp_path: Path, rec: Recorder
) -> None:
    """Only rows for ``settings.embedding_model`` make a build incremental."""
    _catalogue(tmp_path / "catalog.sqlite", vectors_for=OTHER_MODEL)
    (tmp_path / "chroma").mkdir()

    assert _refused(_main([]))
    assert rec.dense_calls == []


def test_a_catalogue_without_the_dense_table_is_a_full_build(
    tmp_path: Path, rec: Recorder
) -> None:
    """A pre-migration catalogue (no table at all) is guarded like an empty one."""
    _catalogue(tmp_path / "catalog.sqlite")
    (tmp_path / "chroma").mkdir()

    assert _refused(_main([]))


def test_force_overrides_the_guard_and_builds(tmp_path: Path, rec: Recorder) -> None:
    """``--force`` builds anyway: dense through ``build_dense_index``, lexical through FTS."""
    _catalogue(tmp_path / "catalog.sqlite")
    (tmp_path / "chroma").mkdir()

    assert _main(["--force"]) == 0

    assert rec.model_loads == [MODEL]
    assert len(rec.dense_calls) == 1
    conn, model, kw = rec.dense_calls[0]
    assert isinstance(conn, sqlite3.Connection)
    assert model is rec.model
    assert kw["model_name"] == MODEL
    assert len(rec.fts_calls) == 1


def test_without_a_chroma_dir_a_full_build_runs(tmp_path: Path, rec: Recorder) -> None:
    """Fresh checkout, nothing to migrate: the first build simply runs."""
    _catalogue(tmp_path / "catalog.sqlite")

    assert _main([]) == 0
    assert len(rec.dense_calls) == 1 and len(rec.fts_calls) == 1
    assert rec.dense_calls[0][2]["model_name"] == MODEL


def test_an_incremental_build_runs_even_while_the_chroma_dir_exists(
    tmp_path: Path, rec: Recorder
) -> None:
    """Once vectors exist for the model, ``make index`` is incremental and never guarded."""
    _catalogue(tmp_path / "catalog.sqlite", vectors_for=MODEL)
    (tmp_path / "chroma").mkdir()

    assert _main([]) == 0
    assert len(rec.dense_calls) == 1


def test_the_chroma_dir_seam_defaults_to_data_chroma() -> None:
    """The guard watches the directory the old store lived in."""
    assert Path(index.CHROMA_DIR).parts[-2:] == ("data", "chroma")


def test_index_no_longer_builds_chroma() -> None:
    """``build_chroma_index`` is gone from the index entry point."""
    assert not hasattr(index, "build_chroma_index")
