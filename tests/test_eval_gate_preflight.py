"""Tests for the eval-gate preflight's dense-index check (spec Step 6, P24).

The script lives at ``.claude/skills/eval-gate/scripts/preflight.py`` and is imported by path.
Contract pinned here:

``index_state(db_path: str, model_name: str, embed_texts: dict[str, str]) -> tuple[int, int, int]``
returns ``(indexed, stale, orphaned)`` read from ``dataset_embeddings``:

- ``indexed``  = rows whose ``model`` is ``model_name`` (orphans included: they are rows);
- ``stale``    = datasets in ``embed_texts`` needing (re-)embedding: no row, a ``sig`` that is
  not ``_signature(embed_text)``, or a row written under a different model;
- ``orphaned`` = ``model_name`` rows whose ``dataset_id`` is not in ``embed_texts``.

A catalogue without the table reads as nothing indexed and everything stale, never a raise.
The script must no longer import ``chromadb``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sqlite3
import struct
from pathlib import Path
from types import ModuleType

import pytest

from pythia.ingest import db
from pythia.ingest.models import DatasetRow

SCRIPT = Path(__file__).resolve().parents[1] / ".claude/skills/eval-gate/scripts/preflight.py"
MODEL = "intfloat/multilingual-e5-large"
OTHER_MODEL = "intfloat/multilingual-e5-small"

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


@pytest.fixture(scope="module")
def preflight() -> ModuleType:
    """Import the preflight script by path, as ``uv run python <path>`` would run it."""
    spec = importlib.util.spec_from_file_location("eval_gate_preflight", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sig(text: str) -> str:
    """The content signature: sha1 of the UTF-8 embed_text."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _row(dataset_id: str, embed_text: str) -> DatasetRow:
    """A minimal dataset row."""
    return DatasetRow(
        id=dataset_id, name=f"name-{dataset_id}", title=embed_text, title_en=None, notes=None,
        notes_en=None, org_name=None, org_title=None, license_id=None, license_title=None,
        frequency=None, language_options=[], theme=[], num_resources=0, tags=[],
        temporal_start=None, temporal_end=None, spatial_text=None, metadata_created=None,
        last_updated="2026-01-01T00:00:00", state="active",
        harvested_at="2026-06-01T00:00:00", embed_text=embed_text,
    )


_TEXTS = {
    "ds-current": "Δασικές πυρκαγιές ανά έτος",
    "ds-stale-sig": "Εμβολιασμοί COVID-19 ανά ημέρα",
    "ds-other-model": "Τροχαία ατυχήματα",
    "ds-missing": "Αφίξεις τουριστών",
}


def _catalogue(path: Path, *, table: bool = True) -> dict[str, str]:
    """Four datasets; dense rows: one current, one stale sig, one other-model, one orphan.

    ``ds-missing`` has no row at all. Returns ``{dataset_id: embed_text}`` as
    ``catalog_counts`` would.
    """
    conn = db.connect(path)
    db.init_db(conn)
    for dataset_id, text in _TEXTS.items():
        db.upsert_dataset(conn, _row(dataset_id, text))
    conn.commit()
    conn.close()
    if table:
        raw = sqlite3.connect(path)  # foreign keys OFF, so the orphan can be inserted
        raw.executescript(_DENSE_DDL)
        rows = [
            ("ds-current", MODEL, _sig(_TEXTS["ds-current"])),
            ("ds-stale-sig", MODEL, _sig("an older embed_text")),
            ("ds-other-model", OTHER_MODEL, _sig(_TEXTS["ds-other-model"])),
            ("ds-orphan", MODEL, _sig("a dataset no longer in the catalogue")),
        ]
        for dataset_id, model, sig in rows:
            raw.execute(
                "INSERT INTO dataset_embeddings (dataset_id, model, dim, sig, embedded_at, vector)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (dataset_id, model, 2, sig, "2026-10-30T12:00:00.000001+00:00",
                 struct.pack("<2f", 1.0, 0.0)),
            )
        raw.commit()
        raw.close()
    return dict(_TEXTS)


def test_index_state_reads_dataset_embeddings(preflight: ModuleType, tmp_path: Path) -> None:
    """indexed=3 (current, stale-sig, orphan); stale=3 (sig, model, missing); orphaned=1."""
    path = tmp_path / "catalog.sqlite"
    texts = _catalogue(path)

    assert preflight.index_state(str(path), MODEL, texts) == (3, 3, 1)


def test_a_model_mismatch_alone_is_stale(preflight: ModuleType, tmp_path: Path) -> None:
    """P24: a row with the right sig under another model still needs re-embedding."""
    path = tmp_path / "catalog.sqlite"
    _catalogue(path)
    only = {"ds-other-model": _TEXTS["ds-other-model"]}

    indexed, stale, _ = preflight.index_state(str(path), MODEL, only)

    assert stale == 1
    assert indexed == 3, "indexed counts every row for the configured model"


def test_a_clean_index_reports_zero_stale_and_zero_orphaned(
    preflight: ModuleType, tmp_path: Path
) -> None:
    """The post-migration expectation (AC5): every dataset current, nothing orphaned."""
    path = tmp_path / "catalog.sqlite"
    _catalogue(path)
    current = {"ds-current": _TEXTS["ds-current"]}
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM dataset_embeddings WHERE dataset_id <> 'ds-current'")
    conn.commit()
    conn.close()

    assert preflight.index_state(str(path), MODEL, current) == (1, 0, 0)


def test_index_state_without_the_table_is_all_stale(
    preflight: ModuleType, tmp_path: Path
) -> None:
    """A pre-migration catalogue: nothing indexed, every dataset stale, no raise."""
    path = tmp_path / "catalog.sqlite"
    texts = _catalogue(path, table=False)

    assert preflight.index_state(str(path), MODEL, texts) == (0, len(texts), 0)


def test_preflight_no_longer_imports_chromadb() -> None:
    """The eval gate reads the catalogue; ``chromadb`` is gone from the project."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+chromadb(\.|\s|$)", source, re.MULTILINE)
