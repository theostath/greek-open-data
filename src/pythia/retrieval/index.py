"""Build the retrieval indexes (Chroma dense + SQLite FTS5 lexical) from the catalog."""

from __future__ import annotations

import logging

from config import get_settings

from pythia.ingest.db import connect, init_db
from pythia.logging_setup import configure_logging, get_logger, log_event
from pythia.retrieval.embed import build_chroma_index, load_model
from pythia.retrieval.lexical import build_fts_index

LOGGER_NAME = "pythia.retrieval.index"


def main() -> int:
    """Build/refresh the dense and lexical indexes over the harvested catalog."""
    configure_logging()
    logger = get_logger(LOGGER_NAME)
    settings = get_settings()
    conn = connect(settings.catalog_db_path)
    init_db(conn)
    log_event(
        logger, logging.INFO, "index.start",
        model=settings.embedding_model, chroma=settings.chroma_path,
    )
    model = load_model(settings.embedding_model)
    dense = build_chroma_index(conn, model, chroma_path=settings.chroma_path)
    lexical = build_fts_index(conn)
    conn.commit()
    conn.close()
    log_event(logger, logging.INFO, "index.done", dense=dense, lexical=lexical)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
