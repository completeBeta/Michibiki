"""Tests for Mihon .tachibk backup parser."""

import gzip
import pytest
from pathlib import Path

from src.backup_parser import (
    parse_backup,
    _map_status,
    MangaEntry,
    BackupParseResult,
    ANILIST_SYNC_ID,
)
from src.mihon_backup_pb2 import Backup, BackupManga, BackupChapter, BackupTracking


def _make_minimal_backup(tmp_path: Path, manga_count: int = 1) -> Path:
    """Create a minimal valid .tachibk file for testing.

    Args:
        tmp_path: Pytest tmp_path fixture directory.
        manga_count: Number of manga entries to include.

    Returns:
        Path to the created .tachibk file.
    """
    backup = Backup()
    backup.version = 42

    for i in range(manga_count):
        manga = backup.backupManga.add()
        manga.source = 100 + i
        manga.url = f"https://example.com/manga/{i}"
        manga.title = f"Test Manga {i}"
        manga.status = 1  # ONGOING

        # Add 3 chapters, last one read
        for ch_num in [1.0, 2.0, 3.0]:
            chapter = manga.chapters.add()
            chapter.url = f"https://example.com/manga/{i}/ch/{int(ch_num)}"
            chapter.name = f"Chapter {int(ch_num)}"
            chapter.chapterNumber = ch_num
            chapter.read = (ch_num == 3.0)

    filepath = tmp_path / "test_backup.tachibk"
    with gzip.open(filepath, "wb") as f:
        f.write(backup.SerializeToString())
    return filepath


def _make_backup_with_tracker(tmp_path: Path, anilist_id: int) -> Path:
    """Create a .tachibk with an AniList tracker binding."""
    backup = Backup()
    backup.version = 42

    manga = backup.backupManga.add()
    manga.source = 1
    manga.url = "https://example.com/manga/tracked"
    manga.title = "Tracked Manga"
    manga.status = 1

    chapter = manga.chapters.add()
    chapter.url = "https://example.com/manga/tracked/ch/1"
    chapter.name = "Chapter 1"
    chapter.chapterNumber = 1.0
    chapter.read = True

    track = manga.tracking.add()
    track.syncId = ANILIST_SYNC_ID
    track.mediaId = anilist_id
    track.lastChapterRead = 42.0

    filepath = tmp_path / "test_backup_tracked.tachibk"
    with gzip.open(filepath, "wb") as f:
        f.write(backup.SerializeToString())
    return filepath


class TestMapStatus:
    def test_known_statuses(self):
        assert _map_status(0) == "UNKNOWN"
        assert _map_status(1) == "ONGOING"
        assert _map_status(2) == "COMPLETED"
        assert _map_status(3) == "LICENSED"
        assert _map_status(4) == "PUBLISHING_FINISHED"
        assert _map_status(5) == "CANCELLED"
        assert _map_status(6) == "ON_HIATUS"

    def test_unknown_status_defaults(self):
        assert _map_status(99) == "UNKNOWN"
        assert _map_status(-1) == "UNKNOWN"


class TestParseBackup:
    def test_missing_file_raises(self, tmp_path):
        """parse_backup raises FileNotFoundError for nonexistent file."""
        missing = tmp_path / "does_not_exist.tachibk"
        with pytest.raises(FileNotFoundError, match="Backup file not found"):
            parse_backup(missing)

    def test_parse_empty_backup(self, tmp_path):
        """Backup with zero manga returns empty result."""
        backup = Backup()
        backup.version = 1
        filepath = tmp_path / "empty.tachibk"
        with gzip.open(filepath, "wb") as f:
            f.write(backup.SerializeToString())

        result = parse_backup(filepath)
        assert isinstance(result, BackupParseResult)
        assert result.total_manga == 0
        assert len(result.entries) == 0
        assert result.backup_version == 1

    def test_parse_single_manga(self, tmp_path):
        filepath = _make_minimal_backup(tmp_path, manga_count=1)
        result = parse_backup(filepath)

        assert result.total_manga == 1
        assert result.total_without_trackers == 1
        assert result.total_with_trackers == 0
        assert result.backup_version == 42

        entry = result.entries[0]
        assert entry.title == "Test Manga 0"
        assert entry.source_id == 100
        assert entry.status == "ONGOING"
        assert entry.anilist_media_id is None
        assert entry.last_chapter_read == 3.0
        assert entry.total_chapters == 3

    def test_parse_multiple_manga(self, tmp_path):
        filepath = _make_minimal_backup(tmp_path, manga_count=3)
        result = parse_backup(filepath)

        assert result.total_manga == 3
        assert result.total_without_trackers == 3
        assert len(result.entries) == 3
        assert {e.title for e in result.entries} == {
            "Test Manga 0",
            "Test Manga 1",
            "Test Manga 2",
        }

    def test_extracts_anilist_tracker_binding(self, tmp_path):
        filepath = _make_backup_with_tracker(tmp_path, anilist_id=96798)
        result = parse_backup(filepath)

        assert result.total_manga == 1
        assert result.total_with_trackers == 1
        assert result.total_without_trackers == 0
        assert result.entries[0].anilist_media_id == 96798
        assert result.entries[0].last_chapter_read == 42.0

    def test_volume_based_detection(self, tmp_path):
        """Manga with volume-based progress (reading_units * 10000) gets detected."""
        backup = Backup()
        backup.version = 42
        manga = backup.backupManga.add()
        manga.source = 1
        manga.url = "https://example.com/volume-manga"
        manga.title = "Volume Based Series"
        manga.status = 1

        # Volume 5 = 5/10000 = 0.0005
        chapter = manga.chapters.add()
        chapter.url = "https://example.com/ch"
        chapter.name = "Vol. 5"
        chapter.chapterNumber = 0.0005
        chapter.read = True

        filepath = tmp_path / "volume.tachibk"
        with gzip.open(filepath, "wb") as f:
            f.write(backup.SerializeToString())

        result = parse_backup(filepath)
        entry = result.entries[0]
        assert entry.is_volume_based is True
        assert entry.last_chapter_read == 5.0

    def test_chapter_based_not_mistaken_for_volume(self, tmp_path):
        """Normal chapter numbers (e.g., 1.0, 5.5) are not flagged as volume-based."""
        backup = Backup()
        backup.version = 42
        manga = backup.backupManga.add()
        manga.source = 1
        manga.url = "https://example.com/normal"
        manga.title = "Normal Series"
        manga.status = 1

        chapter = manga.chapters.add()
        chapter.url = "https://example.com/ch"
        chapter.name = "Chapter 5.5"
        chapter.chapterNumber = 5.5
        chapter.read = True

        filepath = tmp_path / "normal.tachibk"
        with gzip.open(filepath, "wb") as f:
            f.write(backup.SerializeToString())

        result = parse_backup(filepath)
        entry = result.entries[0]
        assert entry.is_volume_based is False
        assert entry.last_chapter_read == 5.5
