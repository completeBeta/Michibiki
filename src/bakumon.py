"""Bakumon — Mihon Backup Monitor.

Orchestrates the full pipeline:
1. Watch for new Mihon .tachibk backup files
2. Parse backup → extract manga library + progress
3. Sync reading progress to AniList (direct from backup data)
4. Populate Suwayomi library (search sources, add manga, bind trackers)
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .anilist import AniListClient, MediaListStatus
from .anilist_search import search_anilist
from .backup_parser import BackupParseResult, parse_backup
from .config import Config

if TYPE_CHECKING:
    from .suwayomi_populator import SuwayomiPopulator

log = logging.getLogger(__name__)


async def sync_from_backup(
    backup_path: str,
    config: Config,
    *,
    populate_suwayomi: bool = True,
    clear_suwayomi_first: bool = False,
    dry_run: bool = False,
) -> dict:
    """Full sync pipeline from a Mihon backup file.

    Args:
        backup_path: Path to .tachibk file.
        config: Application config.
        populate_suwayomi: If True, add manga to Suwayomi + bind trackers.
        clear_suwayomi_first: If True, clear Suwayomi library before populating.
        dry_run: If True, log actions but don't make API calls.

    Returns:
        Dict with sync summary.
    """
    from .suwayomi_populator import SuwayomiPopulator

    # 1. Parse backup
    log.info("Parsing backup: %s", backup_path)
    result: BackupParseResult = parse_backup(backup_path)
    log.info(
        "Backup: %d manga, %d with trackers, %d without",
        result.total_manga,
        result.total_with_trackers,
        result.total_without_trackers,
    )

    if result.total_manga == 0:
        log.warning("No manga found in backup — nothing to sync")
        return {"synced": 0, "searched": 0, "populated": 0, "errors": []}

    # 2. Resolve AniList media IDs for unbound manga
    anilist = AniListClient(config.anilist_token)
    entries = result.entries

    searched = 0
    for entry in entries:
        if not entry.anilist_media_id:
            media_id = await search_anilist(entry.title, config.anilist_token)
            if media_id:
                entry.anilist_media_id = media_id
                searched += 1
                log.info("AniList match: '%s' → ID %d", entry.title, media_id)
            # AniList rate limit: 90 req/min. 1s delay = 60 req/min — safe.
            await asyncio.sleep(1)

    # 2.5 Deduplicate by AniList media ID.
    # When duplicates exist: keep the entry with the most chapters
    # (the main series, not a spinoff/viewpoint). Warn if ambiguous.
    seen: dict[int, list] = {}
    for entry in entries:
        if entry.anilist_media_id:
            seen.setdefault(entry.anilist_media_id, []).append(entry)
    deduped = []
    for mid, group in seen.items():
        if len(group) > 1:
            # Keep the entry with the most total chapters — that's the main series
            group.sort(key=lambda e: e.total_chapters, reverse=True)
            best = group[0]
            rest = group[1:]
            titles = [f"'{e.title}' ({e.total_chapters}ch, progress={e.last_chapter_read})" for e in group]
            log.warning(
                "Duplicate AniList ID %d: keeping '%s' (%dch) over %s",
                mid, best.title, best.total_chapters,
                ", ".join(f"'{t.title}'" for t in rest),
            )
            deduped.append(best)
        else:
            deduped.append(group[0])
    entries = deduped
    log.info("After dedup: %d unique AniList entries", len(entries))

    # 3. Sync to AniList (skip entries with no progress to avoid destructive 0-pushes)
    synced = 0
    errors: list[str] = []
    for entry in entries:
        if not entry.anilist_media_id:
            continue
        if entry.last_chapter_read <= 0:
            log.info("Skipping '%s': no chapters read (progress=0)", entry.title)
            continue

        if dry_run:
            units = "vol" if entry.is_volume_based else "ch"
            log.info(
                "[DRY RUN] Would sync '%s': mediaId=%d, progress=%d %s",
                entry.title,
                entry.anilist_media_id,
                anilist.round_progress(entry.last_chapter_read),
                units,
            )
        else:
            try:
                status = anilist.map_status(entry.status)
                await anilist.update_progress(
                    media_id=entry.anilist_media_id,
                    progress=entry.last_chapter_read,
                    status=status,
                    is_volume_based=entry.is_volume_based,
                )
                synced += 1
            except Exception as e:
                log.error("AniList sync failed for '%s': %s", entry.title, e)
                errors.append(f"{entry.title}: {e}")

    log.info("Synced %d manga to AniList", synced)

    # 4. Populate Suwayomi (optional)
    populated = 0
    if populate_suwayomi and not dry_run:
        populator = SuwayomiPopulator(config.suwayomi_url)

        if clear_suwayomi_first:
            log.info("Clearing Suwayomi library...")
            try:
                await populator.clear_library()
            except Exception as e:
                log.error("Failed to clear Suwayomi library: %s", e)

        log.info("Populating Suwayomi library...")
        pop_result = await populator.populate(entries)
        populated = pop_result.added
        errors.extend(pop_result.errors)

    return {
        "synced": synced,
        "searched": searched,
        "populated": populated,
        "errors": errors,
    }


def watch_for_backups(
    backup_dir: str,
    config: Config,
    *,
    poll_interval: int = 300,
    populate_suwayomi: bool = True,
    clear_suwayomi_first: bool = False,
    dry_run: bool = True,
):
    """Watch a directory for new .tachibk backup files and sync them.

    Runs synchronously (blocking loop). Intended as the main entrypoint
    for a long-running watcher process.

    Args:
        backup_dir: Directory to watch for .tachibk files.
        config: Application config.
        poll_interval: Seconds between directory scans.
        populate_suwayomi: If True, populate Suwayomi after sync.
        clear_suwayomi_first: If True, clear Suwayomi before populate.
        dry_run: If True, log actions but don't make API calls.
    """
    watch_path = Path(backup_dir)
    watch_path.mkdir(parents=True, exist_ok=True)

    log.info(
        "Bakumon watcher started: dir=%s, interval=%ds, dry_run=%s",
        watch_path,
        poll_interval,
        dry_run,
    )

    processed: set[str] = set()

    while True:
        try:
            files = sorted(
                watch_path.glob("*.tachibk"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            # Only process the single most recent backup — older ones are redundant
            unprocessed = [f for f in files if f.name not in processed]
            if unprocessed:
                files = [unprocessed[0]]  # just the newest

            for f in files:
                fname = f.name
                if fname not in processed:
                    log.info("New backup detected: %s", fname)
                    try:
                        asyncio.run(
                            sync_from_backup(
                                str(f),
                                config,
                                populate_suwayomi=populate_suwayomi,
                                clear_suwayomi_first=clear_suwayomi_first,
                                dry_run=dry_run,
                            )
                        )
                        processed.add(fname)
                        # Mark all older files as processed too — no need to revisit them
                        for older in unprocessed[1:]:
                            processed.add(older.name)
                        clear_suwayomi_first = False
                    except Exception as e:
                        log.error("Sync failed for %s: %s", fname, e)

            # Clean up old processed files (>30 days)
            for old in processed.copy():
                old_path = watch_path / old
                if not old_path.exists():
                    processed.discard(old)

        except Exception as e:
            log.error("Watcher error: %s", e)

        time.sleep(poll_interval)
