"""Auto-cleanup of downloaded manga chapters older than 14 days."""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger("michibiki.cleanup")

DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "/downloads")
CLEANUP_AGE_DAYS = int(os.getenv("CLEANUP_AGE_DAYS", "14"))


def delete_chapter_dir(chapter_dir: Path) -> bool:
    """Delete a single chapter directory. Returns True on success."""
    if not chapter_dir.exists() or not chapter_dir.is_dir():
        return False
    try:
        shutil.rmtree(chapter_dir)
        log.info("Cleaned up chapter: %s", chapter_dir)
        return True
    except Exception as e:
        log.warning("Failed to delete %s: %s", chapter_dir, e)
        return False


def delete_manga_downloads(manga_dir: Path, chapter_names: list[str] | None = None) -> int:
    """Delete downloaded chapter directories for a manga.

    If chapter_names is None, delete ALL chapter dirs in the manga dir.
    Returns number of deleted chapters.
    """
    if not manga_dir.exists() or not manga_dir.is_dir():
        return 0

    deleted = 0
    for entry in sorted(manga_dir.iterdir()):
        if not entry.is_dir():
            continue
        if chapter_names is not None:
            # Case-insensitive name match
            match = any(
                cn.lower() == entry.name.lower()
                or cn.lower() in entry.name.lower()
                for cn in chapter_names
            )
            if not match:
                continue
        if delete_chapter_dir(entry):
            deleted += 1

    # If manga dir is now empty, remove it too
    try:
        remaining = list(manga_dir.iterdir())
        if not remaining:
            manga_dir.rmdir()
            log.info("Removed empty manga dir: %s", manga_dir)
    except Exception:
        pass

    return deleted


async def run_cleanup_daily() -> None:
    """Background task: scan download dirs daily, delete chapters older than CLEANUP_AGE_DAYS."""
    cutoffs_by_dir: dict[Path, float] = {}

    while True:
        now = time.time()
        cutoff = now - (CLEANUP_AGE_DAYS * 86400)
        total_deleted = 0

        base = Path(DOWNLOADS_DIR)
        if not base.exists():
            log.warning("Downloads dir %s does not exist, skipping cleanup", DOWNLOADS_DIR)
            await asyncio.sleep(86400)
            continue

        # Iterate through /downloads/mangas/{source}/{title}/{chapter}/
        mangas_dir = base / "mangas"
        if mangas_dir.is_dir():
            for source_dir in mangas_dir.iterdir():
                if not source_dir.is_dir():
                    continue
                for manga_dir in source_dir.iterdir():
                    if not manga_dir.is_dir():
                        continue
                    for chapter_dir in sorted(manga_dir.iterdir()):
                        if not chapter_dir.is_dir():
                            continue
                        try:
                            mtime = chapter_dir.stat().st_mtime
                        except OSError:
                            continue
                        if mtime < cutoff:
                            if delete_chapter_dir(chapter_dir):
                                total_deleted += 1
                    # Clean up empty manga dirs
                    try:
                        remaining = list(manga_dir.iterdir())
                        if not remaining:
                            manga_dir.rmdir()
                    except Exception:
                        pass

        # Also check top-level source dirs (some extensions store there)
        for top in base.iterdir():
            if not top.is_dir() or top.name == "mangas" or top.name == "thumbnails":
                continue
            for manga_dir in top.iterdir():
                if not manga_dir.is_dir():
                    continue
                for chapter_dir in sorted(manga_dir.iterdir()):
                    if not chapter_dir.is_dir():
                        continue
                    try:
                        mtime = chapter_dir.stat().st_mtime
                    except OSError:
                        continue
                    if mtime < cutoff:
                        if delete_chapter_dir(chapter_dir):
                            total_deleted += 1
                try:
                    remaining = list(manga_dir.iterdir())
                    if not remaining:
                        manga_dir.rmdir()
                except Exception:
                    pass

        if total_deleted > 0:
            log.info(
                "Cleanup: removed %d chapter(s) older than %d days",
                total_deleted, CLEANUP_AGE_DAYS,
            )

        await asyncio.sleep(86400)  # Sleep 24 hours
