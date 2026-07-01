"""Typed application settings, loaded from environment variables and ``.env``."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration read from environment variables and ``.env``.

    Field names map case-insensitively to env vars (e.g. ``data_gov_gr_token``
    reads ``DATA_GOV_GR_TOKEN``). Secrets default to ``None`` so the app can run
    without them in development; never log their values.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_gov_gr_token: str | None = None
    anthropic_api_key: str | None = None
    data_gov_gr_base_url: str = "https://data.gov.gr"
    catalog_db_path: str = "data/catalog.sqlite"

    # Retrieval (Phase 3). e5-large is the production model; tests pin e5-small for speed.
    embedding_model: str = "intfloat/multilingual-e5-large"
    chroma_path: str = "data/chroma"
    retrieval_top_k: int = 10

    # Reranking (Phase 3 follow-up, ADR-0002). Opt-in and eval-gated: when enabled the
    # cross-encoder reorders the top ``rerank_pool`` fused candidates down to top_k.
    rerank_enabled: bool = False
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_pool: int = 20


def get_settings() -> Settings:
    """Return application settings loaded from the environment."""
    return Settings()
