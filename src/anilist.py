"""AniList GraphQL client — pushes reading progress updates."""

import asyncio
import logging
import math
from enum import Enum

import httpx

log = logging.getLogger(__name__)

ANILIST_API = "https://graphql.anilist.co"

MUTATION = """
mutation ($mediaId: Int, $progress: Int, $status: MediaListStatus) {
  SaveMediaListEntry(mediaId: $mediaId, progress: $progress, status: $status) {
    id
    mediaId
    progress
    progressVolumes
    status
  }
}
"""

MUTATION_VOLUMES = """
mutation ($mediaId: Int, $progressVolumes: Int, $status: MediaListStatus) {
  SaveMediaListEntry(mediaId: $mediaId, progressVolumes: $progressVolumes, status: $status) {
    id
    mediaId
    progress
    progressVolumes
    status
  }
}
"""


async def retry_with_backoff(
    fn,
    *args,
    max_retries: int = 5,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    **kwargs,
):
    """Call an async function with exponential backoff on HTTP 429 errors.

    Delays: 2s, 4s, 8s, 16s, 32s (capped at 60s).
    Non-429 exceptions are re-raised immediately.
    """
    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                log.warning(
                    "AniList rate limited (429) — retrying in %.0fs (attempt %d/%d)",
                    delay, attempt + 1, max_retries,
                )
                await asyncio.sleep(delay)
            else:
                raise


class MediaListStatus(str, Enum):
    CURRENT = "CURRENT"
    PLANNING = "PLANNING"
    COMPLETED = "COMPLETED"
    DROPPED = "DROPPED"
    PAUSED = "PAUSED"
    REPEATING = "REPEATING"


class AniListClient:
    """Async client for AniList's GraphQL API."""

    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def map_status(suwayomi_status: str) -> MediaListStatus:
        """Map Suwayomi manga status to AniList MediaListStatus."""
        mapping: dict[str, MediaListStatus] = {
            "ONGOING": MediaListStatus.CURRENT,
            "COMPLETED": MediaListStatus.COMPLETED,
            "PUBLISHING_FINISHED": MediaListStatus.CURRENT,
        }
        return mapping.get(suwayomi_status, MediaListStatus.CURRENT)

    @staticmethod
    def round_progress(chapter: float | None) -> int:
        """Floor chapter number to int (42.5 → 42, None → 0).

        Values below 0.5 are treated as 0 — this catches floating-point
        artifacts like 9.999999747378752e-05 (~0.0001) that Mihon sometimes
        stores instead of clean 0 for unread chapters.
        """
        if chapter is None:
            return 0
        if chapter < 0.5:
            return 0
        return math.floor(chapter)

    async def update_progress(
        self,
        media_id: int,
        progress: float | None,
        status: MediaListStatus,
        dry_run: bool = False,
        is_volume_based: bool = False,
    ) -> dict:
        """Push progress update to AniList.

        When is_volume_based=True, sends progressVolumes instead of progress.
        When dry_run=True, returns a preview dict without calling the API.
        """
        if dry_run:
            return self._build_dry_run_result(media_id, progress, status, is_volume_based)

        payload = self._build_save_mutation(media_id, progress, status, is_volume_based)
        async with httpx.AsyncClient(timeout=15) as client:

            async def _call():
                resp = await client.post(
                    ANILIST_API,
                    json=payload,
                    headers=self.headers,
                )
                resp.raise_for_status()
                return resp.json()

            return await retry_with_backoff(_call)

    def _build_save_mutation(
        self,
        media_id: int,
        progress: float | None,
        status: MediaListStatus,
        is_volume_based: bool = False,
    ) -> dict:
        if is_volume_based:
            return {
                "query": MUTATION_VOLUMES,
                "variables": {
                    "mediaId": media_id,
                    "progressVolumes": self.round_progress(progress),
                    "status": status.value,
                },
            }
        return {
            "query": MUTATION,
            "variables": {
                "mediaId": media_id,
                "progress": self.round_progress(progress),
                "status": status.value,
            },
        }

    def _build_dry_run_result(
        self,
        media_id: int,
        progress: float | None,
        status: MediaListStatus,
        is_volume_based: bool = False,
    ) -> dict:
        result = {
            "dry_run": True,
            "mediaId": media_id,
            "status": status.value,
        }
        if is_volume_based:
            result["progressVolumes"] = self.round_progress(progress)
        else:
            result["progress"] = self.round_progress(progress)
        return result
