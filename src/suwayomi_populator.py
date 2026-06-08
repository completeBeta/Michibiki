"""Suwayomi library populator — programmatically add manga and bind trackers.

Uses Suwayomi's GraphQL API to:
1. Discover installed source extensions
2. Search for manga across available sources (scored matching)
3. Add manga to the library (fetchManga)
4. Bind AniList trackers (bindTrack)
5. Clear existing library (optional)

Strategy: tries installed sources in order, scores search results
against the target title to avoid wrong matches.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

# AniList tracker ID in Suwayomi
ANILIST_TRACKER_ID = 2

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

FETCH_CHAPTERS = """
mutation FetchChapters($input: FetchChaptersInput!) {
  fetchChapters(input: $input) {
    clientMutationId
    chapters {
      name
    }
  }
}
"""


@dataclass
class SourceInfo:
    id: str
    name: str


# Minimum score to accept a search match (0.0–1.0)
MATCH_THRESHOLD = 0.50


def _simplify(s: str) -> str:
    """Lowercase, strip punctuation, normalize whitespace."""
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _score_suwayomi_match(
    query: str, candidate: dict[str, Any]
) -> float:
    """Score how well a Suwayomi search result matches the target title.

    Suwayomi results have a flat `title` field (not nested like AniList).
    Returns 0.0–1.0.
    """
    q = _simplify(query)
    if not q:
        return 0.0

    t = _simplify(candidate.get("title") or "")
    if not t:
        return 0.0

    # Exact match
    if t == q:
        return 0.85
    # Result title starts with query (e.g. "Gate" → "Gate: Where the JSDF Fought")
    if t.startswith(q + " "):
        return 0.90
    # Query starts with result title
    if q.startswith(t + " "):
        return 0.80
    # Result contains query as a whole word
    if (" " + q + " ") in (" " + t + " "):
        return 0.70
    # Substring match
    if q in t or t in q:
        return 0.55

    # Word overlap
    q_words = set(q.split())
    t_words = set(t.split())
    if q_words and t_words:
        overlap = len(q_words & t_words)
        if overlap == len(q_words):
            return 0.50 + (0.10 * min(overlap / len(t_words), 1))
        elif overlap > 0:
            return 0.30 * (overlap / len(q_words))

    return 0.0


@dataclass
class PopulateResult:
    """Result of a Suwayomi population run."""

    added: int = 0
    bound: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


class SuwayomiPopulator:
    """Populates a Suwayomi library from backup manga entries."""

    def __init__(self, graphql_url: str):
        self.url = graphql_url
        self._sources: list[SourceInfo] | None = None
        self._library_titles: set[str] | None = None

    async def _post(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GraphQL query. Raises on HTTP errors, returns parsed JSON."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.url,
                json={"query": query, "variables": variables or {}},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()

    async def _get_installed_sources(self) -> list[SourceInfo]:
        """Discover installed source extensions, excluding Local source."""
        if self._sources is not None:
            return self._sources

        data = await self._post("""
        query {
          sources {
            nodes { id name }
          }
        }
        """)
        nodes = data.get("data", {}).get("sources", {}).get("nodes", [])
        sources = []
        for n in nodes:
            sid = n.get("id", "")
            name = n.get("name", "")
            # Skip Local source (id "0") — can't search it
            if sid and sid != "0" and name.lower() != "local source":
                sources.append(SourceInfo(id=sid, name=name))

        log.info("Found %d usable sources: %s", len(sources),
                 ", ".join(s.name for s in sources))
        self._sources = sources
        return sources

    async def _get_library_titles(self) -> set[str]:
        """Get lowercase titles already in the Suwayomi library."""
        if self._library_titles is not None:
            return self._library_titles

        data = await self._post("""
        query {
          mangas(inLibrary: true) {
            nodes { title }
          }
        }
        """)
        nodes = data.get("data", {}).get("mangas", {}).get("nodes", [])
        titles = {n["title"].lower().strip() for n in nodes if n.get("title")}
        log.info("Library has %d manga", len(titles))
        self._library_titles = titles
        return titles

    async def clear_library(self) -> int:
        """Remove all manga from Suwayomi's library."""
        data = await self._post("""
        query {
          mangas(inLibrary: true) { nodes { id title } }
        }
        """)
        nodes = data.get("data", {}).get("mangas", {}).get("nodes", [])

        count = 0
        for m in nodes:
            await self._post("""
            mutation RemoveFromLibrary($input: UpdateMangaInput!) {
              updateManga(input: $input) { clientMutationId }
            }
            """, {
                "input": {
                    "clientMutationId": f"clear-{m['id']}",
                    "id": m["id"],
                    "patch": {"inLibrary": False},
                }
            })
            count += 1

        log.info("Cleared %d manga from Suwayomi library", count)
        self._library_titles = None
        self._sources = None
        return count

    async def populate(
        self,
        entries: list,
        *,
        max_concurrent: int = 3,
    ) -> PopulateResult:
        """Populate Suwayomi with manga entries and bind AniList trackers.

        Tries each installed source until the manga is found and added.
        Skips entries already in the library.

        Args:
            entries: List of MangaEntry objects (must have anilist_media_id set).
            max_concurrent: Max concurrent GraphQL calls.

        Returns:
            PopulateResult with counts of added/bound/skipped/failed manga.
        """
        # Preload sources and library
        sources = await self._get_installed_sources()
        if not sources:
            log.error("No usable source extensions installed in Suwayomi. "
                      "Install sources via WebUI → Extensions.")
            result = PopulateResult()
            result.errors.append("No sources installed")
            return result

        library_titles = await self._get_library_titles()
        result = PopulateResult()
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _process(entry) -> None:
            async with semaphore:
                try:
                    # Skip if already in library
                    if entry.title.lower().strip() in library_titles:
                        log.info("Already in library: '%s'", entry.title)
                        result.skipped += 1
                        return

                    # Skip entries without AniList IDs? No — add them anyway.
                    # We just can't bind the tracker. User can do that manually.

                    manga_id = await self._search_and_add(entry, sources)
                    if manga_id:
                        result.added += 1
                        library_titles.add(entry.title.lower().strip())
                        # Bind tracker
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

        tasks = [_process(e) for e in entries]
        await asyncio.gather(*tasks)

        log.info(
            "Suwayomi populate complete: %d added, %d bound, %d skipped, %d failed",
            result.added, result.bound, result.skipped, result.failed,
        )
        return result

    async def _search_and_add(
        self, entry, sources: list[SourceInfo]
    ) -> int | None:
        """Search for a manga across installed sources and add to library.

        Tries each source in order. Returns Suwayomi manga ID or None.
        """
        for source in sources:
            manga_id = await self._try_source(entry, source)
            if manga_id:
                return manga_id

        log.warning("No source found '%s' — tried %d sources",
                    entry.title, len(sources))
        return None

    async def _try_source(
        self, entry, source: SourceInfo
    ) -> int | None:
        """Try to find and add a manga from a specific source.

        Returns Suwayomi manga ID or None.
        """
        # Step 1: Search the source
        variables = {
            "input": {
                "source": source.id,
                "query": entry.title,
                "type": "SEARCH",
                "page": 1,
            }
        }
        data = await self._post(FETCH_SOURCE_MANGA, variables)

        fetch_result = data.get("data", {}).get("fetchSourceManga")
        # Handle null response (source not found, API error, etc.)
        if fetch_result is None:
            log.debug("Source '%s' returned null for '%s'",
                      source.name, entry.title)
            return None

        mangas = fetch_result.get("mangas") or []
        if not mangas:
            log.debug("No search results for '%s' in %s",
                      entry.title, source.name)
            return None

        # Step 2: Pick best match and add to library
        best_match = self._pick_best_match(entry.title, mangas)
        if not best_match:
            log.debug("No good match for '%s' in %s results",
                      entry.title, source.name)
            return None

        source_manga_id = best_match["id"]
        add_variables = {"input": {"id": source_manga_id}}

        try:
            data = await self._post(FETCH_MANGA, add_variables)
        except Exception as e:
            log.warning("Add failed for '%s' from %s: %s",
                        entry.title, source.name, e)
            return None

        fetch_result = data.get("data", {}).get("fetchManga")
        if fetch_result is None:
            log.warning("fetchManga returned null for '%s' (source: %s)",
                        entry.title, source.name)
            return None

        manga = fetch_result.get("manga") or {}
        manga_id = manga.get("id")
        if manga_id:
            log.info("Added '%s' via %s → Suwayomi ID %d",
                     entry.title, source.name, manga_id)
            # Fetch chapter list (Suwayomi doesn't auto-fetch on add)
            await self._fetch_chapters(manga_id)
        return manga_id

    async def _fetch_chapters(self, manga_id: int) -> bool:
        """Trigger chapter list fetch for a newly added manga.

        Suwayomi does NOT auto-fetch chapters when manga is added to the
        library. Without this, chapters() returns 0 and the WebUI shows
        empty manga. Must be called after fetchManga succeeds.
        """
        try:
            data = await self._post(FETCH_CHAPTERS, {
                "input": {"mangaId": manga_id}
            })
        except Exception as e:
            log.warning("Chapter fetch failed for manga %d: %s", manga_id, e)
            return False

        if "errors" in data:
            log.warning("Chapter fetch error for manga %d: %s",
                        manga_id, data["errors"])
            return False

        chapters = (data.get("data", {})
                    .get("fetchChapters", {})
                    .get("chapters") or [])
        log.info("Fetched %d chapters for manga %d", len(chapters), manga_id)
        return True

    async def _bind_tracker(
        self, manga_id: int, anilist_media_id: int
    ) -> bool:
        """Bind the AniList tracker to a Suwayomi manga.

        NOTE: Suwayomi's bindTrack mutation requires a prior tracker
        search to be done via the WebUI (it populates an internal cache).
        Without it, bindTrack returns 'Collection is empty'. This is a
        known limitation — bind trackers manually in the WebUI for now:
        Manga → Tracking tab → search → select AniList entry.
        """
        variables = {
            "input": {
                "mangaId": manga_id,
                "trackerId": ANILIST_TRACKER_ID,
                "remoteId": str(anilist_media_id),
            }
        }
        try:
            data = await self._post(BIND_TRACK, variables)
        except Exception as e:
            log.error("Tracker bind failed for manga %d: %s", manga_id, e)
            return False

        # Check for errors (Suwayomi returns errors inline, not as HTTP errors)
        if "errors" in data:
            err_msg = str(data["errors"])
            if "Collection is empty" in err_msg:
                log.warning(
                    "Tracker bind for manga %d requires manual setup: "
                    "open Suwayomi WebUI → manga → Tracking tab → "
                    "search AniList → select entry. "
                    "(Suwayomi bindTrack bug: no prior search cache)",
                    manga_id,
                )
            else:
                log.warning("bindTrack error for manga %d: %s", manga_id, err_msg)
            return False

        bind_result = data.get("data", {}).get("bindTrack")
        if bind_result is None:
            log.warning("bindTrack returned null for manga %d", manga_id)
            return False

        record = bind_result.get("trackRecord") or {}
        ok = bool(record.get("id"))
        if ok:
            log.info("Bound tracker: manga %d → AniList %d",
                     manga_id, anilist_media_id)
        return ok

    @staticmethod
    def _pick_best_match(
        title: str, results: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Pick the best matching manga from search results using scoring.

        Scores each result against the target title and returns the
        highest-scoring match above the threshold. This prevents
        wrong matches (e.g. 'Gate' → 'Dragon Tiger Gate GOLD').
        """
        if not results:
            return None

        scored = [
            (candidate, _score_suwayomi_match(title, candidate))
            for candidate in results
        ]
        scored.sort(key=lambda x: x[1], reverse=True)

        best, best_score = scored[0]

        # Log all candidates for debugging
        for c, s in scored:
            log.debug("  Candidate: '%s' — score %.2f", c.get("title", "?"), s)

        if best_score >= 0.50:
            log.info("  Best match: '%s' (score %.2f)", best.get("title", "?"), best_score)
            return best

        log.warning(
            "No good match for '%s' (best: '%s', score %.2f < 0.50)",
            title, best.get("title", "?"), best_score,
        )
        return None
