"""AniList title search — find media IDs by manga title.

Used when the backup has no tracker binding for a manga.
Performs a fuzzy-ish search via AniList's GraphQL search endpoint.
"""

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

ANILIST_API = "https://graphql.anilist.co"

SEARCH_QUERY = """
query ($search: String) {
  Media(search: $search, type: MANGA, format_not_in: [NOVEL]) {
    id
    title {
      romaji
      english
      native
    }
    format
    status
    startDate { year }
  }
}
"""

# Number of search results to evaluate for best match
SEARCH_RESULT_LIMIT = 5

# Common subtitle patterns to strip for better search matching
_SUBTITLE_PATTERNS = [
    "Part ", "Season ", "Arc", "Side Story", "Special",
    "Extra", "Omake", "Anthology", "Volume ",
]


class AniListSearchError(Exception):
    """Raised when AniList search fails."""


async def search_anilist(
    title: str,
    token: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> int | None:
    """Search AniList for a manga by title.

    Returns the AniList media ID if a good match is found, None otherwise.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Try exact search first, then cleaned title
    search_terms = [title, _clean_title(title)]
    # Deduplicate while preserving order
    seen: set[str] = set()
    search_terms = [t for t in search_terms if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]

    async def _do_search(client: httpx.AsyncClient, term: str) -> dict[str, Any]:
        payload = {
            "query": SEARCH_QUERY,
            "variables": {"search": term},
        }
        resp = await client.post(ANILIST_API, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=15)

    try:
        for term in search_terms:
            if not term.strip():
                continue
            try:
                data = await _do_search(client, term)
                media = data.get("data", {}).get("Media")
                if media and isinstance(media, dict):
                    media_id = media.get("id")
                    if media_id:
                        log.info(
                            "AniList search: '%s' → ID %d (%s)",
                            term,
                            media_id,
                            media.get("title", {}).get("romaji", "?"),
                        )
                        return int(media_id)
            except Exception as e:
                log.warning("AniList search failed for '%s': %s", term, e)

        log.info("AniList search: no match for '%s'", title)
        return None
    finally:
        if own_client and client:
            await client.aclose()


def _clean_title(title: str) -> str:
    """Clean a manga title for better search matching.

    Strips common subtitle/suffix patterns that confuse AniList search.
    """
    import re

    # Remove content in parentheses at the end
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", title)

    # Remove common subtitle patterns and everything after them
    for pattern in _SUBTITLE_PATTERNS:
        idx = cleaned.lower().find(pattern.lower())
        if idx > 5:  # Only strip if pattern is after a reasonable title prefix
            cleaned = cleaned[:idx].strip()

    return cleaned.strip()
