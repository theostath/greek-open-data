"""Tests for application settings."""

from __future__ import annotations

import pytest
from config import Settings


def test_defaults_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings load with safe defaults when no secrets are set."""
    monkeypatch.delenv("DATA_GOV_GR_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(_env_file=None)
    assert settings.data_gov_gr_token is None
    assert settings.anthropic_api_key is None
    assert settings.data_gov_gr_base_url == "https://data.gov.gr"


def test_reads_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bearer token is read from the environment."""
    monkeypatch.setenv("DATA_GOV_GR_TOKEN", "secret-value")
    settings = Settings(_env_file=None)
    assert settings.data_gov_gr_token == "secret-value"


def test_chroma_path_setting_is_removed() -> None:
    """The dense index lives in the catalogue now (ADR-0010); there is no Chroma path."""
    assert "chroma_path" not in Settings.model_fields
    assert not hasattr(Settings(_env_file=None), "chroma_path")


def test_a_leftover_chroma_path_env_var_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """``extra="ignore"`` keeps an old ``CHROMA_PATH`` in a ``.env`` or shell harmless."""
    monkeypatch.setenv("CHROMA_PATH", "data/chroma")
    settings = Settings(_env_file=None)
    assert not hasattr(settings, "chroma_path")
