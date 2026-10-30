"""Tests for ``/healthz`` and ``health()`` over the exact dense index (ADR-0010, spec P3/P4/P13).

Contract pinned here:

- ``pythia.api.app.health(settings, dense_info=None)`` publishes ``dense_index`` and
  ``dense_version`` (``count_vectors(settings.catalog_db_path, settings.embedding_model)``),
  plus ``dense_loaded`` / ``dense_loaded_version`` (``dense_info``'s two halves, or ``None``).
- ``status`` is ``"degraded"`` when any of: datasets == 0, lexical == 0, dense_index == 0,
  dense_index < datasets, dense_loaded == 0, or dense_info is not None and differs from the
  table's ``(count, version)``. Otherwise ``"ok"``.
- ``/healthz`` reads ``getattr(request.app.state, "pipeline", None)`` and asks it for
  ``dense_info()``, which never triggers a load.

``_llm_reachable`` is patched in every test: Ollama may be running on this box, and a health
probe must not depend on it here.
"""

from __future__ import annotations

import sqlite3
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from config import Settings
from fastapi.testclient import TestClient
from tests.api_harness import build

from pythia.api import app as app_module
from pythia.api.service import Pipeline
from pythia.ingest import db
from pythia.ingest.models import DatasetRow
from pythia.llm import FakeLLM
from pythia.retrieval import dense as dense_mod
from pythia.retrieval.lexical import build_fts_index

MODEL = "fake/e5-test"
OTHER_MODEL = "fake/other-model"
V1 = "2026-10-30T12:00:00.000001+00:00"
V2 = "2026-10-30T12:00:00.000002+00:00"
V3 = "2026-10-30T12:00:00.000003+00:00"
IDS = ["0b5f2c1e-1111-4a3b-9c1d-000000000001", "0b5f2c1e-1111-4a3b-9c1d-000000000002",
       "7f3e9a0c2d4b4e8f9a1b2c3d4e5f6a7b"]  # both real id shapes: 36-char UUID and 32-hex


@pytest.fixture(autouse=True)
def _no_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never probe a real Ollama from a health test."""
    monkeypatch.setattr(app_module, "_llm_reachable", lambda _settings: False)


def _row(dataset_id: str) -> DatasetRow:
    """A minimal active dataset row with a Greek embed_text."""
    return DatasetRow(
        id=dataset_id, name=f"name-{dataset_id[:8]}", title="Τροχαία ατυχήματα", title_en=None,
        notes=None, notes_en=None, org_name=None, org_title=None, license_id=None,
        license_title=None, frequency=None, language_options=[], theme=[], num_resources=0,
        tags=[], temporal_start=None, temporal_end=None, spatial_text=None,
        metadata_created=None, last_updated="2026-01-01T00:00:00", state="active",
        harvested_at="2026-06-01T00:00:00", embed_text=f"Τροχαία ατυχήματα {dataset_id}",
    )


def _catalogue(
    path: Path,
    *,
    fts: bool = True,
    dense_table: bool = True,
    vectors: dict[str, str] | None = None,
    model: str = MODEL,
) -> None:
    """A FILE catalogue of the three ``IDS`` built through the real ingest layer.

    ``vectors`` maps dataset_id -> embedded_at for the rows written into
    ``dataset_embeddings`` under ``model``; ``None`` means one row per dataset (V1, V2, V3).
    """
    conn = db.connect(path)
    db.init_db(conn)
    for dataset_id in IDS:
        db.upsert_dataset(conn, _row(dataset_id))
    conn.commit()
    if fts:
        build_fts_index(conn)
        conn.commit()
    if dense_table:
        dense_mod.ensure_dense_schema(conn)
        rows = vectors if vectors is not None else dict(zip(IDS, [V1, V2, V3], strict=True))
        for n, (dataset_id, stamp) in enumerate(rows.items()):
            vector = [0.0, 0.0, 0.0, 0.0]
            vector[n % 4] = 1.0
            conn.execute(
                "INSERT INTO dataset_embeddings (dataset_id, model, dim, sig, embedded_at, vector)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (dataset_id, model, 4, "sig", stamp, struct.pack("<4f", *vector)),
            )
        conn.commit()
    conn.close()


def _settings(path: Path) -> Settings:
    """Settings pointing at the test catalogue and the fake model name."""
    return Settings(catalog_db_path=str(path), embedding_model=MODEL)


@pytest.fixture
def full(tmp_path: Path) -> Settings:
    """Three datasets, three lexical rows, three vectors for MODEL; table version V3."""
    path = tmp_path / "catalog.sqlite"
    _catalogue(path)
    return _settings(path)


# --- health(): the published facts ------------------------------------------------------


def test_health_is_ok_when_table_and_snapshot_match(full: Settings) -> None:
    """Every count matches and the loaded snapshot equals the table: ``ok``."""
    facts = app_module.health(full, dense_info=(3, V3))

    assert facts["datasets"] == 3
    assert facts["lexical_index"] == 3
    assert facts["dense_index"] == 3
    assert facts["dense_version"] == V3
    assert facts["dense_loaded"] == 3
    assert facts["dense_loaded_version"] == V3
    assert facts["status"] == "ok"


def test_health_without_dense_info_reports_nothing_loaded_and_stays_ok(full: Settings) -> None:
    """``dev.py`` calls ``health(settings)``: no pipeline means ``dense_loaded`` is ``None``."""
    facts = app_module.health(full)

    assert facts["dense_loaded"] is None
    assert facts["dense_loaded_version"] is None
    assert facts["dense_index"] == 3 and facts["dense_version"] == V3
    assert facts["status"] == "ok"


def test_health_counts_only_the_configured_model(tmp_path: Path) -> None:
    """Rows written under another model are not this deployment's dense index."""
    path = tmp_path / "catalog.sqlite"
    _catalogue(path, model=OTHER_MODEL)

    facts = app_module.health(_settings(path))

    assert facts["dense_index"] == 0
    assert facts["dense_version"] is None
    assert facts["status"] == "degraded"


