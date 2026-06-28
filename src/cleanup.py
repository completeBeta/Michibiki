"""Auto-cleanup of downloaded manga chapters older than 14 days."""

import asyncio
import logging
import os
import shutil
import time
import zipfile
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


def convert_folder_to_cbz(chapter_dir: Path, output_dir: Path, chapter_name: str) -> Path | None:
    """Convert a chapter folder of images into a .cbz file.

    CBZ is just a zip file renamed to .cbz. Comic readers (Kavita, Mihon)
    recognize this as a chapter archive.

    Args:
        chapter_dir: Directory containing page images (.jpg, .png, .webp)
        output_dir: Where to save the .cbz file
        chapter_name: Clean name for the CBZ filename (without extension)

    Returns the path to the created .cbz file, or None on failure.
    """
    if not chapter_dir.is_dir():
        return None

    # Collect image files
    image_exts = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'}
    images = sorted(
        [p for p in chapter_dir.iterdir() if p.is_file() and p.suffix.lower() in image_exts],
        key=lambda p: p.name,
    )

    if not images:
        log.warning("No images found in %s, skipping CBZ conversion", chapter_dir)
        return None

    # Sanitize filename
    safe_name = "".join(c for c in chapter_name if c.isprintable()).strip()
    safe_name = safe_name.replace("/", "-").replace(":", " -")
    cbz_path = output_dir / f"{safe_name}.cbz"

    try:
        with zipfile.ZipFile(str(cbz_path), 'w', zipfile.ZIP_DEFLATED) as zf:
            for img in images:
                zf.write(str(img), img.name)
        log.info("Created CBZ: %s (%d pages)", cbz_path, len(images))
        return cbz_path
    except Exception as e:
        log.error("Failed to create CBZ %s: %s", cbz_path, e)
        # Clean up partial file
        try:
            cbz_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def convert_chapters_to_cbz(
    manga_dir: Path,
    chapter_ids: list[int],
    chapter_lookup: dict[int, dict],
) -> int:
    """Convert downloaded chapter folders to CBZ files.

    Args:
        manga_dir: The manga download directory (contains chapter subdirs)
        chapter_ids: List of Suwayomi chapter IDs that finished downloading
        chapter_lookup: Dict mapping chapter_id → {name, chapterNumber}

    Returns number of chapters successfully converted.
    """
    converted = 0

    for cid in chapter_ids:
        ch = chapter_lookup.get(cid)
        if not ch:
            continue

        ch_name = ch.get("name", f"Chapter {ch.get('chapterNumber', '?')}")
        ch_num = ch.get("chapterNumber") or 0

        # Find the chapter directory
        from src.webui import _find_chapter_dir
        chapter_dir = _find_chapter_dir(manga_dir, ch_name)
        if not chapter_dir:
            log.warning("Chapter dir not found for CBZ: %s / %s", manga_dir.name, ch_name)
            continue

        # Build clean CBZ name
        if ch_num == int(ch_num):
            cbz_name = f"Ch {int(ch_num):04d} - {ch_name}"
        else:
            cbz_name = f"Ch {float(ch_num):05.1f} - {ch_name}"

        # Convert to CBZ
        cbz_path = convert_folder_to_cbz(chapter_dir, manga_dir, cbz_name)
        if cbz_path:
            # Delete the original folder
            delete_chapter_dir(chapter_dir)
            converted += 1

    return converted


def delete_manga_downloads(manga_dir: Path, chapter_names: list[str] | None = None) -> int:
    """Delete downloaded chapter directories or CBZ files for a manga.

    If chapter_names is None, delete ALL chapter dirs/CBZs in the manga dir.
    Returns number of deleted items.
    """
    if not manga_dir.exists() or not manga_dir.is_dir():
        return 0

    deleted = 0
    for entry in sorted(manga_dir.iterdir()):
        if entry.is_dir():
            if chapter_names is not None:
                match = any(
                    cn.lower() == entry.name.lower()
                    or cn.lower() in entry.name.lower()
                    for cn in chapter_names
                )
                if not match:
                    continue
            if delete_chapter_dir(entry):
                deleted += 1
        elif entry.is_file() and entry.suffix.lower() == '.cbz':
            if chapter_names is not None:
                match = any(
                    cn.lower() in entry.name.lower()
                    for cn in chapter_names
                )
                if not match:
                    continue
            try:
                entry.unlink()
                log.info("Deleted CBZ: %s", entry)
                deleted += 1
            except Exception as e:
                log.warning("Failed to delete CBZ %s: %s", entry, e)

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
                    # Also clean up old CBZ files
                    for cbz_file in sorted(manga_dir.glob("*.cbz")):
                        try:
                            mtime = cbz_file.stat().st_mtime
                        except OSError:
                            continue
                        if mtime < cutoff:
                            try:
                                cbz_file.unlink()
                                log.info("Cleaned up CBZ: %s", cbz_file)
                                total_deleted += 1
                            except Exception as e:
                                log.warning("Failed to delete CBZ %s: %s", cbz_file, e)
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
                # Also clean up CBZ files in top-level source dirs
                for cbz_file in sorted(manga_dir.glob("*.cbz")):
                    try:
                        mtime = cbz_file.stat().st_mtime
                    except OSError:
                        continue
                    if mtime < cutoff:
                        try:
                            cbz_file.unlink()
                            log.info("Cleaned up CBZ: %s", cbz_file)
                            total_deleted += 1
                        except Exception as e:
                            log.warning("Failed to delete CBZ %s: %s", cbz_file, e)
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
