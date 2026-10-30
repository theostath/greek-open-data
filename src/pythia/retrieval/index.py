"""Build the retrieval indexes (exact dense vectors + SQLite FTS5 lexical) from the catalog."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from config import get_settings

from pythia.ingest.db import connect, init_db
from pythia.logging_setup import configure_logging, get_logger, log_event
from pythia.retrieval.dense import build_dense_index, count_vectors, ensure_dense_schema
from pythia.retrieval.embed import load_model
from pythia.retrieval.lexical import build_fts_index

LOGGER_NAME = "pythia.retrieval.index"

# Legacy Chroma store. While it exists, a full build is refused (see ``main``); the guard
# goes away with scripts/migrate_chroma_vectors.py.
CHROMA_DIR: Path = Path("data/chroma")


def main(argv: Sequence[str] | None = None) -> int:
    """Build/refresh the dense and lexical indexes over the harvested catalog."""
    parser = argparse.ArgumentParser(description="Build the dense and lexical indexes.")
    parser.add_argument(
        "--force", action="store_true",
        help="allow a full re-embed even though data/chroma still holds vectors to migrate",
    )
    args = parser.parse_args(argv)

    configure_logging()
    logger = get_logger(LOGGER_NAME)
    settings = get_settings()

    existing, _ = count_vectors(settings.catalog_db_path, settings.embedding_model)
    if existing == 0 and Path(CHROMA_DIR).exists() and not args.force:
        log_event(
            logger, logging.ERROR, "index.refused",
            reason="no stored vectors for the model but the legacy Chroma store exists; "
            "run scripts/migrate_chroma_vectors.py to import them, or pass --force "
            "to re-embed everything (~98 CPU-minutes)",
            chroma=str(CHROMA_DIR),
        )
        return 2

    conn = connect(settings.catalog_db_path)
    init_db(conn)
    ensure_dense_schema(conn)
    log_event(logger, logging.INFO, "index.start", model=settings.embedding_model)
    model = load_model(settings.embedding_model)
    dense = build_dense_index(conn, model, model_name=settings.embedding_model)
    lexical = build_fts_index(conn)
    conn.commit()
    conn.close()
    log_event(logger, logging.INFO, "index.done", dense=dense, lexical=lexical)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