def test_degraded_when_the_catalogue_is_empty(tmp_path: Path) -> None:
    """datasets == 0, even with lexical and dense rows left behind and a matching snapshot."""
    path = tmp_path / "catalog.sqlite"
    _catalogue(path)
    conn = sqlite3.connect(path)  # foreign keys OFF: the vectors survive the delete
    conn.execute("DELETE FROM datasets")
    conn.commit()
    conn.close()

    facts = app_module.health(_settings(path), dense_info=(3, V3))

    assert facts["datasets"] == 0
    assert facts["lexical_index"] == 3 and facts["dense_index"] == 3
    assert facts["status"] == "degraded"


def test_degraded_when_the_lexical_index_is_missing(tmp_path: Path) -> None:
    """lexical == 0: half the hybrid retriever is dead."""
    path = tmp_path / "catalog.sqlite"
    _catalogue(path, fts=False)

    facts = app_module.health(_settings(path), dense_info=(3, V3))

    assert facts["lexical_index"] == 0
    assert facts["status"] == "degraded"


def test_degraded_when_the_dense_table_is_empty(tmp_path: Path) -> None:
    """dense_index == 0, even when the (empty) snapshot matches the (empty) table."""
    path = tmp_path / "catalog.sqlite"
    _catalogue(path, vectors={})

    facts = app_module.health(_settings(path), dense_info=(0, None))

    assert facts["dense_index"] == 0
    assert facts["status"] == "degraded"


def test_degraded_when_the_dense_table_is_partial(tmp_path: Path) -> None:
    """dense_index < datasets: some datasets are invisible to the dense leg."""
    path = tmp_path / "catalog.sqlite"
    _catalogue(path, vectors={IDS[0]: V1, IDS[1]: V2})

    facts = app_module.health(_settings(path), dense_info=(2, V2))

    assert facts["dense_index"] == 2 and facts["datasets"] == 3
    assert facts["status"] == "degraded"


def test_degraded_when_the_loaded_snapshot_is_empty(full: Settings) -> None:
    """dense_loaded == 0: the server started before the table was filled."""
    facts = app_module.health(full, dense_info=(0, None))

    assert facts["dense_loaded"] == 0
    assert facts["dense_loaded_version"] is None
    assert facts["status"] == "degraded"


def test_degraded_when_the_loaded_count_differs(full: Settings) -> None:
    """A snapshot from before a re-index that added rows: restart needed."""
    facts = app_module.health(full, dense_info=(2, V3))

    assert facts["dense_loaded"] == 2
    assert facts["status"] == "degraded"


def test_degraded_when_the_same_count_has_a_different_version(full: Settings) -> None:
    """An in-place re-index keeps the count and moves the version; count alone would miss it."""
    facts = app_module.health(full, dense_info=(3, V1))

    assert facts["dense_loaded"] == 3 and facts["dense_index"] == 3
    assert facts["dense_loaded_version"] == V1 and facts["dense_version"] == V3
    assert facts["status"] == "degraded"


