from typing import Any, ClassVar, cast
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.enums import (
    CompetitiveDivisionHistoryFilter,
    Locale,
    MapKey,
    PlayerGamemode,
    PlayerPlatform,
    PlayerRegion,
)
from app.domain.exceptions import (
    InvalidGamemodeFilterError,
    ParserBlizzardError,
    ParserInternalError,
    ParserParsingError,
)
from app.domain.parsers.maps import (
    COMPETITIVE_KEYS_STORAGE_KEY,
    encode_competitive_keys,
)
from app.domain.services.hero_service import (
    HERO_STATS_SNAPSHOT_SLOT_SECONDS,
    HeroService,
    dict_insert_value_before_key,
    hero_stats_snapshot_slot,
)
from tests.helpers import read_html_file

# An exact UTC-day boundary, so it is its own snapshot slot.
_SNAPSHOT_SLOT = 1700006400


def _make_hero_service() -> HeroService:
    cache = AsyncMock()
    cache.get.return_value = None
    storage = AsyncMock()
    storage.get_static_data.return_value = None
    blizzard_client = AsyncMock()
    task_queue = AsyncMock()
    task_queue.is_job_pending_or_running.return_value = False
    return HeroService(cache, storage, blizzard_client, task_queue)


class TestHeroServiceListHeroesParseError:
    def test_parse_raises_parser_internal_error_on_parser_parsing_error(self):
        svc = _make_hero_service()
        config = svc._heroes_list_config(Locale.ENGLISH_US, "/heroes")
        parser = config.parser
        assert parser is not None

        with (
            patch(
                "app.domain.services.hero_service.parse_heroes_html",
                side_effect=ParserParsingError("bad HTML"),
            ),
            pytest.raises(ParserInternalError) as exc_info,
        ):
            parser("<bad-html>")

        assert str(Locale.ENGLISH_US) in exc_info.value.blizzard_url


class TestHeroServiceGetHeroStatsParseError:
    @pytest.mark.asyncio
    async def test_get_hero_stats_raises_parser_internal_error_on_parser_parsing_error(
        self,
    ):
        svc = _make_hero_service()

        with (
            patch(
                "app.domain.services.hero_service.parse_hero_stats_summary",
                side_effect=ParserParsingError("unexpected JSON"),
            ),
            pytest.raises(ParserInternalError),
        ):
            await svc.get_hero_stats(
                platform=PlayerPlatform.PC,
                gamemode=PlayerGamemode.QUICKPLAY,
                region=PlayerRegion.EUROPE,
                role=None,
                map_filter=None,
                competitive_division=None,
                order_by="hero:asc",
                cache_key="/heroes/stats",
            )


class TestHeroServiceGetHeroStatsGamemodeFilter:
    _base_kwargs: ClassVar[dict] = {
        "platform": PlayerPlatform.PC,
        "gamemode": PlayerGamemode.COMPETITIVE,
        "region": PlayerRegion.EUROPE,
        "role": None,
        "map_filter": None,
        "competitive_division": None,
        "order_by": "hero:asc",
        "cache_key": "/heroes/stats",
    }

    @pytest.mark.asyncio
    async def test_retries_on_invalid_gamemode_filter(self):
        svc = _make_hero_service()
        expected = [{"hero": "ana", "pickrate": 0.1, "winrate": 0.5}]

        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            side_effect=[
                InvalidGamemodeFilterError("filter '1' != selected '2'"),
                expected,
            ],
        ) as mock_parse:
            data, _, _ = await svc.get_hero_stats(**self._base_kwargs)

        assert data == expected
        assert mock_parse.call_count == 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_raises_parser_internal_error_when_all_filters_exhausted(self):
        svc = _make_hero_service()

        with (
            patch(
                "app.domain.services.hero_service.parse_hero_stats_summary",
                side_effect=InvalidGamemodeFilterError("no valid filter found"),
            ),
            pytest.raises(ParserInternalError),
        ):
            await svc.get_hero_stats(**self._base_kwargs)

    @pytest.mark.asyncio
    async def test_returns_empty_list_without_retry(self):
        svc = _make_hero_service()

        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            return_value=[],
        ) as mock_parse:
            data, _, _ = await svc.get_hero_stats(**self._base_kwargs)

        assert data == []
        assert mock_parse.call_count == 1


