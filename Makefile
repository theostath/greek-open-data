.PHONY: setup probe harvest index eval fetch cache-purge answer dev check

RESOURCE_ID ?=
QUESTION ?=

setup:  ## uv sync (pre-commit hooks added in a later phase)
	uv sync

probe:  ## run the Phase 1 API discovery probe -> docs/api_probe_raw.md
	uv run python -m pythia.ingest.client_probe

harvest:  ## Phase 2: harvest catalog metadata -> data/catalog.sqlite
	uv run python -m pythia.ingest.harvest

index:  ## Phase 3: build dense (Chroma) + lexical (FTS5) indexes
	uv run python -m pythia.retrieval.index

eval:  ## Phase 3: run the golden-question RETRIEVAL eval
	uv run python -m pythia.eval.run_eval

fetch:  ## Phase 5: fetch one resource -> typed table (make fetch RESOURCE_ID=<id>)
	uv run python -m pythia.access.data_client --resource-id $(RESOURCE_ID)

cache-purge:  ## Phase 5: drop cache rows past the TTL ceiling
	uv run python -c "from config import get_settings; from pythia.access.cache import connect_cache, init_cache_db, purge_expired; c=get_settings(); conn=connect_cache(c.cache_db_path); init_cache_db(conn); print('purged', purge_expired(conn, ttl_s=c.access_cache_ttl_s)); conn.commit()"

answer:  ## Phase 6: answer one question (make answer QUESTION="..." [RESOURCE_ID=<id>])
	uv run python -m pythia.synthesis.answer --question "$(QUESTION)" $(if $(RESOURCE_ID),--resource-id $(RESOURCE_ID),)

dev:  ## Phase 7
	@echo "dev: not implemented until Phase 7"

check:  ## ruff + mypy + pytest
	uv run ruff check .
	uv run mypy
	# Offline, exactly as CI runs it. test_embed.py and test_search.py load real
	# e5-small weights; without these the loader makes a live HEAD request to
	# huggingface.co per module, which fails intermittently and errors 7-10 tests
	# at random. The weights are already cached, so nothing is actually fetched.
	HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uv run pytest -q
