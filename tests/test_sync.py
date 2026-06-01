"""Tests for sync engine."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.anilist import MediaListStatus
from src.state import StateStore
from src.suwayomi import MangaProgress
from src.sync import SyncEngine


@pytest.fixture
def mock_suwayomi():
    client = AsyncMock()
    client.fetch_manga_progress.return_value = [
        MangaProgress(
            manga_id=42,
            title="One Piece",
            status="ONGOING",
            anilist_media_id=21,
            last_chapter_read=1100.0,
            highest_read_chapter=1105.0,
            last_read_at="1717200000000",
        ),
        MangaProgress(
            manga_id=43,
            title="Finished Manga",
            status="COMPLETED",
            anilist_media_id=22,
            last_chapter_read=200.0,
            highest_read_chapter=200.0,
            last_read_at="1717200000000",
        ),
    ]
    return client


@pytest.fixture
def mock_anilist():
    return AsyncMock()


@pytest.fixture
def mock_state():
    store = MagicMock(spec=StateStore)
    store.needs_update.return_value = True
    return store


@pytest.mark.asyncio
async def test_sync_updates_all_when_changed(mock_suwayomi, mock_anilist, mock_state):
    engine = SyncEngine(mock_suwayomi, mock_anilist, mock_state, dry_run=True)
    stats = await engine.run()

    assert stats["checked"] == 2
    assert stats["updated"] == 2
    assert stats["skipped"] == 0
    assert mock_anilist.update_progress.call_count == 2


@pytest.mark.asyncio
async def test_sync_skips_unchanged(mock_suwayomi, mock_anilist, mock_state):
    mock_state.needs_update.side_effect = [False, True]

    engine = SyncEngine(mock_suwayomi, mock_anilist, mock_state, dry_run=True)
    stats = await engine.run()

    assert stats["updated"] == 1
    assert stats["skipped"] == 1


@pytest.mark.asyncio
async def test_sync_marks_state_after_push(mock_suwayomi, mock_anilist, mock_state):
    engine = SyncEngine(mock_suwayomi, mock_anilist, mock_state, dry_run=True)
    await engine.run()

    assert mock_state.mark_synced.call_count == 2
    # Check first call args
    call = mock_state.mark_synced.call_args_list[0]
    assert call.kwargs["anilist_id"] == 21
    assert call.kwargs["suwayomi_id"] == 42
    assert call.kwargs["title"] == "One Piece"


@pytest.mark.asyncio
async def test_dry_run_passes_flag_to_anilist(mock_suwayomi, mock_anilist, mock_state):
    engine = SyncEngine(mock_suwayomi, mock_anilist, mock_state, dry_run=True)
    await engine.run()

    for call in mock_anilist.update_progress.call_args_list:
        assert call.kwargs["dry_run"] is True


@pytest.mark.asyncio
async def test_errors_are_counted(mock_suwayomi, mock_anilist, mock_state):
    mock_anilist.update_progress.side_effect = Exception("API down")

    engine = SyncEngine(mock_suwayomi, mock_anilist, mock_state, dry_run=False)
    stats = await engine.run()

    assert stats["errors"] == 2
    assert stats["updated"] == 0


@pytest.mark.asyncio
async def test_falls_back_to_highest_read_chapter(mock_suwayomi, mock_anilist, mock_state):
    """When trackRecord.lastChapterRead is None, use chapters query result."""
    mock_suwayomi.fetch_manga_progress.return_value = [
        MangaProgress(
            manga_id=99,
            title="No Tracker Progress",
            status="ONGOING",
            anilist_media_id=1,
            last_chapter_read=None,
            highest_read_chapter=42.0,
            last_read_at="1",
        ),
    ]
    engine = SyncEngine(mock_suwayomi, mock_anilist, mock_state, dry_run=True)
    await engine.run()

    call = mock_anilist.update_progress.call_args
    assert call.kwargs["progress"] == 42.0
