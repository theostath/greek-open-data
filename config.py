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
    # RAGAS dev-eval only (ADR-0003/0004); never read on the planning/synthesis path.
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

    # Planning (Phase 4, ADR-0004/0005). LLM = local Qwen via Ollama's native /api/chat
    # (needed to send think:false — see pythia/llm.py); no API key, no network egress.
    # Model id lives here, never inline. A legacy ".../v1" base URL is still accepted.
    llm_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen3.5:9b"
    # Ceiling, not a target: ~10s warm for a 9B on this CPU, but a cold load of the
    # ~6GB model adds to the first call. Do not retry timeouts.
    llm_timeout_s: float = 120.0
    llm_temperature: float = 0.0  # deterministic extraction
    llm_max_tokens: int = 512
    # Grounded-or-silent: LLM relevance gate is primary; this score floor is the
    # degraded-mode fallback (min confidence, 0..1) when the LLM is unavailable.
    planning_score_threshold: float = 0.15
    planning_limit_max: int = 32000  # DataStore hard cap; clamp any extracted limit to it
    # Opt-in, eval-gated LLM disambiguation over the top-N shortlist (ADR-0002 precedent).
    planning_llm_disambiguate: bool = False
    planning_disambiguate_pool: int = 5
    # Minimum question length (chars, stripped) below which we decline without an LLM call.
    planning_min_question_chars: int = 3

    # Access (Phase 5, ADR-0006). Treat every fetch as untrusted third-party content:
    # 75% of fetchable CSV/JSON resources are NOT hosted by data.gov.gr.
    cache_db_path: str = "data/cache.sqlite"
    # httpx's read timeout is PER-CHUNK, so it cannot bound a slow drip; access_deadline_s
    # is the real wall-clock budget across redirects, DataStore pages and retries.
    access_read_timeout_s: float = 30.0
    access_connect_timeout_s: float = 8.0
    access_deadline_s: float = 90.0
    # Abort a transfer sustaining less than this — the only defence against a server that
    # dribbles bytes slowly enough to never trip the per-chunk read timeout.
    access_min_throughput_bps: int = 10_000
    access_max_rows: int = 50_000
    # Load-bearing, not headroom: the largest declared resource is ~174 MiB.
    access_max_bytes: int = 25_000_000
    access_datastore_page_limit: int = 32_000  # measured ceiling; larger is silently clamped
    access_retry_attempts: int = 4
    access_max_redirects: int = 3
    # Ceiling on EVERY cache row: metadata_modified is catalog-level and does not move when
    # a file is replaced in place, so without this 74% of resources would cache forever.
    access_cache_ttl_s: int = 2_592_000  # 30 days
    access_cache_max_bytes: int = 2_000_000_000
    # Politeness toward ~51 small municipal GIS hosts; the portal blocks crawlers.
    access_host_min_interval_s: float = 1.0
    access_allow_http: bool = False  # 109 resources are plain http:// — opt in explicitly
    access_allow_off_portal: bool = True


def get_settings() -> Settings:
    """Return application settings loaded from the environment."""
    return Settings()
