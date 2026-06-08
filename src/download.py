"""Batch download manga chapters from Suwayomi.

CLI tool — run inside the container:
  docker exec michibiki python -m src.download "Omniscient Reader" --all
"""

import argparse
import asyncio
import logging
import os
import sys
import time

import httpx

log = logging.getLogger(__name__)

SUWAYOMI_URL = os.getenv("SUWAYOMI_URL", "http://suwayomi:4567/api/graphql")


async def _find_manga(client: httpx.AsyncClient, title: str) -> tuple[int | None, str | None]:
    """Search Suwayomi library for a manga. Returns (id, title) or (None, None)."""
    query = """
    query {
      mangas(inLibrary: true) {
        nodes { id title }
      }
    }
    """
    resp = await client.post(SUWAYOMI_URL, json={"query": query})
    resp.raise_for_status()
    mangas = resp.json().get("data", {}).get("mangas", {}).get("nodes", [])

    if not mangas:
        print("Library is empty. Add manga to Suwayomi first.")
        return None, None

    t = title.lower().strip()

    # 1. Exact match
    for m in mangas:
        if m["title"].lower().strip() == t:
            return m["id"], m["title"]

    # 2. Contains match
    contains = [m for m in mangas if t in m["title"].lower()]
    if len(contains) == 1:
        return contains[0]["id"], contains[0]["title"]

    # 3. Word overlap
    query_words = set(t.split())
    scored = []
    for m in mangas:
        title_words = set(m["title"].lower().split())
        overlap = len(query_words & title_words)
        if overlap > 0:
            scored.append((overlap, m))
    scored.sort(key=lambda x: x[0], reverse=True)

    if len(scored) == 1:
        return scored[0][1]["id"], scored[0][1]["title"]

    if len(scored) > 1:
        print(f"Multiple matches for '{title}':")
        for _, m in scored[:10]:
            print(f"  • {m['title']} (ID: {m['id']})")
        print("\nUse a more specific title or the manga ID directly with --id")
        return None, None

    print(f"No match found for '{title}'. Library contents:")
    for m in mangas[:20]:
        print(f"  • {m['title']} (ID: {m['id']})")
    return None, None


async def _get_chapters(
    client: httpx.AsyncClient, manga_id: int
) -> list[dict]:
    """Get all chapters for a manga, sorted by chapter number."""
    query = """
    query($id: Int!) {
      manga(id: $id) {
        chapters {
          nodes { id name chapterNumber }
        }
      }
    }
    """
    resp = await client.post(
        SUWAYOMI_URL,
        json={"query": query, "variables": {"id": manga_id}},
    )
    resp.raise_for_status()
    chapters = resp.json().get("data", {}).get("manga", {}).get("chapters", {}).get("nodes", [])
    chapters.sort(key=lambda c: float(c.get("chapterNumber", 0) or 0))
    return chapters


async def _queue_batch(client: httpx.AsyncClient, ids: list[int]) -> str:
    """Queue a batch of chapters for download. Returns status string."""
    query = """
    mutation($ids: [Int!]!) {
      enqueueChapterDownloads(input: {ids: $ids}) {
        downloadStatus { state }
      }
    }
    """
    resp = await client.post(
        SUWAYOMI_URL,
        json={"query": query, "variables": {"ids": ids}},
    )
    resp.raise_for_status()
    data = resp.json()
    state = (
        data.get("data", {})
        .get("enqueueChapterDownloads", {})
        .get("downloadStatus", {})
        .get("state", "?")
    )
    return state


