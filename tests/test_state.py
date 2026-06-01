"""Tests for SQLite state store."""

import pytest
from src.state import StateStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "michibiki.db"
    return StateStore(str(db_path))


def test_new_store_creates_table(store):
    """Store initializes with the manga_state table."""
    store._conn.execute("SELECT COUNT(*) FROM manga_state")


def test_upsert_and_retrieve(store):
    """Insert a manga and read it back."""
    store.upsert_progress(
        anilist_id=21, suwayomi_id=42, title="One Piece", chapter_read=1100.0
    )
    states = store.get_all_states()
    assert len(states) == 1
    assert states[21].title == "One Piece"
    assert states[21].last_chapter_read == 1100.0


def test_needs_update_true_when_changed(store):
    """Return True when current chapter differs from last synced."""
    store.mark_synced(21, 42, "One Piece", 1100.0)
    assert store.needs_update(anilist_id=21, current_chapter=1105.0) is True


def test_needs_update_false_when_unchanged(store):
    """Return False when chapter hasn't changed."""
    store.mark_synced(21, 42, "One Piece", 1100.0)
    assert store.needs_update(anilist_id=21, current_chapter=1100.0) is False


def test_needs_update_true_for_new_entry(store):
    """New manga (no prior state) always needs update."""
    assert store.needs_update(anilist_id=99, current_chapter=5.0) is True


def test_needs_update_none_vs_value(store):
    """None vs a value is a change."""
    store.mark_synced(21, 42, "Test", None)
    assert store.needs_update(21, 1.0) is True
    assert store.needs_update(21, None) is False


def test_mark_synced_updates_state(store):
    """mark_synced should update the synced chapter value."""
    store.mark_synced(21, 42, "One Piece", 1100.0)
    before = store.get_all_states()[21].last_synced_chapter
    assert before == 1100.0
    store.mark_synced(21, 42, "One Piece", 1105.0)
    after = store.get_all_states()[21].last_synced_chapter
    assert after == 1105.0
    assert after != before


def test_upsert_updates_existing(store):
    """Upserting an existing manga updates its fields."""
    store.upsert_progress(21, 42, "Old Title", 100.0)
    store.upsert_progress(21, 42, "New Title", 200.0)
    state = store.get_all_states()[21]
    assert state.title == "New Title"
    assert state.last_chapter_read == 200.0
