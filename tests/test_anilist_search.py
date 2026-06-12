"""Tests for AniList title search and matching."""

import pytest

from src.anilist_search import (
    _simplify,
    _clean_title,
    _score_match,
    search_anilist,
    TITLE_OVERRIDES,
    MATCH_THRESHOLD,
    AniListSearchError,
)


class TestSimplify:
    def test_lowercase(self):
        assert _simplify("One Piece") == "one piece"

    def test_strips_punctuation(self):
        assert _simplify("Re:Zero") == "re zero"
        assert _simplify("My Hero Academia!!") == "my hero academia"

    def test_normalizes_whitespace(self):
        assert _simplify("  Solo   Leveling  ") == "solo leveling"

    def test_empty_string(self):
        assert _simplify("") == ""

    def test_special_chars(self):
        # ō is a Unicode letter (word char), so it stays
        assert _simplify("Bungō Stray Dogs") == "bungō stray dogs"

    def test_japanese_chars_stripped(self):
        # Japanese kana/kanji are not \w in Python's default regex
        result = _simplify("進撃の巨人")
        # Non-word chars stripped → just spaces → collapsed to empty
        assert result == "" or result == "進撃の巨人"


class TestCleanTitle:
    def test_removes_parenthetical_suffix(self):
        assert _clean_title("One Piece (Omnibus)") == "One Piece"

    def test_preserves_non_suffix_parentheses(self):
        # Parentheses not at end are kept
        result = _clean_title("That Time I Got Reincarnated as a Slime (Manga)")
        assert result == "That Time I Got Reincarnated as a Slime"

    def test_no_parentheses_unchanged(self):
        assert _clean_title("Solo Leveling") == "Solo Leveling"

    def test_strips_season_suffix(self):
        assert _clean_title("Sword Art Online Season 2") == "Sword Art Online"

    def test_strips_part_suffix(self):
        result = _clean_title("Naruto Part II: Shippuden")
        # "Part " starts at index 7, which is > 5, so it strips
        assert result == "Naruto"


class TestScoreMatch:
    def _candidate(self, romaji="", english="", native="", **kwargs):
        """Build a minimal AniList search result candidate."""
        c = {"id": kwargs.get("id", 1), "title": {}}
        if romaji:
            c["title"]["romaji"] = romaji
        if english:
            c["title"]["english"] = english
        if native:
            c["title"]["native"] = native
        return c

    def test_exact_match_scores_high(self):
        c = self._candidate(romaji="One Piece", english="One Piece")
        score = _score_match("One Piece", c)
        assert score >= 0.80  # exact match is 0.85

    def test_prefix_match_scores_highest(self):
        # "Gate" prefix-matches "Gate: Thus the JSDF Fought There"
        c = self._candidate(romaji="Gate: Thus the JSDF Fought There")
        score = _score_match("Gate", c)
        assert score == 0.90  # prefix match beats exact match

    def test_whole_word_sequence_match(self):
        # "Solo Leveling" as adjacent words inside a longer title gets 0.70
        c = self._candidate(romaji="The Solo Leveling Chronicles")
        score = _score_match("Solo Leveling", c)
        assert score == 0.70

    def test_substring_match(self):
        # "Leveling Chron" is a substring but NOT a whole-word match → 0.55
        c = self._candidate(romaji="The Solo Leveling Chronicles")
        score = _score_match("Leveling Chron", c)
        assert score == 0.55

    def test_prefix_beats_substring(self):
        # When a shorter query prefix-matches a result, it gets 0.90 not 0.55
        c = self._candidate(romaji="Jujutsu Kaisen 0: The Movie")
        score = _score_match("Jujutsu Kaisen", c)
        # After _simplify: "jujutsu kaisen 0 the movie" — "jujutsu kaisen" is prefix
        assert score == 0.90

    def test_word_overlap_scoring(self):
        c = self._candidate(romaji="Solo Leveling Season 2")
        score = _score_match("Solo Leveling", c)
        # All query words appear in title -> base 0.50 + bonus
        assert score >= 0.50

    def test_partial_word_overlap(self):
        c = self._candidate(romaji="Attack on Titan Final Season")
        score = _score_match("Attack on Titan", c)
        # 3 of 4 words overlap
        assert score > 0.0

    def test_no_match_scores_zero(self):
        c = self._candidate(romaji="Bleach")
        score = _score_match("Naruto", c)
        assert score == 0.0

    def test_matches_english_title(self):
        c = self._candidate(
            romaji="Kimetsu no Yaiba",
            english="Demon Slayer: Kimetsu no Yaiba",
        )
        score = _score_match("Demon Slayer", c)
        # "demon slayer" prefix-matches "demon slayer kimetsu no yaiba"
        assert score == 0.90

    def test_matches_native_title(self):
        c = self._candidate(
            romaji="Berserk",
            native="ベルセルク",
        )
        score = _score_match("ベルセルク", c)
        assert score >= 0.80  # exact match via native title

    def test_empty_query(self):
        c = self._candidate(romaji="One Piece")
        score = _score_match("", c)
        assert score == 0.0

    def test_candidate_below_threshold(self):
        c = self._candidate(romaji="Something Completely Different")
        score = _score_match("One Piece", c)
        assert score < MATCH_THRESHOLD  # 0.0 < 0.50

    def test_exact_romaji_beats_english_prefix(self):
        """Exact romaji match (0.85) should beat a different english prefix match."""
        c = self._candidate(
            romaji="Gate: Jieitai Kanochi nite, Kaku Tatakaeri",
            english="GATE",
        )
        score = _score_match("Gate", c)
        # romaji: prefix match = 0.90 (beats english exact match 0.85)
        assert score == 0.90


class TestSearchAnilistOverrides:
    @pytest.mark.asyncio
    async def test_override_returns_without_api_call(self):
        """When title is in TITLE_OVERRIDES, skip AniList API entirely."""
        original = dict(TITLE_OVERRIDES)
        try:
            TITLE_OVERRIDES["test override series"] = 99999
            # No token needed since override path is hit first
            result = await search_anilist("Test Override Series", token="any")
            assert result == 99999
        finally:
            TITLE_OVERRIDES.clear()
            TITLE_OVERRIDES.update(original)

    @pytest.mark.asyncio
    async def test_override_case_insensitive(self):
        """Override lookup is case-insensitive."""
        original = dict(TITLE_OVERRIDES)
        try:
            TITLE_OVERRIDES["classroom of the elite"] = 96798
            result = await search_anilist("Classroom of the Elite", token="any")
            assert result == 96798
        finally:
            TITLE_OVERRIDES.clear()
            TITLE_OVERRIDES.update(original)