class TestHeroServiceGamemodeFilterCaching:
    _base_kwargs: ClassVar[dict] = {
        "platform": PlayerPlatform.PC,
        "gamemode": PlayerGamemode.COMPETITIVE,
        "region": PlayerRegion.EUROPE,
        "role": None,
        "map_filter": None,
        "competitive_division": None,
        "order_by": "hero:asc",
        "cache_key": "/heroes/stats",
    }

    @pytest.mark.asyncio
    async def test_cached_filter_is_tried_first(self):
        svc = _make_hero_service()
        cast("Any", svc.cache).get_gamemode_filter.return_value = "2"
        expected = [{"hero": "ana"}]

        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            return_value=expected,
        ) as mock_parse:
            data, _, _ = await svc.get_hero_stats(**self._base_kwargs)

        assert mock_parse.call_count == 1
        assert mock_parse.call_args.kwargs["gamemode_filter"] == "2"
        assert data == expected

    @pytest.mark.asyncio
    async def test_no_cache_writes_working_filter(self):
        svc = _make_hero_service()
        cast("Any", svc.cache).get_gamemode_filter.return_value = None

        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            return_value=[],
        ):
            await svc.get_hero_stats(**self._base_kwargs)

        cast("Any", svc.cache).set_gamemode_filter.assert_awaited_once_with(
            PlayerGamemode.COMPETITIVE, "1"
        )

    @pytest.mark.asyncio
    async def test_stale_cached_filter_falls_back_and_updates_cache(self):
        svc = _make_hero_service()
        cast("Any", svc.cache).get_gamemode_filter.return_value = "2"
        expected = [{"hero": "ana"}]

        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            side_effect=[
                InvalidGamemodeFilterError("filter '2' != selected '1'"),
                expected,
            ],
        ) as mock_parse:
            data, _, _ = await svc.get_hero_stats(**self._base_kwargs)

        assert mock_parse.call_count == 2  # noqa: PLR2004
        assert data == expected
        cast("Any", svc.cache).set_gamemode_filter.assert_awaited_once_with(
            PlayerGamemode.COMPETITIVE, "1"
        )

    @pytest.mark.asyncio
    async def test_filter_written_to_cache_unconditionally(self):
        svc = _make_hero_service()
        cast("Any", svc.cache).get_gamemode_filter.return_value = "1"

        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            return_value=[],
        ):
            await svc.get_hero_stats(**self._base_kwargs)

        cast("Any", svc.cache).set_gamemode_filter.assert_awaited_once_with(
            PlayerGamemode.COMPETITIVE, "1"
        )

    @pytest.mark.asyncio
    async def test_quickplay_single_filter_cached_and_written(self):
        svc = _make_hero_service()
        cast("Any", svc.cache).get_gamemode_filter.return_value = None

        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            return_value=[],
        ):
            await svc.get_hero_stats(
                platform=PlayerPlatform.PC,
                gamemode=PlayerGamemode.QUICKPLAY,
                region=PlayerRegion.EUROPE,
                role=None,
                map_filter=None,
                competitive_division=None,
                order_by="hero:asc",
                cache_key="/heroes/stats",
            )

        cast("Any", svc.cache).set_gamemode_filter.assert_awaited_once_with(
            PlayerGamemode.QUICKPLAY, "0"
        )


@pytest.mark.parametrize(
    ("input_dict", "key", "new_key", "new_value"),
    [
        # Empty dict
        ({}, "key", "new_key", "new_value"),
        # Key doesn't exist
        ({"key_one": 1, "key_two": 2}, "key", "new_key", "new_value"),
    ],
)
def test_dict_insert_value_before_key_with_key_error(
    input_dict: dict,
    key: str,
    new_key: str,
    new_value: Any,
):
    with pytest.raises(KeyError):
        dict_insert_value_before_key(input_dict, key, new_key, new_value)


@pytest.mark.parametrize(
    ("input_dict", "key", "new_key", "new_value", "result_dict"),
    [
        # Before first key
        (
            {"key_one": 1, "key_two": 2, "key_three": 3},
            "key_one",
            "key_four",
            4,
            {"key_four": 4, "key_one": 1, "key_two": 2, "key_three": 3},
        ),
        # Before middle key
        (
            {"key_one": 1, "key_two": 2, "key_three": 3},
            "key_two",
            "key_four",
            4,
            {"key_one": 1, "key_four": 4, "key_two": 2, "key_three": 3},
        ),
        # Before last key
        (
            {"key_one": 1, "key_two": 2, "key_three": 3},
            "key_three",
            "key_four",
            4,
            {"key_one": 1, "key_two": 2, "key_four": 4, "key_three": 3},
        ),
    ],
)
def test_dict_insert_value_before_key_valid(
    input_dict: dict,
    key: str,
    new_key: str,
    new_value: Any,
    result_dict: dict,
):
    actual = dict_insert_value_before_key(input_dict, key, new_key, new_value)

    assert actual == result_dict


