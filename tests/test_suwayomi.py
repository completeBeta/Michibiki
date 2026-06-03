"""Tests for Suwayomi GraphQL client."""

import pytest
from src.suwayomi import SuwayomiClient, MangaProgress


@pytest.fixture
def client():
    return SuwayomiClient("http://fake:4567/api/graphql")


def test_parse_manga_with_anilist_tracker(client):
    """Extracts MangaProgress when AniList track record exists."""
    sample = {
        "data": {
            "mangas": {
                "nodes": [
                    {
                        "id": 42,
                        "title": "One Piece",
                        "status": "ONGOING",
                        "trackRecords": {
                            "nodes": [
                                {
                                    "id": 1,
                                    "trackerId": 7,
                                    "remoteId": "21",
                                    "lastChapterRead": 1100.0,
                                    "totalChapters": 0,
                                    "tracker": {"name": "AniList"},
                                }
                            ]
                        },
                        "lastReadChapter": {
                            "chapterNumber": 1105.0,
                            "lastReadAt": "1717200000000",
                        },
                    }
                ]
            }
        }
    }
    results = client._parse_manga_progress(sample)
    assert len(results) == 1
    r = results[0]
    assert r.manga_id == 42
    assert r.title == "One Piece"
    assert r.anilist_media_id == 21
    assert r.last_chapter_read == 1100.0
    assert r.highest_read_chapter == 1105.0


def test_skips_manga_without_anilist_tracker(client):
    """Manga with only MAL/other trackers should be skipped."""
    sample = {
        "data": {
            "mangas": {
                "nodes": [
                    {
                        "id": 1,
                        "title": "MyAnimeList Only",
                        "status": "ONGOING",
                        "trackRecords": {
                            "nodes": [
                                {
                                    "id": 2,
                                    "trackerId": 1,
                                    "remoteId": "123",
                                    "lastChapterRead": 5.0,
                                    "tracker": {"name": "MyAnimeList"},
                                }
                            ]
                        },
                        "lastReadChapter": None,
                    }
                ]
            }
        }
    }
    results = client._parse_manga_progress(sample)
    assert len(results) == 0


def test_skips_manga_with_no_trackers(client):
    """Untracked manga should be skipped entirely."""
    sample = {
        "data": {
            "mangas": {
                "nodes": [
                    {
                        "id": 2,
                        "title": "No Trackers",
                        "status": "ONGOING",
                        "trackRecords": {"nodes": []},
                        "lastReadChapter": None,
                    }
                ]
            }
        }
    }
    results = client._parse_manga_progress(sample)
    assert len(results) == 0


def test_handles_no_chapters_read(client):
    """Newly added manga with no chapters read yet."""
    sample = {
        "data": {
            "mangas": {
                "nodes": [
                    {
                        "id": 3,
                        "title": "Fresh Start",
                        "status": "ONGOING",
                        "trackRecords": {
                            "nodes": [
                                {
                                    "id": 3,
                                    "remoteId": "99",
                                    "lastChapterRead": 0.0,
                                    "tracker": {"name": "AniList"},
                                }
                            ]
                        },
                        "lastReadChapter": None,
                    }
                ]
            }
        }
    }
    results = client._parse_manga_progress(sample)
    assert len(results) == 1
    assert results[0].last_chapter_read == 0.0
    assert results[0].highest_read_chapter is None


def test_multiple_manga_mixed(client):
    """Mix of tracked and untracked manga."""
    sample = {
        "data": {
            "mangas": {
                "nodes": [
                    {
                        "id": 10,
                        "title": "Tracked",
                        "status": "ONGOING",
                        "trackRecords": {
                            "nodes": [
                                {
                                    "id": 10,
                                    "remoteId": "100",
                                    "lastChapterRead": 42.0,
                                    "tracker": {"name": "AniList"},
                                }
                            ]
                        },
                        "lastReadChapter": None,
                    },
                    {
                        "id": 11,
                        "title": "Untracked",
                        "status": "COMPLETED",
                        "trackRecords": {"nodes": []},
                        "lastReadChapter": None,
                    },
                    {
                        "id": 12,
                        "title": "Also Tracked",
                        "status": "ONGOING",
                        "trackRecords": {
                            "nodes": [
                                {
                                    "id": 12,
                                    "remoteId": "200",
                                    "lastChapterRead": 10.0,
                                    "tracker": {"name": "AniList"},
                                }
                            ]
                        },
                        "lastReadChapter": None,
                    },
                ]
            }
        }
    }
    results = client._parse_manga_progress(sample)
    assert len(results) == 2
    ids = {r.manga_id for r in results}
    assert ids == {10, 12}
