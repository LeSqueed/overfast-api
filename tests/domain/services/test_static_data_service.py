"""Tests for StaticDataService — get_or_fetch, _parse_stored, _store_in_storage"""

import time
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.domain.services.static_data_service import StaticDataService, StaticFetchConfig


def _make_service() -> StaticDataService:
    cache = AsyncMock()
    storage = AsyncMock()
    blizzard_client = AsyncMock()
    task_queue = AsyncMock()
    task_queue.is_job_pending_or_running.return_value = False
    return StaticDataService(cache, storage, blizzard_client, task_queue)


def _make_config(
    *,
    fetcher=None,
    parser=None,
    result_filter=None,
    fetch_fallback=None,
    storage_key="heroes:en-us",
    cache_key="/heroes",
    entity_type="heroes",
    staleness_threshold=3600,
) -> StaticFetchConfig:
    if fetcher is None:
        fetcher = lambda: [{"key": "ana"}]  # noqa: E731
    return StaticFetchConfig(
        storage_key=storage_key,
        fetcher=fetcher,
        parser=parser,
        cache_key=cache_key,
        cache_ttl=86400,
        staleness_threshold=staleness_threshold,
        entity_type=entity_type,
        result_filter=result_filter,
        fetch_fallback=fetch_fallback,
    )


def _failing_fetcher():
    msg = "blizzard down"
    raise RuntimeError(msg)


class TestGetOrFetch:
    @pytest.mark.asyncio
    async def test_cold_fetch_when_storage_miss(self):
        """When storage returns None, performs a cold fetch."""
        svc = _make_service()
        cast("Any", svc.storage).get_static_data.return_value = None

        parsed = [{"key": "ana"}]
        config = _make_config(fetcher=lambda: parsed)

        data, is_stale, age = await svc.get_or_fetch(config)

        assert data == parsed
        assert is_stale is False
        assert age == 0

    @pytest.mark.asyncio
    async def test_serves_fresh_from_storage(self):
        """Storage hit with fresh data returns is_stale=False."""
        svc = _make_service()
        now = int(time.time())
        cast("Any", svc.storage).get_static_data.return_value = {
            "data": "raw-html",
            "updated_at": now - 100,  # 100s old, threshold=3600 → fresh
        }

        parsed = [{"key": "ana"}]
        config = _make_config(
            fetcher=lambda: parsed,
            parser=lambda _html: parsed,
            staleness_threshold=3600,
        )

        _data, is_stale, age = await svc.get_or_fetch(config)

        assert is_stale is False
        assert 99 <= age <= 102  # noqa: PLR2004

        # Age header correctness: stored_at must be the original updated_at (not now)
        # so that nginx/Lua computes Age from the true data age, not the Valkey write time.
        call_kwargs = cast("Any", svc.cache).update_api_cache.call_args.kwargs
        assert call_kwargs["stored_at"] == now - 100

    @pytest.mark.asyncio
    async def test_serves_stale_from_storage_and_enqueues_refresh(self):
        """Storage hit with stale data returns is_stale=True and enqueues refresh."""
        svc = _make_service()
        now = int(time.time())
        updated_at = now - 7200  # 7200s old, threshold=3600 → stale
        cast("Any", svc.storage).get_static_data.return_value = {
            "data": "old-html",
            "updated_at": updated_at,
        }

        parsed = [{"key": "ana"}]
        config = _make_config(
            fetcher=lambda: parsed,
            parser=lambda _html: parsed,
            staleness_threshold=3600,
        )

        _data, is_stale, age = await svc.get_or_fetch(config)

        assert is_stale is True
        assert age >= 3600  # noqa: PLR2004
        cast("Any", svc.task_queue).enqueue.assert_awaited_once()

        # Age header correctness: stored_at must be the original updated_at (not now),
        # and cache_ttl must be the full config.cache_ttl (not stale_cache_timeout).
        call_kwargs = cast("Any", svc.cache).update_api_cache.call_args.kwargs
        assert call_kwargs["stored_at"] == updated_at
        assert (
            cast("Any", svc.cache).update_api_cache.call_args.args[2]
            == config.cache_ttl
        )

    @pytest.mark.asyncio
    async def test_result_filter_applied(self):
        """result_filter is called on the data before returning."""
        svc = _make_service()
        cast("Any", svc.storage).get_static_data.return_value = None

        all_heroes = [{"key": "ana"}, {"key": "mercy"}]
        filtered = [{"key": "ana"}]

        config = _make_config(
            fetcher=lambda: all_heroes,
            result_filter=lambda heroes: [h for h in heroes if h["key"] == "ana"],
        )

        data, _, _ = await svc.get_or_fetch(config)

        assert data == filtered


