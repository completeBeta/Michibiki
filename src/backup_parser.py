"""Parse Mihon .tachibk backup files.

Reads gzipped protobuf backup files and extracts manga library entries
with their AniList tracker bindings and reading progress.
"""

import gzip
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .mihon_backup_pb2 import Backup  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

# AniList tracker syncId in Mihon backups
ANILIST_SYNC_ID = 2

# Status mapping: Mihon backup manga status → AniList-readable string
_STATUS_MAP: dict[int, str] = {
    0: "UNKNOWN",
    1: "ONGOING",
    2: "COMPLETED",
    3: "LICENSED",
    4: "PUBLISHING_FINISHED",
    5: "CANCELLED",
    6: "ON_HIATUS",
}


@dataclass
class MangaEntry:
    """A single manga extracted from a Mihon backup."""

    title: str
    source_id: int
    source_url: str
    anilist_media_id: int | None  # from tracker binding (syncId=2)
    last_chapter_read: float  # highest read chapter/volume number
    total_chapters: int  # total chapters in library
    status: str  # ONGOING, COMPLETED, etc.
    is_volume_based: bool = False  # True if last_chapter_read represents volumes, not chapters


@dataclass
class BackupParseResult:
    """Full result of parsing a Mihon backup."""

    entries: list[MangaEntry] = field(default_factory=list)
    total_manga: int = 0
    total_with_trackers: int = 0  # manga that already have AniList bindings
    total_without_trackers: int = 0  # manga needing AniList search
    backup_version: int = 0


def parse_backup(filepath: str | Path) -> BackupParseResult:
    """Parse a .tachibk backup file.

    Args:
        filepath: Path to the .tachibk file.

    Returns:
        BackupParseResult with all extracted manga entries and summary stats.
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Backup file not found: {filepath}")

    with gzip.open(path, "rb") as f:
        backup = Backup()
        backup.ParseFromString(f.read())

    entries: list[MangaEntry] = []
    with_trackers = 0
    without_trackers = 0

    for manga in backup.backupManga:
        entry = _extract_manga_entry(manga)
        entries.append(entry)
        if entry.anilist_media_id:
            with_trackers += 1
        else:
            without_trackers += 1

    log.info(
        "Parsed backup v%d: %d manga (%d with AniList trackers, %d without)",
        backup.version,
        len(entries),
        with_trackers,
        without_trackers,
    )

    return BackupParseResult(
        entries=entries,
        total_manga=len(entries),
        total_with_trackers=with_trackers,
        total_without_trackers=without_trackers,
        backup_version=backup.version,
    )


def _extract_manga_entry(manga) -> MangaEntry:
    """Extract a single MangaEntry from a protobuf BackupManga message."""
    # Extract AniList tracker info (syncId=2)
    anilist_id: int | None = None
    tracker_last_read: float = 0.0

    for track in manga.tracking:
        if track.syncId == ANILIST_SYNC_ID:
            # mediaId (proto 100) takes precedence, fallback to mediaIdInt (proto 3)
            media_id = track.mediaId or track.mediaIdInt
            if media_id:
                anilist_id = int(media_id)
            tracker_last_read = max(tracker_last_read, track.lastChapterRead)

    # If no tracker progress, count read chapters
    last_read = tracker_last_read
    if last_read == 0.0:
        read_chapters = [ch for ch in manga.chapters if ch.read]
        if read_chapters:
            last_read = float(max(ch.chapterNumber for ch in read_chapters))

    # Detect volume-based manga: Mihon stores volumes as volume/10000.
    # If all chapters have sub-1 numbers, scale to actual volume count.
    volume_based = False
    if last_read > 0 and last_read < 1.0:
        # Confirm it's volume-based by checking sample chapters
        sample = [ch.chapterNumber for ch in manga.chapters[:5]]
        if all(0 < n < 1 for n in sample if n > 0):
            original = last_read
            last_read = round(last_read * 10000)
            volume_based = True
            log.info("Volume-based manga '%s': scaled %.6f → %d", manga.title, original, last_read)

    return MangaEntry(
        title=manga.title,
        source_id=manga.source,
        source_url=manga.url,
        anilist_media_id=anilist_id,
        last_chapter_read=last_read,
        total_chapters=len(manga.chapters),
        status=_map_status(manga.status),
        is_volume_based=volume_based,
    )


def _map_status(status_int: int) -> str:
    """Map Mihon backup integer status to string."""
    return _STATUS_MAP.get(status_int, "UNKNOWN")
