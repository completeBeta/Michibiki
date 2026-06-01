"""Sync engine — orchestrates Suwayomi→AniList progress sync."""

import logging

from src.anilist import AniListClient
from src.state import StateStore
from src.suwayomi import SuwayomiClient

logger = logging.getLogger(__name__)


class SyncEngine:
    def __init__(
        self,
        suwayomi: SuwayomiClient,
        anilist: AniListClient,
        state: StateStore,
        dry_run: bool = False,
    ):
        self.suwayomi = suwayomi
        self.anilist = anilist
        self.state = state
        self.dry_run = dry_run

    async def run(self) -> dict:
        """Run a full sync cycle. Returns stats dict."""
        stats: dict[str, int] = {
            "checked": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
        }

        logger.info("Fetching manga progress from Suwayomi...")
        manga_list = await self.suwayomi.fetch_manga_progress()
        stats["checked"] = len(manga_list)

        for manga in manga_list:
            if manga.anilist_media_id is None:
                continue

            try:
                progress = manga.last_chapter_read or manga.highest_read_chapter

                if not self.state.needs_update(manga.anilist_media_id, progress):
                    stats["skipped"] += 1
                    continue

                status = AniListClient.map_status(manga.status)

                prefix = "[DRY RUN] " if self.dry_run else ""
                logger.info(
                    "%sUpdating '%s' → Ch %d (%s)",
                    prefix,
                    manga.title,
                    AniListClient.round_progress(progress),
                    status.value,
                )

                await self.anilist.update_progress(
                    media_id=manga.anilist_media_id,
                    progress=progress,
                    status=status,
                    dry_run=self.dry_run,
                )

                self.state.mark_synced(
                    anilist_id=manga.anilist_media_id,
                    suwayomi_id=manga.manga_id,
                    title=manga.title,
                    synced_chapter=progress,
                )
                stats["updated"] += 1

            except Exception:
                logger.error(
                    "Failed to sync '%s' (mediaId=%d)",
                    manga.title,
                    manga.anilist_media_id,
                    exc_info=True,
                )
                stats["errors"] += 1

        logger.info("Sync complete: %s", stats)
        return stats