def test_health_on_a_catalogue_without_the_dense_table_does_not_raise(tmp_path: Path) -> None:
    """A pre-migration catalogue reads as an empty dense index, degraded, never an error."""
    path = tmp_path / "catalog.sqlite"
    _catalogue(path, dense_table=False)

    facts = app_module.health(_settings(path))

    assert facts["dense_index"] == 0
    assert facts["dense_version"] is None
    assert facts["status"] == "degraded"


def test_health_on_a_missing_catalogue_does_not_create_it(tmp_path: Path) -> None:
    """Every read is read-only: probing health must not manufacture an empty catalogue."""
    path = tmp_path / "absent.sqlite"

    facts = app_module.health(_settings(path))

    assert facts["datasets"] == 0 and facts["dense_index"] == 0
    assert facts["status"] == "degraded"
    assert not path.exists()


# --- /healthz: the route reads the pipeline off app.state ------------------------------


class _LoadedPipeline:
    """Stands in for a Pipeline whose snapshot is loaded."""

    def __init__(self, info: tuple[int, str | None] | None) -> None:
        """Report ``info`` from ``dense_info()``."""
        self.info = info

    def dense_info(self) -> tuple[int, str | None] | None:
        """Return the canned snapshot facts."""
        return self.info


def test_healthz_without_a_pipeline_reports_dense_loaded_none(full: Settings) -> None:
    """Route tests never run the lifespan, so ``app.state.pipeline`` may not exist at all."""
    client, _ = build(catalog_db_path=full.catalog_db_path, embedding_model=MODEL)

    response = client.get("/healthz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["dense_index"] == 3 and payload["dense_version"] == V3
    assert payload["dense_loaded"] is None and payload["dense_loaded_version"] is None
    assert payload["status"] == "ok"


@pytest.mark.parametrize(
    ("info", "status"),
    [((3, V3), "ok"), ((3, V1), "degraded"), ((2, V3), "degraded"), ((0, None), "degraded")],
)
def test_healthz_compares_the_running_snapshot_with_the_table(
    full: Settings, info: tuple[int, str | None], status: str
) -> None:
    """``/healthz`` passes ``app.state.pipeline.dense_info()`` into ``health``."""
    client, _ = build(catalog_db_path=full.catalog_db_path, embedding_model=MODEL)
    client.app.state.pipeline = _LoadedPipeline(info)  # type: ignore[attr-defined]

    payload = client.get("/healthz").json()

    assert payload["dense_loaded"] == info[0]
    assert payload["dense_loaded_version"] == info[1]
    assert payload["status"] == status


def test_healthz_never_loads_the_snapshot(full: Settings) -> None:
    """A real but unloaded Pipeline: ``dense_loaded`` is None and the loader is never called."""
    calls: list[int] = []

    def loader() -> Any:
        """Fail loudly if a health probe triggers the load."""
        calls.append(1)
        raise AssertionError("a health probe loaded the dense snapshot")

    client, _ = build(catalog_db_path=full.catalog_db_path, embedding_model=MODEL)
    client.app.state.pipeline = Pipeline(full, dense_loader=loader)  # type: ignore[attr-defined]

    payload = client.get("/healthz").json()

    assert calls == []
    assert payload["dense_loaded"] is None
    assert payload["status"] == "ok"


# --- startup: the real lifespan over a catalogue without the dense table (P3) ------------


@pytest.fixture
def started_without_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Path]]:
    """Run the REAL lifespan (Pipeline.create + warm) with only the heavy loaders faked.

    The embedding model and the LLM are stand-ins; the dense loader is whatever
    ``Pipeline.create`` wires, run for real against a catalogue that predates the table.
    """
    path = tmp_path / "catalog.sqlite"
    _catalogue(path, dense_table=False)
    settings = _settings(path)
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr("pythia.retrieval.embed.load_model", lambda *_a, **_k: "model")
    monkeypatch.setattr("pythia.llm.load_llm", lambda *_a, **_k: FakeLLM({}))
    monkeypatch.setattr("pythia.net.use_system_trust_store", lambda: None)
    monkeypatch.setattr("pythia.logging_setup.configure_logging", lambda *_a, **_k: None)
    with TestClient(app_module.create_app()) as client:
        yield client, path


def test_an_app_without_the_dense_table_starts_and_serves_healthz(
    started_without_table: tuple[TestClient, Path],
) -> None:
    """Startup must not depend on the table existing: it boots, loads empty, reports degraded."""
    client, path = started_without_table

    response = client.get("/healthz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["dense_index"] == 0
    assert payload["dense_loaded"] == 0, "warm() loads the (empty) snapshot at startup"

    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "dataset_embeddings" not in tables, "a read-only load must not create the table"
