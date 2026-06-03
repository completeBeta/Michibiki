"""Michibiki — Daily Mihon→AniList reading progress sync."""

import asyncio
import logging
import sys
import time

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


async def run_sync(config):
    """Execute a single sync cycle."""
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


def main():
    """Main entrypoint — waits for Suwayomi, then syncs on interval."""
    config = load_config()

    logger.info(
        "Michibiki starting — sync every %ds (dry_run=%s)",
        config.poll_interval_seconds,
        config.dry_run,
    )

    _wait_for_suwayomi(config.suwayomi_url)

    while True:
        try:
            stats = asyncio.run(run_sync(config))
            if stats["errors"] > 0:
                logger.warning(
                    "Sync completed with %d error(s)", stats["errors"]
                )
        except Exception:
            logger.error("Sync cycle failed", exc_info=True)

        logger.info(
            "Sleeping for %d seconds...", config.poll_interval_seconds
        )
        time.sleep(config.poll_interval_seconds)


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
    logger.error("Suwayomi unreachable after %d attempts — proceeding anyway", max_retries)


if __name__ == "__main__":
    main()
