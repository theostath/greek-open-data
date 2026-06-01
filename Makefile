.PHONY: setup probe harvest index eval dev check

setup:  ## uv sync (pre-commit hooks added in a later phase)
	uv sync

probe:  ## run the Phase 1 API discovery probe -> docs/api_probe_raw.md
	uv run python -m pythia.ingest.client_probe

harvest:  ## Phase 2: harvest catalog metadata -> data/catalog.sqlite
	uv run python -m pythia.ingest.harvest

index:  ## Phase 3
	@echo "index: not implemented until Phase 3"

eval:  ## Phase 3+
	@echo "eval: not implemented until Phase 3"

dev:  ## Phase 7
	@echo "dev: not implemented until Phase 7"

check:  ## ruff + mypy + pytest
	uv run ruff check .
	uv run mypy
	uv run pytest -q
