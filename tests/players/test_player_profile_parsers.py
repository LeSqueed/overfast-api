"""Unit tests for player_profile parser module"""

from unittest.mock import Mock, patch

import pytest
from fastapi import status

from app.adapters.blizzard import BlizzardClient
from app.domain.enums import PlayerGamemode, PlayerPlatform
from app.domain.exceptions import ParserBlizzardError
from app.domain.parsers.player_profile import (
    _get_player_hero_keys,
    fetch_player_html,
    filter_all_stats_data,
    filter_stats_by_query,
    parse_player_profile_html,
    validate_hero_filter,
)
from tests.helpers import read_html_file

_TEKROP_HTML = read_html_file("players/TeKrop-2217.html") or ""

# A stats structure mirroring parser output (string keys, not enum keys)
_PC_KEY = PlayerPlatform.PC.value
_CONSOLE_KEY = PlayerPlatform.CONSOLE.value
_QP_KEY = PlayerGamemode.QUICKPLAY.value
_COMP_KEY = PlayerGamemode.COMPETITIVE.value

_HERO_STATS = [
    {
        "category": "combat",
        "label": "Combat",
        "stats": [{"key": "eliminations", "label": "Eliminations", "value": 10}],
    }
]

_FULL_STATS = {
    _PC_KEY: {
        _QP_KEY: {
            "heroes_comparisons": {},
            "career_stats": {"tracer": _HERO_STATS, "genji": _HERO_STATS},
        },
        _COMP_KEY: None,
    },
    _CONSOLE_KEY: None,
}


# ---------------------------------------------------------------------------
# filter_stats_by_query
# ---------------------------------------------------------------------------


class TestFilterStatsByQuery:
    def test_no_platform_no_stats_returns_empty(self):
        """When all platform data is None, returns {}."""
        stats = {_PC_KEY: None, _CONSOLE_KEY: None}

        result = filter_stats_by_query(stats, PlayerGamemode.QUICKPLAY)

        assert result == {}

    def test_none_stats_returns_empty(self):
        """None input returns {}."""
        result = filter_stats_by_query(None, PlayerGamemode.QUICKPLAY)

        assert result == {}

    def test_explicit_platform_and_gamemode(self):
        """With explicit platform+gamemode, returns career_stats dict."""
        result = filter_stats_by_query(
            _FULL_STATS,
            platform=PlayerPlatform.PC,
            gamemode=PlayerGamemode.QUICKPLAY,
        )

        assert "tracer" in result
        assert "genji" in result

    def test_hero_filter(self):
        """Hero filter restricts to specific hero."""
        result = filter_stats_by_query(
            _FULL_STATS,
            platform=PlayerPlatform.PC,
            gamemode=PlayerGamemode.QUICKPLAY,
            hero="tracer",
        )

        assert result == {"tracer": _HERO_STATS}

    def test_hero_filter_no_match(self):
        """Hero filter with no match returns empty dict."""
        result = filter_stats_by_query(
            _FULL_STATS,
            platform=PlayerPlatform.PC,
            gamemode=PlayerGamemode.QUICKPLAY,
            hero="mercy",
        )

        assert result == {}

    def test_platform_with_no_gamemode_data_returns_empty(self):
        """When gamemode data is None, returns {}."""
        stats = {
            _PC_KEY: {_QP_KEY: None, _COMP_KEY: None},
            _CONSOLE_KEY: None,
        }

        result = filter_stats_by_query(
            stats, platform=PlayerPlatform.PC, gamemode=PlayerGamemode.QUICKPLAY
        )

        assert result == {}

    def test_string_platform_and_gamemode(self):
        """String values for platform/gamemode are handled via str() fallback."""
        result = filter_stats_by_query(
            _FULL_STATS,
            platform=_PC_KEY,  # str, not enum
            gamemode=_QP_KEY,
        )

        assert "tracer" in result

    def test_auto_detect_platform_with_gamemode(self):
        """Without explicit platform, first non-None platform is auto-detected and correct gamemode slice is returned."""
        # Provide stats for multiple platforms and rely on auto-detection to prefer PC.
        result = filter_stats_by_query(
            _FULL_STATS,
            PlayerGamemode.QUICKPLAY,
            platform=None,
        )

        # Expect the same data as if we had explicitly requested the PC platform.
        expected = filter_stats_by_query(
            _FULL_STATS,
            PlayerGamemode.QUICKPLAY,
            platform=PlayerPlatform.PC,
        )

        assert result == expected


# ---------------------------------------------------------------------------
# filter_all_stats_data
# ---------------------------------------------------------------------------