class TestHeroStatsSnapshotSlot:
    """The slot makes every run of one scheduled day agree on a ``captured_at``."""

    def test_boundary_timestamp_is_its_own_slot(self):
        boundary = _SNAPSHOT_SLOT

        assert hero_stats_snapshot_slot(boundary) == boundary

    @pytest.mark.parametrize("offset", [1, 3600, HERO_STATS_SNAPSHOT_SLOT_SECONDS - 1])
    def test_any_time_within_a_day_maps_to_that_day(self, offset: int):
        boundary = _SNAPSHOT_SLOT

        assert hero_stats_snapshot_slot(boundary + offset) == boundary

    def test_next_day_maps_to_the_next_slot(self):
        boundary = _SNAPSHOT_SLOT

        actual = hero_stats_snapshot_slot(boundary + HERO_STATS_SNAPSHOT_SLOT_SECONDS)

        assert actual == boundary + HERO_STATS_SNAPSHOT_SLOT_SECONDS

    def test_defaults_to_the_slot_covering_now(self):
        with patch(
            "app.domain.services.hero_service.time.time",
            return_value=_SNAPSHOT_SLOT + 3599,
        ):
            actual = hero_stats_snapshot_slot()

        assert actual == _SNAPSHOT_SLOT


class TestHeroServiceSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_stores_rows(self):
        svc = _make_hero_service()
        banrate = 8.0
        expected_stat = {
            "hero": "ana",
            "pickrate": 5.5,
            "winrate": 52.3,
            "banrate": banrate,
        }

        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            return_value=[expected_stat],
        ) as mock_parse:
            result = await svc.snapshot_hero_stats()

        assert result.rows_stored > 0
        assert mock_parse.await_count > 0
        cast("Any", svc.storage).store_hero_stats_snapshots.assert_awaited()
        all_rows = [
            row
            for call in cast(
                "Any", svc.storage
            ).store_hero_stats_snapshots.await_args_list
            for row in call.args[1]
        ]
        assert all(row["hero"] == "ana" for row in all_rows)
        assert all(row["banrate"] == banrate for row in all_rows)
        expected_tiers = {tier.value for tier in CompetitiveDivisionHistoryFilter}
        assert {row["tier"] for row in all_rows} == expected_tiers

    @pytest.mark.asyncio
    async def test_snapshot_skips_failed_combos(self):
        svc = _make_hero_service()
        svc._get_hero_stats_gamemode_filters = AsyncMock(return_value=["1"])
        expected_stat = {"hero": "ana", "pickrate": 5.5, "winrate": 52.3}

        async def _parse(*_args, **_kwargs):
            if _kwargs.get("map_filter") == "busan":
                raise ParserBlizzardError(status_code=400, message="map not compatible")
            return [expected_stat]

        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            side_effect=_parse,
        ):
            result = await svc.snapshot_hero_stats()

        assert result.rows_stored > 0
        all_rows = [
            row
            for call in cast(
                "Any", svc.storage
            ).store_hero_stats_snapshots.await_args_list
            for row in call.args[1]
        ]
        stored_maps = {row["map"] for row in all_rows}
        assert "busan" not in stored_maps
        assert stored_maps != set()
        assert result.combinations_failed > 0

    @pytest.mark.asyncio
    async def test_snapshot_retries_failed_combos_after_the_walk(self):
        svc = _make_hero_service()
        svc._get_hero_stats_gamemode_filters = AsyncMock(return_value=["1"])
        expected_stat = {"hero": "ana", "pickrate": 5.5, "winrate": 52.3}
        failed_once: set = set()

        async def _parse(*_args, **_kwargs):
            if _kwargs.get("map_filter") == "busan":
                tier = _kwargs.get("competitive_division")
                if tier not in failed_once:
                    failed_once.add(tier)
                    msg = "transient Blizzard timeout"
                    raise ParserParsingError(msg)
            return [expected_stat]

        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            side_effect=_parse,
        ):
            result = await svc.snapshot_hero_stats()

        all_rows = [
            row
            for call in cast(
                "Any", svc.storage
            ).store_hero_stats_snapshots.await_args_list
            for row in call.args[1]
        ]
        assert failed_once
        assert "busan" in {row["map"] for row in all_rows}
        assert result.combinations_failed == 0

    @pytest.mark.asyncio
    async def test_snapshot_skips_parser_parsing_error(self):
        svc = _make_hero_service()
        svc._get_hero_stats_gamemode_filters = AsyncMock(return_value=["1"])
        expected_stat = {"hero": "ana", "pickrate": 5.5, "winrate": 52.3}

        async def _parse(*_args, **_kwargs):
            if _kwargs.get("map_filter") == "busan":
                msg = "unexpected JSON structure"
                raise ParserParsingError(msg)
            return [expected_stat]

        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            side_effect=_parse,
        ):
            result = await svc.snapshot_hero_stats()

        assert result.rows_stored > 0
        all_rows = [
            row
            for call in cast(
                "Any", svc.storage
            ).store_hero_stats_snapshots.await_args_list
            for row in call.args[1]
        ]
        stored_maps = {row["map"] for row in all_rows}
        assert "busan" not in stored_maps
        assert stored_maps != set()

    @pytest.mark.asyncio
    async def test_snapshot_no_rows_skips_storage(self):
        svc = _make_hero_service()
        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            side_effect=ParserInternalError(
                "https://blizzard", ValueError("always fails")
            ),
        ):
            result = await svc.snapshot_hero_stats()

        assert result.rows_stored == 0
        assert result.combinations_failed == result.combinations_total
        cast("Any", svc.storage).store_hero_stats_snapshots.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_snapshot_uses_the_current_slot_by_default(self):
        svc = _make_hero_service()
        expected_stat = {"hero": "ana", "pickrate": 5.5, "winrate": 52.3}

        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            return_value=[expected_stat],
        ):
            result = await svc.snapshot_hero_stats()

        timestamps = {
            call.args[0]
            for call in cast(
                "Any", svc.storage
            ).store_hero_stats_snapshots.await_args_list
        }
        assert timestamps == {hero_stats_snapshot_slot()}
        assert result.captured_at == hero_stats_snapshot_slot()

    @pytest.mark.asyncio
    async def test_snapshot_stores_every_row_under_the_given_slot(self):
        svc = _make_hero_service()
        slot = _SNAPSHOT_SLOT
        expected_stat = {"hero": "ana", "pickrate": 5.5, "winrate": 52.3}

        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            return_value=[expected_stat],
        ):
            result = await svc.snapshot_hero_stats(captured_at=slot)

        timestamps = {
            call.args[0]
            for call in cast(
                "Any", svc.storage
            ).store_hero_stats_snapshots.await_args_list
        }
        assert timestamps == {slot}
        assert result.captured_at == slot

    @pytest.mark.asyncio
    async def test_snapshot_continues_after_a_failed_flush(self):
        svc = _make_hero_service()
        storage = cast("Any", svc.storage)
        storage.store_hero_stats_snapshots.side_effect = [
            OSError("connection reset"),
            *([None] * 1000),
        ]
        expected_stat = {"hero": "ana", "pickrate": 5.5, "winrate": 52.3}

        with patch(
            "app.domain.services.hero_service.parse_hero_stats_summary",
            return_value=[expected_stat],
        ):
            result = await svc.snapshot_hero_stats()

        assert storage.store_hero_stats_snapshots.await_count > 1
        assert result.rows_lost > 0
        assert result.rows_stored > 0
        assert result.combinations_failed == 0

    def test_snapshot_grid_uses_competitive_only(self):
        svc = _make_hero_service()
        grid = svc._hero_stats_snapshot_grid(map_keys=["busan", "dorado"])

        assert len(grid) > 0
        assert all(entry[1] == PlayerGamemode.COMPETITIVE for entry in grid)
        assert {entry[3] for entry in grid} == {"busan", "dorado"}
        assert {entry[4] for entry in grid} >= {"all", "gold"}

    @pytest.mark.asyncio
    async def test_competitive_map_keys_falls_back_to_csv_enum(self):
        svc = _make_hero_service()

        keys = await svc._competitive_map_keys()

        assert keys == [str(m) for m in MapKey]

    @pytest.mark.asyncio
    async def test_competitive_map_keys_uses_scraped_list(self):
        svc = _make_hero_service()
        rates_maps_html = read_html_file("rates_map_dropdown.html")
        assert rates_maps_html is not None
        cast("Any", svc.storage).get_static_data.return_value = {
            "data": rates_maps_html
        }

        keys = await svc._competitive_map_keys()

        assert "busan" in keys
        assert "anubis" not in keys
        assert "all-maps" not in keys

    @pytest.mark.asyncio
    async def test_competitive_map_keys_handles_bad_stored_data(self):
        svc = _make_hero_service()
        cast("Any", svc.storage).get_static_data.return_value = {
            "data": "<not-the-rates-page>"
        }

        keys = await svc._competitive_map_keys()

        assert keys == [str(m) for m in MapKey]

    @pytest.mark.asyncio
    async def test_competitive_map_keys_handles_stored_not_a_dict(self):
        svc = _make_hero_service()
        cast("Any", svc.storage).get_static_data.return_value = "unexpected-cache-value"

        keys = await svc._competitive_map_keys()

        assert keys == [str(m) for m in MapKey]

    @pytest.mark.asyncio
    async def test_competitive_map_keys_handles_stored_data_not_a_str(self):
        svc = _make_hero_service()
        cast("Any", svc.storage).get_static_data.return_value = {
            "data": {"not": "a string"}
        }

        keys = await svc._competitive_map_keys()

        assert keys == [str(m) for m in MapKey]

    @pytest.mark.asyncio
    async def test_competitive_map_keys_handles_stored_none(self):
        svc = _make_hero_service()
        cast("Any", svc.storage).get_static_data.return_value = None

        keys = await svc._competitive_map_keys()

        assert keys == [str(m) for m in MapKey]

    @pytest.mark.asyncio
    async def test_competitive_map_keys_keeps_map_dropped_from_the_dropdown(self):
        svc = _make_hero_service()
        rates_maps_html = read_html_file("rates_map_dropdown.html")
        assert rates_maps_html is not None
        without_busan = rates_maps_html.replace('value="busan"', 'value="ilios"')
        assert 'value="busan"' not in without_busan
        cast("Any", svc.storage).get_static_data.side_effect = lambda key: (
            {"data": encode_competitive_keys({"busan"})}
            if key == COMPETITIVE_KEYS_STORAGE_KEY
            else {"data": without_busan}
        )

        keys = await svc._competitive_map_keys()

        assert "busan" in keys
        assert "anubis" not in keys

    @pytest.mark.asyncio
    async def test_competitive_map_keys_ignores_remembered_keys_it_cannot_read(self):
        svc = _make_hero_service()
        rates_maps_html = read_html_file("rates_map_dropdown.html")
        assert rates_maps_html is not None

        def _get_static_data(key: str) -> dict:
            if key == COMPETITIVE_KEYS_STORAGE_KEY:
                msg = "storage down"
                raise RuntimeError(msg)
            return {"data": rates_maps_html}

        cast("Any", svc.storage).get_static_data.side_effect = _get_static_data

        keys = await svc._competitive_map_keys()

        assert "busan" in keys
        assert "anubis" not in keys

    @pytest.mark.asyncio
    async def test_competitive_map_keys_ignores_dropdown_failing_quorum(self):
        svc = _make_hero_service()
        cast("Any", svc.storage).get_static_data.return_value = {
            "data": (
                "<html><body><main class='main-content'>"
                "<select id='filter-map-select'><optgroup label='Control'>"
                "<option value='junk-one'>Junk One</option>"
                "<option value='junk-two'>Junk Two</option>"
                "</optgroup></select></main></body></html>"
            )
        }

        keys = await svc._competitive_map_keys()

        assert keys == [str(m) for m in MapKey]


