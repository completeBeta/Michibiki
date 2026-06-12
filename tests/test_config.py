"""Tests for config module."""

import pytest
from src.config import Config, load_config


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("SUWAYOMI_URL", "http://suwayomi:4567/api/graphql")
    monkeypatch.setenv("ANILIST_TOKEN", "test_token_123")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "86400")
    monkeypatch.setenv("DRY_RUN", "false")

    config = load_config()

    assert config.suwayomi_url == "http://suwayomi:4567/api/graphql"
    assert config.anilist_token == "test_token_123"
    assert config.poll_interval_seconds == 86400
    assert config.dry_run is False


def test_load_config_defaults(monkeypatch):
    monkeypatch.setenv("SUWAYOMI_URL", "http://localhost:4567/api/graphql")
    monkeypatch.setenv("ANILIST_TOKEN", "token")

    config = load_config()

    assert config.poll_interval_seconds == 43200
    assert config.dry_run is False


def test_missing_anilist_token_raises(monkeypatch):
    monkeypatch.setenv("SUWAYOMI_URL", "http://localhost:4567/api/graphql")
    monkeypatch.delenv("ANILIST_TOKEN", raising=False)

    with pytest.raises(ValueError, match="ANILIST_TOKEN"):
        load_config()


def test_missing_suwayomi_url_raises(monkeypatch):
    monkeypatch.setenv("ANILIST_TOKEN", "token")
    monkeypatch.delenv("SUWAYOMI_URL", raising=False)

    with pytest.raises(ValueError, match="SUWAYOMI_URL"):
        load_config()
