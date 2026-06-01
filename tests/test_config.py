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
