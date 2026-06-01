"""Phase 2 catalog harvest: walk CKAN ``package_search`` into the SQLite store.

The page-fetching strategy is injected so the orchestration is testable without
network access; ``main`` wires the live, retrying httpx fetcher.
"""

from __future__ import annotations

import logging
import sqlite3
import ssl
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from config import get_settings
from tenacity import retry, stop_after_attempt, wait_exponential

from pythia.ingest.db import connect, count_datasets, init_db, upsert_dataset, upsert_resources
from pythia.ingest.normalize import normalize_package
from pythia.logging_setup import configure_logging, get_logger, log_event

LOGGER_NAME = "pythia.ingest.harvest"
PACKAGE_SEARCH_PATH = "/api/3/action/package_search"
DEFAULT_ROWS = 1000
USER_AGENT = "pythia-harvest/0.0 (data.gov.gr)"

# (start, rows) -> {"count": int, "results": [package, ...]}
PageFetcher = Callable[[int, int], dict[str, Any]]


@dataclass
class HarvestStats:
    """Counters for one harvest run."""

    seen: int = 0
    datasets: int = 0
    skipped: int = 0
    resources: int = 0


def iter_packages(fetch_page: PageFetcher, rows: int = DEFAULT_ROWS) -> Iterator[dict[str, Any]]:
    """Yield every CKAN package, paging ``package_search`` by start/rows until count."""
    first = fetch_page(0, rows)
    count = int(first.get("count", 0))
    yield from first.get("results", [])
    start = rows
    while start < count:
        page = fetch_page(start, rows)
        results = page.get("results", [])
        if not results:
            break
        yield from results
        start += rows


def harvest(
    conn: sqlite3.Connection,
    fetch_page: PageFetcher,
    *,
    rows: int = DEFAULT_ROWS,
    harvested_at: str,
) -> HarvestStats:
    """Harvest all packages into the catalog DB and return run statistics."""
    stats = HarvestStats()
    for raw in iter_packages(fetch_page, rows):
        stats.seen += 1
        normalized = normalize_package(raw, harvested_at)
        if normalized is None:
            stats.skipped += 1
            continue
        dataset, resources = normalized
        upsert_dataset(conn, dataset)
        upsert_resources(conn, resources)
        stats.datasets += 1
        stats.resources += len(resources)
    conn.commit()
    return stats


def _make_fetcher(client: httpx.Client, base_url: str) -> PageFetcher:
    """Build a retrying ``package_search`` page fetcher bound to an httpx client."""
    url = base_url.rstrip("/") + PACKAGE_SEARCH_PATH

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=2, max=30))
    def fetch_page(start: int, rows: int) -> dict[str, Any]:
        # Deterministic sort keeps deep offset pagination stable across requests.
        params: dict[str, str | int] = {
            "start": start,
            "rows": rows,
            "sort": "metadata_modified asc",
        }
        response = client.get(url, params=params)
        response.raise_for_status()
        result = response.json().get("result", {})
        return {
            "count": int(result.get("count", 0)),
            "results": result.get("results", []),
        }

    return fetch_page


def main() -> int:
    """Run a full live harvest into the configured catalog DB."""
    configure_logging()
    logger = get_logger(LOGGER_NAME)
    settings = get_settings()
    harvested_at = datetime.now(UTC).isoformat(timespec="seconds")
    ssl_context = ssl.create_default_context()

    conn = connect(settings.catalog_db_path)
    init_db(conn)
    log_event(logger, logging.INFO, "harvest.start", db=settings.catalog_db_path)

    with httpx.Client(
        timeout=httpx.Timeout(30.0, connect=8.0),
        headers={"User-Agent": USER_AGENT},
        verify=ssl_context,
        follow_redirects=True,
    ) as client:
        stats = harvest(conn, _make_fetcher(client, settings.data_gov_gr_base_url),
                        harvested_at=harvested_at)

    total = count_datasets(conn)
    conn.close()
    log_event(
        logger,
        logging.INFO,
        "harvest.done",
        seen=stats.seen,
        datasets=stats.datasets,
        skipped=stats.skipped,
        resources=stats.resources,
        total_in_db=total,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
