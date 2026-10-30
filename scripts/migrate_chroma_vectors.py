"""One-time migration: copy the vectors out of the legacy Chroma store into catalog.sqlite.

Stop ``pythia-dev`` first: the import writes ~100 MB into the catalogue in one transaction.

Chromadb is no longer a project dependency, so overlay it for this run. Use the signed
interpreter (the WDAC policy blocks uv-managed Pythons)::

    uv run --python <signed interpreter> --with chromadb==1.5.9 \
        python scripts/migrate_chroma_vectors.py

The import refuses unless the catalogue holds no vectors for the configured model yet, and
it never touches ``data/chroma/``. Afterwards ``make index`` should log ``dense=0``.

Delete this script in the release after 0.2.0.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # config.py lives at the root

import chromadb  # noqa: E402
import numpy as np  # noqa: E402
from chromadb.config import Settings as ChromaSettings  # noqa: E402
from config import get_settings  # noqa: E402

from pythia.ingest.db import connect, init_db  # noqa: E402
from pythia.retrieval.dense import ensure_dense_schema, import_vectors  # noqa: E402
from pythia.retrieval.embed import embed_passages, load_model  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Read every vector and its signature from Chroma and import them into SQLite."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--chroma", default="data/chroma", help="Chroma persistence directory")
    parser.add_argument("--collection", default="datasets", help="Chroma collection name")
    args = parser.parse_args(argv)

    settings = get_settings()
    client = chromadb.PersistentClient(
        path=args.chroma, settings=ChromaSettings(anonymized_telemetry=False)
    )
    coll = client.get_collection(args.collection)
    found = coll.get(include=["embeddings", "metadatas"])
    ids = [str(i) for i in found["ids"]]
    embeddings = found.get("embeddings")
    metadatas = found.get("metadatas")
    if embeddings is None or metadatas is None or not ids:
        print(f"no vectors found in {args.chroma}/{args.collection}")
        return 1
    sigs = [str((meta or {}).get("sig", "")) for meta in metadatas]
    print(f"read {len(ids):,} vectors from {args.chroma}/{args.collection}")

    conn = connect(settings.catalog_db_path)
    init_db(conn)
    ensure_dense_schema(conn)
    model = load_model(settings.embedding_model)
    written = import_vectors(
        conn,
        ids,
        np.asarray(embeddings, dtype=np.float32),
        sigs,
        model_name=settings.embedding_model,
        embed_sample=lambda texts: embed_passages(model, texts),
    )
    conn.close()
    print(f"imported {written:,} vectors for {settings.embedding_model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