class TestFilterAllStatsData:
    def test_none_stats_returns_none(self):
        """None input → None."""
        result = filter_all_stats_data(None)

        assert result is None

    def test_empty_stats_returns_none(self):
        """Empty dict → None."""
        result = filter_all_stats_data({})

        assert result is None

    def test_all_none_values_returns_none(self):
        """Dict with all-None values → None."""
        result = filter_all_stats_data({_PC_KEY: None, _CONSOLE_KEY: None})

        assert result is None

    def test_no_filters_returns_both_platforms(self):
        """Without filters, returns both platform keys."""
        result = filter_all_stats_data(_FULL_STATS)

        assert result is not None
        assert _PC_KEY in result
        assert _CONSOLE_KEY in result

    def test_platform_filter_nulls_other_platform(self):
        """Platform filter sets non-matching platform to None."""
        result = filter_all_stats_data(_FULL_STATS, platform=PlayerPlatform.PC)

        assert result is not None
        assert result[_CONSOLE_KEY] is None
        assert result[_PC_KEY] is not None

    def test_platform_filter_console_nulls_pc(self):
        """Console filter sets PC to None."""
        result = filter_all_stats_data(_FULL_STATS, platform=PlayerPlatform.CONSOLE)

        assert result is not None
        assert result[_PC_KEY] is None
        assert result[_CONSOLE_KEY] is None  # no console data

    def test_gamemode_filter_nulls_other_gamemodes(self):
        """Gamemode filter keeps only matching gamemode per platform."""
        result = filter_all_stats_data(
            _FULL_STATS,
            gamemode=PlayerGamemode.QUICKPLAY,
        )

        assert result is not None
        pc_data = result[_PC_KEY]
        assert pc_data is not None
        assert pc_data[_QP_KEY] is not None
        assert pc_data[_COMP_KEY] is None

    def test_platform_and_gamemode_filter(self):
        """Both filters applied."""
        result = filter_all_stats_data(
            _FULL_STATS,
            platform=PlayerPlatform.PC,
            gamemode=PlayerGamemode.QUICKPLAY,
        )

        assert result is not None
        assert result[_CONSOLE_KEY] is None
        pc_data = result[_PC_KEY]
        assert pc_data is not None
        assert pc_data[_QP_KEY] is not None
        assert pc_data[_COMP_KEY] is None

    def test_string_filters(self):
        """String values for platform/gamemode are handled."""
        result = filter_all_stats_data(
            _FULL_STATS,
            platform=_PC_KEY,
            gamemode=_QP_KEY,
        )

        assert result is not None
        assert result[_PC_KEY] is not None

    def test_platform_data_is_none_stays_none(self):
        """If platform data is None in stats, stays None after filter."""
        result = filter_all_stats_data(_FULL_STATS, gamemode=PlayerGamemode.QUICKPLAY)

        assert result is not None
        assert result[_CONSOLE_KEY] is None


# ---------------------------------------------------------------------------
# parse_player_profile_html — edge cases
# ---------------------------------------------------------------------------


class TestParsePlayerProfileHtml:
    def test_real_fixture_returns_summary_and_stats(self):
        """Real HTML fixture produces valid summary+stats."""
        result = parse_player_profile_html(_TEKROP_HTML)

        assert "summary" in result
        assert "stats" in result

    def test_player_not_found_raises(self):
        """HTML without Profile-masthead raises ParserBlizzardError."""
        minimal_html = (
            "<html><body><main class='main-content'><div></div></main></body></html>"
        )

        with pytest.raises(ParserBlizzardError):
            parse_player_profile_html(minimal_html)

    def test_player_summary_overrides_avatar(self):
        """player_summary avatar overrides HTML avatar."""
        result = parse_player_profile_html(
            _TEKROP_HTML,
            player_summary={"avatar": "https://example.com/avatar.png"},
        )

        assert result["summary"]["avatar"] == "https://example.com/avatar.png"


# ---------------------------------------------------------------------------
# fetch_player_html — URL building
# ---------------------------------------------------------------------------


