"""SQLite state store — tracks last-synced progress per manga."""

import os
import sqlite3
from dataclasses import dataclass


@dataclass
class MangaState:
    anilist_media_id: int
    suwayomi_manga_id: int
    title: str
    last_chapter_read: float | None
    last_synced_chapter: float | None
    last_synced_at: str


class StateStore:
    def __init__(self, db_path: str = "data/michibiki.db"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS manga_state (
                anilist_media_id INTEGER PRIMARY KEY,
                suwayomi_manga_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                last_chapter_read REAL,
                last_synced_chapter REAL,
                last_synced_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        self._conn.commit()

    def upsert_progress(
        self,
        anilist_id: int,
        suwayomi_id: int,
        title: str,
        chapter_read: float | None,
    ):
        """Record current reading progress from Suwayomi."""
        self._conn.execute(
            """
            INSERT INTO manga_state
                (anilist_media_id, suwayomi_manga_id, title,
                 last_chapter_read, last_synced_chapter, last_synced_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(anilist_media_id) DO UPDATE SET
                suwayomi_manga_id = excluded.suwayomi_manga_id,
                title = excluded.title,
                last_chapter_read = excluded.last_chapter_read
            """,
            (anilist_id, suwayomi_id, title, chapter_read, chapter_read),
        )
        self._conn.commit()

    def mark_synced(
        self,
        anilist_id: int,
        suwayomi_id: int,
        title: str,
        synced_chapter: float | None,
    ):
        """Record that a chapter has been pushed to AniList. Inserts if new."""
        self._conn.execute(
            """
            INSERT INTO manga_state
                (anilist_media_id, suwayomi_manga_id, title,
                 last_chapter_read, last_synced_chapter, last_synced_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(anilist_media_id) DO UPDATE SET
                suwayomi_manga_id = excluded.suwayomi_manga_id,
                title = excluded.title,
                last_chapter_read = excluded.last_chapter_read,
                last_synced_chapter = excluded.last_synced_chapter,
                last_synced_at = excluded.last_synced_at
            """,
            (anilist_id, suwayomi_id, title, synced_chapter, synced_chapter),
        )
        self._conn.commit()

    def needs_update(self, anilist_id: int, current_chapter: float | None) -> bool:
        """True if current chapter differs from what we last synced."""
        row = self._conn.execute(
            "SELECT last_synced_chapter FROM manga_state WHERE anilist_media_id = ?",
            (anilist_id,),
        ).fetchone()
        if row is None:
            return True
        return row["last_synced_chapter"] != current_chapter

    def get_all_states(self) -> dict[int, MangaState]:
        """Return all tracked manga as {anilist_media_id: MangaState}."""
        rows = self._conn.execute("SELECT * FROM manga_state").fetchall()
        return {
            row["anilist_media_id"]: MangaState(
                anilist_media_id=row["anilist_media_id"],
                suwayomi_manga_id=row["suwayomi_manga_id"],
                title=row["title"],
                last_chapter_read=row["last_chapter_read"],
                last_synced_chapter=row["last_synced_chapter"],
                last_synced_at=row["last_synced_at"],
            )
            for row in rows
        }