async def download(
    title: str | None = None,
    manga_id: int | None = None,
    *,
    all_chapters: bool = False,
    chapter_range: str | None = None,
    batch_size: int = 30,
    delay: int = 180,
    dry_run: bool = False,
    limit: int | None = None,
) -> int:
    """Download manga chapters from Suwayomi.

    Returns exit code (0 = success, 1 = error).
    """
    if not title and not manga_id:
        print("Error: specify either a title or --id", file=sys.stderr)
        return 1

    async with httpx.AsyncClient(timeout=30) as client:
        # Resolve manga
        if manga_id:
            found_id = manga_id
            found_title = f"ID {manga_id}"
            print(f"Using manga ID: {manga_id}")
        else:
            print(f"Looking up '{title}'...")
            found_id, found_title = await _find_manga(client, title)
            if not found_id:
                return 1
            print(f"Found: {found_title} (ID: {found_id})")

        # Get chapters
        chapters = await _get_chapters(client, found_id)
        total = len(chapters)
        print(f"Total chapters: {total}")

        # Filter
        if chapter_range:
            try:
                start, end = chapter_range.split("-")
                start_c, end_c = float(start), float(end)
            except ValueError:
                print(f"Error: invalid range '{chapter_range}' (use e.g. '1-50')", file=sys.stderr)
                return 1
            chapters = [
                c
                for c in chapters
                if start_c <= float(c.get("chapterNumber", 0) or 0) <= end_c
            ]
            print(f"Range {start}-{end}: {len(chapters)} chapters")

        if limit:
            chapters = chapters[:limit]
            print(f"Limited to first {len(chapters)} chapters")

        if not all_chapters and not chapter_range and not limit:
            print("Error: specify --all, --range, or --limit", file=sys.stderr)
            return 1

        if not chapters:
            print("No chapters to download.")
            return 0

        # Preview
        if dry_run:
            batches = (len(chapters) + batch_size - 1) // batch_size
            print(f"\nDRY RUN — would queue {len(chapters)} chapters in {batches} batches")
            for c in chapters[:10]:
                cn = c.get("chapterNumber") or "?"
                print(f"  Ch. {cn}  {c['name']}")
            if len(chapters) > 10:
                print(f"  ... and {len(chapters) - 10} more")
            return 0

        # Queue
        chapter_ids = [c["id"] for c in chapters]
        batches = [
            chapter_ids[i : i + batch_size]
            for i in range(0, len(chapter_ids), batch_size)
        ]
        print(
            f"\nQueuing {len(chapter_ids)} chapters in {len(batches)} batches "
            f"(batch={batch_size}, delay={delay}s)"
        )

        for i, batch in enumerate(batches, 1):
            state = await _queue_batch(client, batch)
            print(f"[{i}/{len(batches)}] Queued {len(batch)} chapters → {state}")
            if i < len(batches):
                for remaining in range(delay, 0, -30):
                    print(f"  Waiting {remaining}s...")
                    time.sleep(min(30, remaining))
                print()

        print(f"\nDone — {len(chapter_ids)} chapters queued. Suwayomi is downloading them.")
        print(f"Check: ls /downloads/  (or your SUWAYOMI_DOWNLOADS_DIR)")
        return 0


# ── CLI entrypoint ──────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Batch download manga chapters from Suwayomi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  docker exec michibiki python -m src.download "Omniscient Reader" --all
  docker exec michibiki python -m src.download "One Piece" --range 1-100
  docker exec michibiki python -m src.download --id 42 --all --dry-run""",
    )
    parser.add_argument("title", nargs="?", help="Manga title (fuzzy match)")
    parser.add_argument("--id", type=int, dest="manga_id", help="Suwayomi manga ID")
    parser.add_argument(
        "--all", action="store_true", dest="all_chapters", help="Download all chapters"
    )
    parser.add_argument(
        "--range", dest="chapter_range", help="Chapter range (e.g. '1-50')"
    )
    parser.add_argument(
        "--limit", type=int, help="Download only the first N chapters"
    )
    parser.add_argument(
        "--batch-size", type=int, default=30, help="Chapters per batch (default: 30)"
    )
    parser.add_argument(
        "--delay", type=int, default=180, help="Seconds between batches (default: 180)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be downloaded"
    )
    args = parser.parse_args()

    sys.exit(
        asyncio.run(
            download(
                title=args.title,
                manga_id=args.manga_id,
                all_chapters=args.all_chapters,
                chapter_range=args.chapter_range,
                batch_size=args.batch_size,
                delay=args.delay,
                dry_run=args.dry_run,
                limit=args.limit,
            )
        )
    )


if __name__ == "__main__":
    main()
