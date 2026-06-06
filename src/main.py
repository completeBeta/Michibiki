"""Michibiki — Daily Mihon→AniList reading progress sync.

Two modes:
- poll: Query Suwayomi for tracker progress → sync to AniList (legacy)
- watch: Watch for Mihon .tachibk backups → sync to AniList + populate Suwayomi
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import httpx

from src.config import load_config
from src.suwayomi import SuwayomiClient
from src.anilist import AniListClient
from src.state import StateStore
from src.sync import SyncEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("michibiki")


# ── Legacy mode: Suwayomi → AniList poll ──────────────────────────

async def run_sync(config):
    """Execute a single Suwayomi→AniList sync cycle."""
    suwayomi = SuwayomiClient(config.suwayomi_url)
    anilist = AniListClient(config.anilist_token)
    state = StateStore("data/michibiki.db")

    engine = SyncEngine(
        suwayomi=suwayomi,
        anilist=anilist,
        state=state,
        dry_run=config.dry_run,
    )

    logger.info("=" * 50)
    logger.info("Michibiki Sync Run — %s", "DRY RUN" if config.dry_run else "LIVE")
    logger.info("=" * 50)

    stats = await engine.run()

    logger.info("=" * 50)
    logger.info(
        "Summary: %d checked, %d updated, %d skipped, %d errors",
        stats["checked"],
        stats["updated"],
        stats["skipped"],
        stats["errors"],
    )
    logger.info("=" * 50)

    return stats


def poll_loop(config):
    """Run poll mode: sync Suwayomi→AniList on interval."""
    _wait_for_suwayomi(config.suwayomi_url)

    while True:
        try:
            stats = asyncio.run(run_sync(config))
            if stats["errors"] > 0:
                logger.warning("Sync completed with %d error(s)", stats["errors"])
        except Exception:
            logger.error("Sync cycle failed", exc_info=True)

        logger.info("Sleeping for %d seconds...", config.poll_interval_seconds)
        time.sleep(config.poll_interval_seconds)


# ── Watch mode: backup watcher ─────────────────────────────────────

def watch_loop(config):
    """Run watch mode: monitor for .tachibk backups and sync."""
    from src.bakumon import watch_for_backups

    # Ensure backup directory exists
    backup_dir = Path(config.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Wait for Suwayomi in case we need to populate
    if config.populate_suwayomi:
        _wait_for_suwayomi(config.suwayomi_url)

    watch_for_backups(
        backup_dir=str(backup_dir),
        config=config,
        poll_interval=config.poll_interval_seconds,
        populate_suwayomi=config.populate_suwayomi,
        clear_suwayomi_first=config.clear_suwayomi_first,
        dry_run=config.dry_run,
    )


# ── Shared helpers ──────────────────────────────────────────────────

def _wait_for_suwayomi(url: str, max_retries: int = 12, delay: int = 5):
    """Ping Suwayomi until it responds or give up."""
    ping_query = '{"query": "{ mangas(first:1) { nodes { id } } }"}'
    for attempt in range(1, max_retries + 1):
        try:
            resp = httpx.post(url, content=ping_query, timeout=httpx.Timeout(10))
            resp.raise_for_status()
            if "data" in resp.json():
                logger.info("Suwayomi ready (attempt %d)", attempt)
                return
        except Exception as e:
            logger.warning(
                "Suwayomi not ready (attempt %d/%d): %s",
                attempt, max_retries, e,
            )
        time.sleep(delay)
    logger.error(
        "Suwayomi unreachable after %d attempts — proceeding anyway", max_retries
    )


# ── Main ────────────────────────────────────────────────────────────

def main():
    """Main entrypoint — selects mode based on MODE env var.

    MODE=watch  → Watch for .tachibk backups, sync to AniList + populate Suwayomi
    MODE=poll   → Query Suwayomi for tracker progress, sync to AniList (legacy)
    """
    config = load_config()

    mode = os.environ.get("MODE", "poll")

    logger.info(
        "Michibiki starting — mode=%s, dry_run=%s",
        mode,
        config.dry_run,
    )

    if mode == "watch":
        watch_loop(config)
    else:
        poll_loop(config)


if __name__ == "__main__":
    main()