class TestFetchPlayerHtml:
    @pytest.mark.asyncio
    async def test_battletag_player_id_is_url_quoted(self):
        """A raw BattleTag player_id is percent-quoted in the request URL."""
        mock_response = Mock(status_code=status.HTTP_200_OK, url="https://example.com")

        with patch("httpx2.AsyncClient.get", return_value=mock_response) as mock_get:
            client = BlizzardClient()
            await fetch_player_html(client, "TeKrop/../secrets")

        requested_url = mock_get.call_args.args[0]
        assert "TeKrop%2F..%2Fsecrets" in requested_url

    @pytest.mark.asyncio
    async def test_blizzard_id_player_id_is_not_double_encoded(self):
        """A player_id already in %7C-encoded Blizzard ID form isn't double-quoted."""
        blizzard_id = "df51a381fe20caf8baa7%7C0bf3b4c47cbebe84b8db9c676a4e9c1f"
        mock_response = Mock(status_code=status.HTTP_200_OK, url="https://example.com")

        with patch("httpx2.AsyncClient.get", return_value=mock_response) as mock_get:
            client = BlizzardClient()
            await fetch_player_html(client, blizzard_id)

        requested_url = mock_get.call_args.args[0]
        assert requested_url.endswith(f"/{blizzard_id}/")
        assert "%25" not in requested_url


# ---------------------------------------------------------------------------
# validate_hero_filter
# ---------------------------------------------------------------------------

_STATS_WITH_BRAND_NEW_HERO = {
    _PC_KEY: {
        _QP_KEY: {
            "heroes_comparisons": {},
            "career_stats": {"brand-new-hero": _HERO_STATS},
        },
        _COMP_KEY: None,
    },
    _CONSOLE_KEY: None,
}


class TestGetPlayerHeroKeys:
    def test_collects_keys_across_platforms_and_gamemodes(self):
        """Keys are gathered from every section, not only the requested one."""
        stats = {
            _PC_KEY: {
                _QP_KEY: {"career_stats": {"tracer": _HERO_STATS}},
                _COMP_KEY: {"career_stats": {"genji": _HERO_STATS}},
            },
            _CONSOLE_KEY: {
                _QP_KEY: {"career_stats": {"mercy": _HERO_STATS}},
                _COMP_KEY: None,
            },
        }

        result = _get_player_hero_keys(stats)

        assert result == {"tracer", "genji", "mercy"}

    def test_none_stats_returns_empty_set(self):
        """Missing stats yield no hero keys."""
        result = _get_player_hero_keys(None)

        assert result == set()


class TestValidateHeroFilter:
    def test_no_hero_filter_is_accepted(self):
        """No hero filter at all is always valid."""
        assert validate_hero_filter(_FULL_STATS, None) is None

    def test_known_hero_never_played_is_accepted(self):
        """A heroes.csv key the player never played is a truthful empty result."""
        assert validate_hero_filter(_FULL_STATS, "mercy") is None

    def test_all_heroes_pseudo_key_is_accepted(self):
        """The all-heroes pseudo key is a valid filter."""
        assert validate_hero_filter(_FULL_STATS, "all-heroes") is None

    def test_hero_not_in_csv_but_played_is_accepted(self):
        """A hero released before heroes.csv caught up stays usable."""
        assert (
            validate_hero_filter(_STATS_WITH_BRAND_NEW_HERO, "brand-new-hero") is None
        )

    def test_unknown_hero_raises_bad_request(self):
        """A key neither known nor played is rejected instead of returning {}."""
        with pytest.raises(ParserBlizzardError) as exc_info:
            validate_hero_filter(_FULL_STATS, "anaa")

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_unknown_hero_raises_even_without_any_stats(self):
        """Validation doesn't depend on the player having any stats at all."""
        with pytest.raises(ParserBlizzardError):
            validate_hero_filter(None, "anaa")


class TestFilterStatsByQueryHeroValidation:
    def test_hero_not_in_csv_but_played_is_returned(self):
        """The leniency reaches the caller, not only the validation step."""
        result = filter_stats_by_query(
            _STATS_WITH_BRAND_NEW_HERO,
            platform=PlayerPlatform.PC,
            gamemode=PlayerGamemode.QUICKPLAY,
            hero="brand-new-hero",
        )

        assert result == {"brand-new-hero": _HERO_STATS}

    def test_played_hero_stays_valid_for_a_gamemode_it_was_not_played_in(self):
        """Scanning all sections keeps a QP-only hero an empty 200, not a 400."""
        result = filter_stats_by_query(
            _STATS_WITH_BRAND_NEW_HERO,
            platform=PlayerPlatform.PC,
            gamemode=PlayerGamemode.COMPETITIVE,
            hero="brand-new-hero",
        )

        assert result == {}

    def test_unknown_hero_raises_before_any_early_return(self):
        """Rejection happens even when the filtered slice would be empty anyway."""
        with pytest.raises(ParserBlizzardError):
            filter_stats_by_query(
                _FULL_STATS,
                platform=PlayerPlatform.CONSOLE,
                gamemode=PlayerGamemode.COMPETITIVE,
                hero="anaa",
            )
