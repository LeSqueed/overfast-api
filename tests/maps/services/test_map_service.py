"""Tests for MapService — the sticky (monotonic) competitive map keys"""

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import status

from app.config import settings
from app.domain.enums import MapKey
from app.domain.parsers.maps import (
    COMPETITIVE_KEYS_STORAGE_KEY,
    decode_competitive_keys,
    encode_competitive_keys,
    parse_rates_maps_html,
)
from app.domain.ports.storage import StaticDataCategory
from app.domain.services.map_service import MapService

if TYPE_CHECKING:
    from app.domain.ports import StoragePort
    from tests.fake_storage import FakeStorage

BROKEN_HTML = "<html><body><main class='main-content'></main></body></html>"


def _make_map_service(storage: StoragePort) -> MapService:
    cache = AsyncMock()
    blizzard_client = AsyncMock()
    task_queue = AsyncMock()
    task_queue.is_job_pending_or_running.return_value = False
    return MapService(cache, storage, blizzard_client, task_queue)


async def _store_rates_html(storage: FakeStorage, html: str) -> None:
    await storage.set_static_data(
        key="maps:rates", data=html, category=StaticDataCategory.MAPS
    )


async def _store_competitive_keys(storage: FakeStorage, keys: set[str]) -> None:
    await storage.set_static_data(
        key=COMPETITIVE_KEYS_STORAGE_KEY,
        data=encode_competitive_keys(keys),
        category=StaticDataCategory.MAPS,
    )


async def _stored_competitive_keys(storage: FakeStorage) -> frozenset[str]:
    return decode_competitive_keys(
        await storage.get_static_data(COMPETITIVE_KEYS_STORAGE_KEY)
    )


def _dropdown_without(rates_html: str, missing_key: str) -> str:
    """Rebuild the scraped dropdown with one map key left out."""
    options = "".join(
        f'<option data-title="{map_dict["name"]}" value="{map_dict["key"]}"></option>'
        for map_dict in parse_rates_maps_html(rates_html)
        if map_dict["key"] != missing_key
    )
    return (
        "<html><body><main class='main-content'>"
        f'<select id="filter-map-select"><optgroup label="Control">{options}'
        "</optgroup></select></main></body></html>"
    )


@pytest.mark.asyncio
async def test_list_maps_persists_newly_scraped_competitive_keys(
    storage_db: FakeStorage, rates_maps_html_data: str
):
    await _store_rates_html(storage_db, rates_maps_html_data)
    svc = _make_map_service(storage_db)

    maps, _, _ = await svc.list_maps(None, "/maps")

    assert {m["key"]: m["competitive"] for m in maps}["busan"] is True
    assert "busan" in await _stored_competitive_keys(storage_db)


@pytest.mark.asyncio
async def test_list_maps_keeps_remembered_map_competitive_when_scrape_fails(
    storage_db: FakeStorage,
):
    await _store_rates_html(storage_db, BROKEN_HTML)
    await _store_competitive_keys(storage_db, {"busan"})
    svc = _make_map_service(storage_db)

    maps, _, _ = await svc.list_maps(None, "/maps")

    by_key = {m["key"]: m for m in maps}
    assert by_key["busan"]["competitive"] is True
    assert by_key["anubis"]["competitive"] is False


@pytest.mark.asyncio
async def test_list_maps_never_shrinks_the_remembered_keys(
    storage_db: FakeStorage, rates_maps_html_data: str
):
    dropdown = _dropdown_without(rates_maps_html_data, "busan")
    assert 'value="busan"' not in dropdown
    await _store_rates_html(storage_db, dropdown)
    await _store_competitive_keys(storage_db, {"busan"})
    svc = _make_map_service(storage_db)

    maps, _, _ = await svc.list_maps(None, "/maps")

    remembered = await _stored_competitive_keys(storage_db)
    assert {m["key"]: m["competitive"] for m in maps}["busan"] is True
    assert "busan" in remembered
    assert "ilios" in remembered


@pytest.mark.asyncio
async def test_list_maps_survives_unreadable_competitive_keys(
    rates_maps_html_data: str,
):
    storage = AsyncMock()

    async def _get_static_data(key: str) -> dict | None:
        if key == COMPETITIVE_KEYS_STORAGE_KEY:
            msg = "storage down"
            raise RuntimeError(msg)
        return {"data": rates_maps_html_data, "updated_at": 0}

    storage.get_static_data.side_effect = _get_static_data
    svc = _make_map_service(storage)

    maps, _, _ = await svc.list_maps(None, "/maps")

    assert {m["key"]: m["competitive"] for m in maps}["busan"] is True
    storage.set_static_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_maps_survives_a_failing_competitive_keys_write(
    storage_db: FakeStorage, rates_maps_html_data: str
):
    await _store_rates_html(storage_db, rates_maps_html_data)
    svc = _make_map_service(storage_db)
    storage_db.set_static_data = AsyncMock(side_effect=RuntimeError("disk full"))

    maps, _, _ = await svc.list_maps(None, "/maps")

    assert {m["key"]: m["competitive"] for m in maps}["busan"] is True


