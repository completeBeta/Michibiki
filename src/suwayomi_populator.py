"""Suwayomi library populator — programmatically add manga and bind trackers.

Uses Suwayomi's GraphQL API to:
1. Search for manga in source extensions
2. Add manga to the library (fetchManga)
3. Bind AniList trackers (bindTrack)
4. Clear existing library (optional)
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

# AniList tracker ID in Suwayomi
ANILIST_TRACKER_ID = 2

# Source IDs for common extensions (same across Mihon/Suwayomi since
# they share the keiyoushi extension ecosystem)
BATOTO_SOURCE_ID = 1678128543826757763

# GraphQL fragments
FETCH_SOURCE_MANGA = """
mutation SearchSource($input: FetchSourceMangaInput!) {
  fetchSourceManga(input: $input) {
    clientMutationId
    hasNextPage
    mangas {
      id
      title
      url
    }
  }
}
"""

FETCH_MANGA = """
mutation AddManga($input: FetchMangaInput!) {
  fetchManga(input: $input) {
    clientMutationId
    manga {
      id
      title
      inLibrary
    }
  }
}
"""

BIND_TRACK = """
mutation BindTrack($input: BindTrackInput!) {
  bindTrack(input: $input) {
    clientMutationId
    trackRecord {
      id
      remoteId
    }
  }
}
"""

UPDATE_TRACK = """
mutation UpdateTrack($input: UpdateTrackInput!) {
  updateTrack(input: $input) {
    clientMutationId
    trackRecord {
      id
      lastChapterRead
    }
  }
}
"""


@dataclass
class PopulateResult:
    """Result of a Suwayomi population run."""

    added: int = 0
    bound: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


class SuwayomiPopulator:
    """Populates a Suwayomi library from backup manga entries."""

    def __init__(self, graphql_url: str):
        self.url = graphql_url

    async def clear_library(self) -> int:
        """Remove all manga from Suwayomi's library.

        Returns count of removed manga.
        """
        query = """
        mutation ClearLibrary {
          updateMangas(input: {clientMutationId: "clear", patch: {inLibrary: false}}) {
            clientMutationId
          }
        }
        """
        # Actually, Suwayomi may not support bulk update. Let's use
        # the query-then-delete approach via updateManga for each manga.
        manga_query = """
        query GetAllManga {
          mangas(condition: {inLibrary: true}) {
            nodes { id title }
          }
        }
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.url,
                json={"query": manga_query},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            mangas = data.get("data", {}).get("mangas", {}).get("nodes", [])

            count = 0
            for m in mangas:
                # Remove from library via updateManga
                mutation = """
                mutation RemoveFromLibrary($input: UpdateMangaInput!) {
                  updateManga(input: $input) {
                    clientMutationId
                  }
                }
                """
                await client.post(
                    self.url,
                    json={
                        "query": mutation,
                        "variables": {
                            "input": {
                                "clientMutationId": f"clear-{m['id']}",
                                "id": m["id"],
                                "patch": {"inLibrary": False},
                            }
                        },
                    },
                    headers={"Content-Type": "application/json"},
                )
                count += 1

            log.info("Cleared %d manga from Suwayomi library", count)
            return count

    async def populate(
        self,
        entries: list,
        *,
        max_concurrent: int = 3,
    ) -> PopulateResult:
        """Populate Suwayomi with manga entries and bind AniList trackers.

        Args:
            entries: List of MangaEntry objects (must have anilist_media_id set).
            max_concurrent: Max concurrent GraphQL calls.

        Returns:
            PopulateResult with counts of added/bound/skipped/failed manga.
        """
        result = PopulateResult()
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _process(entry) -> None:
            async with semaphore:
                try:
                    manga_id = await self._search_and_add(entry)
                    if manga_id:
                        result.added += 1
                        if entry.anilist_media_id:
                            ok = await self._bind_tracker(
                                manga_id, entry.anilist_media_id
                            )
                            if ok:
                                result.bound += 1
                    else:
                        result.skipped += 1
                except Exception as e:
                    log.error("Failed to process '%s': %s", entry.title, e)
                    result.failed += 1
                    result.errors.append(f"{entry.title}: {e}")

        tasks = [_process(e) for e in entries if e.anilist_media_id]
        await asyncio.gather(*tasks)

        log.info(
            "Suwayomi populate complete: %d added, %d bound, %d skipped, %d failed",
            result.added,
            result.bound,
            result.skipped,
            result.failed,
        )
        return result

    async def _search_and_add(self, entry) -> int | None:
        """Search for a manga in source extensions and add to library.

        Returns Suwayomi manga ID if successful, None otherwise.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            # Step 1: Search in the manga's source
            search_vars = {
                "input": {
                    "source": str(entry.source_id),
                    "query": entry.title,
                    "type": "SEARCH",
                    "page": 1,
                }
            }
            resp = await client.post(
                self.url,
                json={"query": FETCH_SOURCE_MANGA, "variables": search_vars},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

            mangas = (
                data.get("data", {})
                .get("fetchSourceManga", {})
                .get("mangas", [])
            )
            if not mangas:
                log.warning("No search results for '%s' in source %d", entry.title, entry.source_id)
                return None

            # Step 2: Add the best match to library
            best_match = self._pick_best_match(entry.title, mangas)
            if not best_match:
                return None

            source_manga_id = best_match["id"]
            add_vars = {"input": {"id": source_manga_id}}
            resp = await client.post(
                self.url,
                json={"query": FETCH_MANGA, "variables": add_vars},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

            manga = (
                data.get("data", {})
                .get("fetchManga", {})
                .get("manga", {})
            )
            manga_id = manga.get("id")
            log.info("Added '%s' → Suwayomi manga ID %d", entry.title, manga_id)
            return manga_id

    async def _bind_tracker(
        self, manga_id: int, anilist_media_id: int
    ) -> bool:
        """Bind the AniList tracker to a Suwayomi manga."""
        async with httpx.AsyncClient(timeout=15) as client:
            vars_ = {
                "input": {
                    "mangaId": manga_id,
                    "trackerId": ANILIST_TRACKER_ID,
                    "remoteId": str(anilist_media_id),
                }
            }
            resp = await client.post(
                self.url,
                json={"query": BIND_TRACK, "variables": vars_},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            record = (
                data.get("data", {})
                .get("bindTrack", {})
                .get("trackRecord", {})
            )
            ok = bool(record.get("id"))
            if ok:
                log.info(
                    "Bound tracker: manga %d → AniList %d",
                    manga_id,
                    anilist_media_id,
                )
            return ok

    @staticmethod
    def _pick_best_match(
        title: str, results: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Pick the best matching manga from search results."""
        title_lower = title.lower().strip()
        # Prefer exact title match
        for r in results:
            if r.get("title", "").lower().strip() == title_lower:
                return r
        # Fall back to first result
        return results[0] if results else None