class TestHeroServiceNewMapProbe:
    @staticmethod
    def _service_with_new_scraped_map(
        cached_verdict: bytes | None = None,
    ) -> HeroService:
        svc = _make_hero_service()
        rates_maps_html = read_html_file("rates_map_dropdown.html")
        assert rates_maps_html is not None
        cast("Any", svc.storage).get_static_data.return_value = {
            "data": rates_maps_html.replace('value="suravasa"', 'value="brand-new-map"')
        }
        cast("Any", svc.cache).get.return_value = cached_verdict
        return svc

    @pytest.mark.asyncio
    async def test_new_map_accepted_by_blizzard_is_adopted(self):
        svc = self._service_with_new_scraped_map()

        with patch.object(
            HeroService, "_fetch_hero_stats_for_snapshot", new_callable=AsyncMock
        ) as fetch_mock:
            keys = await svc._competitive_map_keys()

        assert "brand-new-map" in keys
        fetch_mock.assert_awaited_once()
        cast("Any", svc.cache).set.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_known_csv_maps_are_never_probed(self):
        svc = self._service_with_new_scraped_map()

        with patch.object(
            HeroService, "_fetch_hero_stats_for_snapshot", new_callable=AsyncMock
        ) as fetch_mock:
            keys = await svc._competitive_map_keys()

        assert "busan" in keys
        assert fetch_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_new_map_rejected_by_blizzard_is_skipped(self):
        svc = self._service_with_new_scraped_map()
        rejection = ParserBlizzardError(status_code=400, message="incompatible map")

        with patch.object(
            HeroService,
            "_fetch_hero_stats_for_snapshot",
            new=AsyncMock(side_effect=rejection),
        ):
            keys = await svc._competitive_map_keys()

        assert "brand-new-map" not in keys
        assert "busan" in keys
        cast("Any", svc.cache).set.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_inconclusive_probe_is_skipped_without_caching(self):
        svc = self._service_with_new_scraped_map()
        failure = ParserInternalError("https://blizzard", ValueError("boom"))

        with patch.object(
            HeroService,
            "_fetch_hero_stats_for_snapshot",
            new=AsyncMock(side_effect=failure),
        ):
            keys = await svc._competitive_map_keys()

        assert "brand-new-map" not in keys
        cast("Any", svc.cache).set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blizzard_transport_error_is_skipped_without_caching(self):
        svc = self._service_with_new_scraped_map()
        transport_error = ParserBlizzardError(status_code=504, message="unreachable")

        with patch.object(
            HeroService,
            "_fetch_hero_stats_for_snapshot",
            new=AsyncMock(side_effect=transport_error),
        ):
            keys = await svc._competitive_map_keys()

        assert "brand-new-map" not in keys
        cast("Any", svc.cache).set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cached_rejection_avoids_a_second_probe(self):
        svc = self._service_with_new_scraped_map(cached_verdict=b"0")

        with patch.object(
            HeroService, "_fetch_hero_stats_for_snapshot", new_callable=AsyncMock
        ) as fetch_mock:
            keys = await svc._competitive_map_keys()

        assert "brand-new-map" not in keys
        fetch_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cached_acceptance_avoids_a_second_probe(self):
        svc = self._service_with_new_scraped_map(cached_verdict=b"1")

        with patch.object(
            HeroService, "_fetch_hero_stats_for_snapshot", new_callable=AsyncMock
        ) as fetch_mock:
            keys = await svc._competitive_map_keys()

        assert "brand-new-map" in keys
        fetch_mock.assert_not_awaited()