@pytest.mark.asyncio
async def test_list_maps_degrades_to_the_csv_when_the_cold_fetch_fails(
    storage_db: FakeStorage,
):
    svc = _make_map_service(storage_db)
    cast("Any", svc.blizzard_client).get.side_effect = RuntimeError("blizzard down")

    maps, _, _ = await svc.list_maps(None, "/maps")

    assert {m["key"] for m in maps} == {str(m) for m in MapKey}
    assert all(m["competitive"] is None for m in maps)


@pytest.mark.asyncio
async def test_list_maps_does_not_store_a_degraded_cold_fetch(
    storage_db: FakeStorage,
):
    svc = _make_map_service(storage_db)
    cast("Any", svc.blizzard_client).get.side_effect = RuntimeError("blizzard down")

    await svc.list_maps(None, "/maps")

    assert await storage_db.get_static_data("maps:rates") is None


@pytest.mark.asyncio
async def test_list_maps_caches_a_degraded_cold_fetch_only_briefly(
    storage_db: FakeStorage,
):
    svc = _make_map_service(storage_db)
    cast("Any", svc.blizzard_client).get.side_effect = RuntimeError("blizzard down")

    await svc.list_maps(None, "/maps")

    cached_ttl = cast("Any", svc.cache).update_api_cache.call_args.args[2]
    assert cached_ttl == settings.stale_cache_timeout
    assert cached_ttl < settings.csv_cache_timeout


@pytest.mark.asyncio
async def test_list_maps_keeps_remembered_maps_competitive_when_the_fetch_fails(
    storage_db: FakeStorage,
):
    await _store_competitive_keys(storage_db, {"busan"})
    svc = _make_map_service(storage_db)
    cast("Any", svc.blizzard_client).get.side_effect = RuntimeError("blizzard down")

    maps, _, _ = await svc.list_maps(None, "/maps")

    by_key = {m["key"]: m for m in maps}
    assert by_key["busan"]["competitive"] is True
    assert by_key["anubis"]["competitive"] is False


@pytest.mark.asyncio
async def test_list_maps_retries_the_fetch_after_a_degraded_cold_fetch(
    storage_db: FakeStorage, rates_maps_html_data: str
):
    svc = _make_map_service(storage_db)
    blizzard = cast("Any", svc.blizzard_client)
    blizzard.get.side_effect = RuntimeError("blizzard down")
    await svc.list_maps(None, "/maps")
    blizzard.get.side_effect = None
    blizzard.get.return_value = Mock(
        status_code=status.HTTP_200_OK, text=rates_maps_html_data
    )

    maps, _, _ = await svc.list_maps(None, "/maps")

    assert {m["key"]: m["competitive"] for m in maps}["busan"] is True
    assert await storage_db.get_static_data("maps:rates") is not None


@pytest.mark.asyncio
async def test_refresh_list_degrades_to_the_csv_when_the_fetch_fails(
    storage_db: FakeStorage,
):
    await _store_competitive_keys(storage_db, {"busan"})
    svc = _make_map_service(storage_db)
    cast("Any", svc.blizzard_client).get.side_effect = RuntimeError("blizzard down")

    await svc.refresh_list()

    assert await storage_db.get_static_data("maps:rates") is None
    assert await _stored_competitive_keys(storage_db) == frozenset({"busan"})


@pytest.mark.asyncio
async def test_refresh_list_persists_newly_scraped_competitive_keys(
    storage_db: FakeStorage, rates_maps_html_data: str
):
    svc = _make_map_service(storage_db)
    cast("Any", svc.blizzard_client).get.return_value = Mock(
        status_code=status.HTTP_200_OK, text=rates_maps_html_data
    )

    await svc.refresh_list()

    assert "busan" in await _stored_competitive_keys(storage_db)


@pytest.mark.asyncio
async def test_refresh_list_keeps_remembered_keys_when_the_scrape_is_unusable(
    storage_db: FakeStorage,
):
    await _store_competitive_keys(storage_db, {"busan"})
    svc = _make_map_service(storage_db)
    cast("Any", svc.blizzard_client).get.return_value = Mock(
        status_code=status.HTTP_200_OK, text=BROKEN_HTML
    )

    await svc.refresh_list()

    assert await _stored_competitive_keys(storage_db) == frozenset({"busan"})
