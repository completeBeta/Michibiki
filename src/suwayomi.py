"""Suwayomi GraphQL client — fetches manga reading progress."""

from dataclasses import dataclass

import httpx

QUERY = """
query GetMangaProgress {
  mangas(condition: {inLibrary: true}) {
    nodes {
      id
      title
      status
      lastReadChapter {
        chapterNumber
        lastReadAt
      }
      trackRecords {
        nodes {
          id
          trackerId
          remoteId
          lastChapterRead
          totalChapters
          tracker { name }
        }
      }
    }
  }
}
"""


@dataclass
class MangaProgress:
    manga_id: int
    title: str
    status: str
    anilist_media_id: int | None
    last_chapter_read: float | None
    highest_read_chapter: float | None
    last_read_at: str | None


class SuwayomiClient:
    """Async client for Suwayomi's GraphQL API."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    async def fetch_manga_progress(self) -> list[MangaProgress]:
        """Fetch all in-library manga with AniList track records."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(self.base_url, json={"query": QUERY})
            resp.raise_for_status()
            data = resp.json()
        return self._parse_manga_progress(data)

    def _parse_manga_progress(self, data: dict) -> list[MangaProgress]:
        results: list[MangaProgress] = []
        for node in data["data"]["mangas"]["nodes"]:
            track_records = node.get("trackRecords", {}).get("nodes", [])
            anilist_tr = _find_anilist_tracker(track_records)

            if anilist_tr is None:
                continue

            last_read = node.get("lastReadChapter") or {}

            results.append(
                MangaProgress(
                    manga_id=node["id"],
                    title=node["title"],
                    status=node.get("status", "UNKNOWN"),
                    anilist_media_id=int(anilist_tr["remoteId"]),
                    last_chapter_read=anilist_tr.get("lastChapterRead"),
                    highest_read_chapter=last_read.get("chapterNumber"),
                    last_read_at=last_read.get("lastReadAt"),
                )
            )
        return results


def _find_anilist_tracker(track_records: list[dict]) -> dict | None:
    for tr in track_records:
        if tr.get("tracker", {}).get("name") == "AniList":
            return tr
    return None