class TestHeroServiceHistory:
    @pytest.mark.asyncio
    async def test_get_history_delegates_to_storage(self):
        svc = _make_hero_service()
        cast("Any", svc.storage).get_hero_stats_history.return_value = [
            {"captured_at": 1700000000, "hero": "ana", "pickrate": 5.5, "winrate": 52.3}
        ]

        result = await svc.get_hero_stats_history(
            platform="pc",
            gamemode="competitive",
            region="europe",
            map_key="busan",
            tier="gold",
            heroes=["ana"],
            since=1700000000,
            until=1700001000,
        )

        assert result[0]["hero"] == "ana"
        cast("Any", svc.storage).get_hero_stats_history.assert_awaited_once_with(
            platform="pc",
            gamemode="competitive",
            region="europe",
            map_="busan",
            tier="gold",
            heroes=["ana"],
            since=1700000000,
            until=1700001000,
            limit=None,
            offset=0,
        )

    @pytest.mark.asyncio
    async def test_get_history_pages_natively_instead_of_over_fetching(self):
        svc = _make_hero_service()
        cast("Any", svc.storage).get_hero_stats_history.return_value = []

        await svc.get_hero_stats_history(
            platform="pc",
            gamemode="competitive",
            limit=100,
            offset=900,
        )

        call = cast("Any", svc.storage).get_hero_stats_history.await_args

        assert call.kwargs["limit"] == 100  # noqa: PLR2004
        assert call.kwargs["offset"] == 900  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_get_history_returns_the_storage_page_verbatim(self):
        svc = _make_hero_service()
        rows = [{"captured_at": 1700000000, "hero": "mercy"}]
        cast("Any", svc.storage).get_hero_stats_history.return_value = rows

        result = await svc.get_hero_stats_history(
            platform="pc",
            gamemode="competitive",
            limit=1,
            offset=1000,
        )

        assert result == rows
