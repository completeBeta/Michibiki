"""Download Governor — real-time batch tracking via Suwayomi HTTP polling.

After WebSocket proved unreliable (subscription completes after idle, reconnect races),
switched to HTTP polling: query downloadStatus every few seconds. Simple, reliable.

B1 approach: Poll completion status instead of blind time.sleep().
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

import httpx

log = logging.getLogger("michibiki.governor")

SUWAYOMI_HTTP_URL = os.getenv("SUWAYOMI_URL", "http://suwayomi:4567/api/graphql")
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "/downloads")

TERMINAL_STATES = {"FINISHED", "ERROR"}


class Governor:
    """Polls Suwayomi HTTP API to track download completion.

    Simple and reliable — no WebSocket lifecycle issues.
    """

    def __init__(self):
        self._http_client: httpx.AsyncClient | None = None

    async def connect(self) -> None:
        """No-op for HTTP polling (kept for API compatibility)."""
        self._http_client = httpx.AsyncClient(timeout=15)

    async def disconnect(self) -> None:
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def _get_queue(self) -> list[dict]:
        """Fetch current download queue from Suwayomi."""
        assert self._http_client
        resp = await self._http_client.post(SUWAYOMI_HTTP_URL, json={
            "query": "{ downloadStatus { state queue { chapter { id } state progress } } }"
        })
        resp.raise_for_status()
        return resp.json().get("data", {}).get("downloadStatus", {}).get("queue", [])

    async def wait_for_batch(
        self, chapter_ids: list[int], timeout_per_chapter: int = 600
    ) -> tuple[list[int], list[int]]:
        """Poll until all chapters reach terminal state. Returns (done, failed)."""
        target_ids = set(chapter_ids)
        if not target_ids:
            return [], []

        log.info("Polling for %d chapters (timeout=%ds)...", len(target_ids), timeout_per_chapter)

        elapsed = 0
        poll_interval = 3  # seconds between polls
        known_finished: set[int] = set()
        known_failed: set[int] = set()

        while elapsed < timeout_per_chapter:
            try:
                queue = await self._get_queue()
            except Exception as e:
                log.warning("Poll failed: %s (retrying...)", e)
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                continue

            # Check each queued item against our target IDs
            for item in queue:
                ch = item.get("chapter")
                cid = ch.get("id") if ch else None
                if cid is None or cid not in target_ids:
                    continue
                state = item.get("state", "?")
                if state == "FINISHED":
                    known_finished.add(cid)
                elif state == "ERROR":
                    known_failed.add(cid)

            # Check if we know about all chapters (some might have already finished
            # and fallen off the queue — count them as done if they're not in queue)
            accounted = known_finished | known_failed
            remaining = target_ids - accounted

            if not remaining:
                log.info("Batch complete: %d done, %d failed", len(known_finished), len(known_failed))
                return list(known_finished), list(known_failed)

            # If queue is empty and we still have remaining, they either completed
            # (fell off queue) or were never queued (already downloaded / rejected).
            # Log what's happening for debugging.
            queue_ids = set()
            for item in queue:
                ch = item.get("chapter")
                if ch:
                    queue_ids.add(ch.get("id"))

            still_in_queue = remaining & queue_ids
            not_in_queue = remaining - queue_ids

            if not_in_queue and not still_in_queue:
                # All remaining chapters are not in queue — they've either completed
                # or were never queued. Assume completed.
                known_finished |= not_in_queue
                log.info("Batch complete (queue empty): %d done, %d failed", len(known_finished), len(known_failed))
                return list(known_finished), list(known_failed)

            log.debug(
                "Poll #%d: %d/%d done, %d remaining (queue has %d items)",
                elapsed // poll_interval + 1,
                len(accounted),
                len(target_ids),
                len(remaining),
                len(queue),
            )

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        # Timeout — count remaining as failed
        remaining = target_ids - known_finished - known_failed
        log.warning(
            "Batch timeout after %ds: %d done, %d failed, %d unknown",
            elapsed, len(known_finished), len(known_failed), len(remaining),
        )
        return list(known_finished), list(known_failed | remaining)

    async def organize_downloads(self, manga_title: str, chapter_ids: list[int]) -> int:
        """Move completed CBZs into /downloads/<Manga Title>/."""
        dest_dir = Path(DOWNLOADS_DIR) / manga_title
        dest_dir.mkdir(parents=True, exist_ok=True)
        organized = 0
        assert self._http_client
        for cid in chapter_ids:
            try:
                resp = await self._http_client.post(SUWAYOMI_HTTP_URL, json={
                    "query": "query($id: Int!) { chapter(id: $id) { name chapterNumber isDownloaded } }",
                    "variables": {"id": cid},
                })
                resp.raise_for_status()
                ch = resp.json().get("data", {}).get("chapter", {})
                if not ch or not ch.get("isDownloaded"):
                    continue
                ch_num = ch.get("chapterNumber") or "?"
                ch_name = ch.get("name", f"Ch. {ch_num}")
                source_dir = Path(DOWNLOADS_DIR)
                patterns = [f"*Ch. {ch_num}*.cbz", f"*Chapter {ch_num}*.cbz",
                            f"*Ch.{ch_num}*.cbz", f"*{ch_num:04.0f}*.cbz"]
                found = None
                for pat in patterns:
                    matches = list(source_dir.glob(pat))
                    if matches:
                        found = max(matches, key=lambda p: p.stat().st_mtime)
                        break
                if not found:
                    recent = [p for p in source_dir.glob("*.cbz") if time.time() - p.stat().st_mtime < 300]
                    if recent:
                        found = max(recent, key=lambda p: p.stat().st_mtime)
                if found:
                    safe = "".join(c for c in f"Ch. {ch_num} - {ch_name}.cbz" if c.isprintable()).replace("/", "-")
                    shutil.move(str(found), str(dest_dir / safe))
                    organized += 1
            except Exception as e:
                log.error("Organize chapter %d failed: %s", cid, e)
        return organized
