"""Tests for the catalog harvest orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pythia.ingest.db import connect, count_datasets, init_db
from pythia.ingest.harvest import harvest, iter_packages

FIXTURE = Path(__file__).parent / "fixtures" / "package_search_sample.json"
HARVESTED_AT = "2026-06-01T00:00:00+00:00"


def _load_packages() -> list[dict[str, Any]]:
    """Return the real packages from the saved package_search fixture."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = data["result"]["results"]
    return results


def test_iter_packages_walks_all_pages() -> None:
    """iter_packages pages with start/rows until the reported count is reached."""
    pages: dict[int, list[str]] = {0: ["a", "b"], 2: ["c", "d"], 4: ["e"]}

    def fetch_page(start: int, rows: int) -> dict[str, Any]:
        return {"count": 5, "results": pages.get(start, [])}

    assert list(iter_packages(fetch_page, rows=2)) == ["a", "b", "c", "d", "e"]


def test_harvest_writes_datasets_and_skips_non_datasets() -> None:
    """harvest persists datasets/resources and skips non-dataset packages."""
    packages = _load_packages()
    showcase = {"type": "showcase", "state": "active", "id": "sc1", "name": "sc"}
    all_pkgs = [*packages, showcase]

    def fetch_page(start: int, rows: int) -> dict[str, Any]:
        return {"count": len(all_pkgs), "results": all_pkgs if start == 0 else []}

    conn = connect(":memory:")
    init_db(conn)
    stats = harvest(conn, fetch_page, rows=1000, harvested_at=HARVESTED_AT)

    assert stats.datasets == len(packages)
    assert stats.skipped == 1
    assert count_datasets(conn) == len(packages)


def test_harvest_is_idempotent() -> None:
    """Re-running the harvest does not duplicate rows."""
    packages = _load_packages()

    def fetch_page(start: int, rows: int) -> dict[str, Any]:
        return {"count": len(packages), "results": packages if start == 0 else []}

    conn = connect(":memory:")
    init_db(conn)
    harvest(conn, fetch_page, rows=1000, harvested_at=HARVESTED_AT)
    harvest(conn, fetch_page, rows=1000, harvested_at=HARVESTED_AT)

    assert count_datasets(conn) == len(packages)
