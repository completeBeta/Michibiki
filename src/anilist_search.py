"""AniList title search — find media IDs by manga title.

Used when the backup has no tracker binding for a manga.
Queries AniList's GraphQL Page endpoint to get multiple results,
then scores each candidate to pick the best match.
"""

import logging
import re
from pathlib import Path
from typing import Any

import httpx

from .anilist import retry_with_backoff

log = logging.getLogger(__name__)

ANILIST_API = "https://graphql.anilist.co"

SEARCH_QUERY = """
query ($search: String, $page: Int) {
  Page(page: $page, perPage: 5) {
    media(search: $search, type: MANGA, format_not_in: [NOVEL]) {
      id
      title {
        romaji
        english
        native
      }
      format
      status
      volumes
      chapters
      startDate { year }
    }
  }
}
"""

# Number of search results to evaluate for best match
SEARCH_RESULT_LIMIT = 5

# Minimum score to accept a match (0.0–1.0)
MATCH_THRESHOLD = 0.50

# Manual overrides for ambiguous titles that AniList search gets wrong.
# Loaded from data/title_overrides.json (volume-mounted, editable without rebuild).
# Falls back to hardcoded defaults if the file is missing.
# Format: {"simplified_title": anilist_media_id, ...}
_TITLE_OVERRIDES_PATH = Path(__file__).parent.parent / "data" / "title_overrides.json"

_DEFAULT_OVERRIDES: dict[str, int] = {
    "gate": 71733,  # Gate: Where the JSDF Fought (28 vols) — not GATE (36977, 4 vols)
}

def _load_overrides() -> dict[str, int]:
    """Load title overrides from JSON file, falling back to defaults."""
    try:
        if _TITLE_OVERRIDES_PATH.exists():
            import json
            with open(_TITLE_OVERRIDES_PATH) as f:
                data = json.load(f)
            if isinstance(data, dict):
                overrides = {}
                for k, v in data.items():
                    # Skip comment/non-numeric entries
                    if isinstance(v, (int, float)):
                        overrides[str(k).lower()] = int(v)
                return overrides
    except Exception as e:
        log.warning("Failed to load title overrides from %s: %s", _TITLE_OVERRIDES_PATH, e)
    return dict(_DEFAULT_OVERRIDES)

TITLE_OVERRIDES: dict[str, int] = _load_overrides()

# Common subtitle patterns to strip for better search matching
_SUBTITLE_PATTERNS = [
    "Part ", "Season ", "Arc", "Side Story", "Special",
    "Extra", "Omake", "Anthology", "Volume ",
]


class AniListSearchError(Exception):
    """Raised when AniList search fails."""


def _simplify(s: str) -> str:
    """Lowercase, strip punctuation, normalize whitespace for comparison."""
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)  # punctuation → space
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _score_match(query: str, candidate: dict[str, Any]) -> float:
    """Score how well an AniList search result matches the search query.

    Returns 0.0–1.0. Higher is better.
    """
    q = _simplify(query)
    if not q:
        return 0.0

    # Collect all title variants from the candidate
    titles = []
    title_obj = candidate.get("title", {})
    for key in ("romaji", "english", "native"):
        val = title_obj.get(key)
        if val:
            titles.append(_simplify(val))

    best = 0.0
    for t in titles:
        if not t:
            continue

        # Exact match (case-insensitive, after simplification)
        if t == q:
            score = 0.85
        # Result title starts with query (e.g. "Gate" → "gate thus the jsdf fought there")
        elif t.startswith(q + " "):
            # Prefer longer/more specific titles that start with the query
            # over bare exact matches on short titles
            score = 0.90
        # Query starts with result title (unlikely but handle it)
        elif q.startswith(t + " "):
            score = 0.80
        # Result title contains query as a whole word or at boundaries
        elif (" " + q + " ") in (" " + t + " "):
            score = 0.70
        # Substring match (fallback)
        elif q in t or t in q:
            score = 0.55
        else:
            # Word overlap score
            q_words = set(q.split())
            t_words = set(t.split())
            if q_words and t_words:
                overlap = len(q_words & t_words)
                if overlap == len(q_words):
                    # All query words appear in title
                    score = 0.50 + (0.10 * min(overlap / len(t_words), 1))
                elif overlap > 0:
                    score = 0.30 * (overlap / len(q_words))
                else:
                    score = 0.0
            else:
                score = 0.0

        if score > best:
            best = score

    return best


async def search_anilist(
    title: str,
    token: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> int | None:
    """Search AniList for a manga by title.

    Queries multiple results and picks the best match via scoring.
    Returns the AniList media ID if a good match is found, None otherwise.
    """
    # Check manual overrides first — some titles confuse AniList search
    override_key = _simplify(title)
    if override_key in TITLE_OVERRIDES:
        override_id = TITLE_OVERRIDES[override_key]
        log.info(
            "AniList search: '%s' → ID %d [manual override]",
            title, override_id,
        )
        return override_id

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Try original title first, then cleaned version if no good match
    cleaned = _clean_title(title)
    search_terms = [title] if title == cleaned else [title, cleaned]

    async def _do_page_search(
        client: httpx.AsyncClient, term: str
    ) -> list[dict[str, Any]]:
        payload = {
            "query": SEARCH_QUERY,
            "variables": {"search": term, "page": 1},
        }

        async def _call():
            resp = await client.post(ANILIST_API, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

        data = await retry_with_backoff(_call)
        page = data.get("data", {}).get("Page", {})
        return page.get("media", []) or []

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=15)

    try:
        for term in search_terms:
            if not term.strip():
                continue
            try:
                results = await _do_page_search(client, term)

                if not results:
                    log.info(
                        "AniList search: no results for '%s'", term
                    )
                    continue

                # Score all results against the ORIGINAL title (not cleaned)
                scored = [
                    (candidate, _score_match(title, candidate))
                    for candidate in results
                ]
                scored.sort(key=lambda x: x[1], reverse=True)

                # Log all candidates for debugging
                for candidate, score in scored:
                    romaji = candidate.get("title", {}).get("romaji", "?")
                    log.debug(
                        "  Candidate: ID %d (%s) — score %.2f",
                        candidate["id"],
                        romaji,
                        score,
                    )

                best_candidate, best_score = scored[0]

                if best_score >= MATCH_THRESHOLD:
                    best_id = best_candidate["id"]
                    romaji = best_candidate.get("title", {}).get("romaji", "?")
                    log.info(
                        "AniList search: '%s' → ID %d (%s) [score: %.2f]",
                        title,
                        best_id,
                        romaji,
                        best_score,
                    )
                    return int(best_id)

                log.info(
                    "AniList search: no good match for '%s' "
                    "(best: ID %d, score %.2f < %.2f)",
                    title,
                    best_candidate["id"],
                    best_score,
                    MATCH_THRESHOLD,
                )
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
    # Remove content in parentheses at the end
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", title)

    # Remove common subtitle patterns and everything after them
    for pattern in _SUBTITLE_PATTERNS:
        idx = cleaned.lower().find(pattern.lower())
        if idx > 5:  # Only strip if pattern is after a reasonable title prefix
            cleaned = cleaned[:idx].strip()

    return cleaned.strip()