class TestFetchFallback:
    """``fetch_fallback`` is opt-in: without it, a failing fetch stays fatal.

    Heroes, roles and gamemodes never set it — they have no local source, so
    serving a degraded answer for them would mean serving nothing at all.
    """

    @pytest.mark.asyncio
    async def test_fetch_failure_propagates_without_a_fallback(self):
        svc = _make_service()
        cast("Any", svc.storage).get_static_data.return_value = None
        config = _make_config(fetcher=_failing_fetcher)

        with pytest.raises(RuntimeError, match="blizzard down"):
            await svc.get_or_fetch(config)

    @pytest.mark.asyncio
    async def test_nothing_is_stored_when_a_fetch_fails_without_a_fallback(self):
        svc = _make_service()
        cast("Any", svc.storage).get_static_data.return_value = None
        config = _make_config(fetcher=_failing_fetcher)

        with pytest.raises(RuntimeError):
            await svc.get_or_fetch(config)

        cast("Any", svc.storage).set_static_data.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_async_fetch_failure_propagates_without_a_fallback(self):
        svc = _make_service()
        cast("Any", svc.storage).get_static_data.return_value = None

        async def async_fetcher():
            msg = "blizzard down"
            raise RuntimeError(msg)

        config = _make_config(fetcher=async_fetcher)

        with pytest.raises(RuntimeError, match="blizzard down"):
            await svc.get_or_fetch(config)

    @pytest.mark.asyncio
    async def test_fallback_serves_the_local_source(self):
        svc = _make_service()
        cast("Any", svc.storage).get_static_data.return_value = None
        local = [{"key": "local"}]
        config = _make_config(fetcher=_failing_fetcher, fetch_fallback=lambda: local)

        data, is_stale, age = await svc.get_or_fetch(config)

        assert data == local
        assert is_stale is False
        assert age == 0

    @pytest.mark.asyncio
    async def test_degraded_result_is_never_persisted(self):
        svc = _make_service()
        cast("Any", svc.storage).get_static_data.return_value = None
        config = _make_config(
            fetcher=_failing_fetcher, fetch_fallback=lambda: [{"key": "local"}]
        )

        await svc.get_or_fetch(config)

        cast("Any", svc.storage).set_static_data.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_degraded_result_is_cached_only_for_the_stale_window(self):
        svc = _make_service()
        cast("Any", svc.storage).get_static_data.return_value = None
        config = _make_config(
            fetcher=_failing_fetcher, fetch_fallback=lambda: [{"key": "local"}]
        )

        await svc.get_or_fetch(config)

        cached_ttl = cast("Any", svc.cache).update_api_cache.call_args.args[2]
        assert cached_ttl == settings.stale_cache_timeout
        assert cached_ttl != config.cache_ttl

    @pytest.mark.asyncio
    async def test_successful_fetch_is_stored_and_cached_normally(self):
        svc = _make_service()
        cast("Any", svc.storage).get_static_data.return_value = None
        config = _make_config(fetch_fallback=lambda: [{"key": "local"}])

        data, _, _ = await svc.get_or_fetch(config)

        assert data == [{"key": "ana"}]
        cast("Any", svc.storage).set_static_data.assert_awaited_once()
        assert (
            cast("Any", svc.cache).update_api_cache.call_args.args[2]
            == config.cache_ttl
        )

    @pytest.mark.asyncio
    async def test_fallback_output_still_goes_through_the_parser(self):
        svc = _make_service()
        cast("Any", svc.storage).get_static_data.return_value = None
        parser = MagicMock(return_value=[{"key": "parsed"}])
        config = _make_config(
            fetcher=_failing_fetcher,
            fetch_fallback=lambda: "local-raw",
            parser=parser,
        )

        data, _, _ = await svc.get_or_fetch(config)

        parser.assert_called_once_with("local-raw")
        assert data == [{"key": "parsed"}]


class TestParseStored:
    @pytest.mark.asyncio
    async def test_uses_parser_when_set(self):
        """With parser, _parse_stored calls parser(raw) and returns result."""
        svc = _make_service()
        parser = MagicMock(return_value=[{"key": "ana"}])
        config = _make_config(parser=parser)

        result = await svc._parse_stored("raw-html", config)

        parser.assert_called_once_with("raw-html")
        assert result == [{"key": "ana"}]

    @pytest.mark.asyncio
    async def test_calls_sync_fetcher_when_no_parser(self):
        """Without parser (CSV source), _parse_stored re-calls the sync fetcher."""
        svc = _make_service()
        sync_fetcher = MagicMock(return_value=[{"key": "map-a"}])
        config = _make_config(fetcher=sync_fetcher, parser=None)

        result = await svc._parse_stored("stored-json", config)

        sync_fetcher.assert_called_once()
        assert result == [{"key": "map-a"}]

    @pytest.mark.asyncio
    async def test_calls_async_fetcher_when_no_parser(self):
        """Without parser (CSV source), _parse_stored awaits async fetcher."""
        svc = _make_service()

        async def async_fetcher():
            return [{"key": "gamemode-a"}]

        config = _make_config(fetcher=async_fetcher, parser=None)
        result = await svc._parse_stored("stored-json", config)

        assert result == [{"key": "gamemode-a"}]


class TestStoreInStorage:
    @pytest.mark.asyncio
    async def test_stores_successfully(self):
        """_store_in_storage calls storage.set_static_data."""
        svc = _make_service()
        await svc._store_in_storage("heroes:en-us", "<html>", "heroes")

        cast("Any", svc.storage).set_static_data.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exception_is_swallowed(self):
        """Storage write errors are logged and swallowed."""
        svc = _make_service()
        cast("Any", svc.storage).set_static_data.side_effect = Exception("disk full")
        # Should not raise
        await svc._store_in_storage("heroes:en-us", "<html>", "heroes")
