"""Tests for AniList GraphQL client."""

import pytest
from src.anilist import AniListClient, MediaListStatus


@pytest.fixture
def client():
    return AniListClient("test_token")


def test_build_save_mutation_payload(client):
    """Mutation payload has correct structure."""
    payload = client._build_save_mutation(
        media_id=21,
        progress=1105.0,
        status=MediaListStatus.CURRENT,
    )
    assert payload["variables"]["mediaId"] == 21
    assert payload["variables"]["progress"] == 1105
    assert payload["variables"]["status"] == "CURRENT"
    assert "SaveMediaListEntry" in payload["query"]


def test_round_progress_floors_float():
    """Chapter numbers like 42.5 should floor to 42 (haven't finished 42.5)."""
    assert AniListClient.round_progress(1105.0) == 1105
    assert AniListClient.round_progress(42.5) == 42
    assert AniListClient.round_progress(0.0) == 0
    assert AniListClient.round_progress(None) == 0


def test_status_mapping():
    assert AniListClient.map_status("ONGOING") == MediaListStatus.CURRENT
    assert AniListClient.map_status("COMPLETED") == MediaListStatus.COMPLETED
    assert AniListClient.map_status("PUBLISHING_FINISHED") == MediaListStatus.CURRENT
    assert AniListClient.map_status("UNKNOWN") == MediaListStatus.CURRENT
    assert AniListClient.map_status("CANCELLED") == MediaListStatus.CURRENT


def test_dry_run_skips_http(client):
    """update_progress in dry_run mode returns a preview dict."""
    result = client._build_dry_run_result(
        media_id=21,
        progress=42.0,
        status=MediaListStatus.CURRENT,
    )
    assert result["dry_run"] is True
    assert result["mediaId"] == 21
    assert result["progress"] == 42
    assert result["status"] == "CURRENT"
