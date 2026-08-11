from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import httpx2
import pytest
from fastapi import status

from app.config import settings
from app.domain.enums import MapGamemode, MapKey
from app.domain.parsers.maps import (
    COMPETITIVE_KEYS_STORAGE_KEY,
    decode_competitive_keys,
    encode_competitive_keys,
)
from app.domain.ports.storage import StaticDataCategory

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from tests.fake_storage import FakeStorage


def test_get_maps(client: TestClient):
    response = client.get("/maps")
    assert response.status_code == status.HTTP_200_OK

    maps = response.json()
    assert len(maps) > 0, "No maps returned"

    for map_data in maps:
        screenshot_url = map_data["screenshot"]
        if screenshot_url is not None:
            screenshot_path = screenshot_url.removeprefix(f"{settings.app_base_url}/")
            path = Path(screenshot_path)
            assert path.is_file(), f"Screenshot file does not exist: {path}"


def test_get_maps_has_competitive_flag(client: TestClient):
    response = client.get("/maps")
    assert response.status_code == status.HTTP_200_OK

    maps = response.json()
    assert len(maps) > 0
    assert all("competitive" in map_data for map_data in maps)
    assert any(map_data["competitive"] for map_data in maps)
    assert not all(map_data["competitive"] for map_data in maps)


@pytest.mark.parametrize("gamemode", list(MapGamemode))
def test_get_maps_filter_by_gamemode(client: TestClient, gamemode: MapGamemode):
    response = client.get("/maps", params={"gamemode": gamemode})
    assert response.status_code == status.HTTP_200_OK
    assert all(gamemode in map_dict["gamemodes"] for map_dict in response.json())


def test_get_maps_invalid_gamemode(client: TestClient):
    response = client.get("/maps", params={"gamemode": "invalid"})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_get_maps_serves_csv_when_dropdown_is_gone(client: TestClient):
    broken_html = "<html><body><main class='main-content'></main></body></html>"

    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(status_code=status.HTTP_200_OK, text=broken_html),
    ):
        response = client.get("/maps")

    assert response.status_code == status.HTTP_200_OK
    maps = response.json()
    assert {map_data["key"] for map_data in maps} == {str(m) for m in MapKey}
    assert all(map_data["competitive"] is None for map_data in maps)


def test_get_maps_serves_csv_when_blizzard_returns_an_error(client: TestClient):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, text="Blizzard is down"
        ),
    ):
        response = client.get("/maps")

    assert response.status_code == status.HTTP_200_OK
    maps = response.json()
    assert {map_data["key"] for map_data in maps} == {str(m) for m in MapKey}
    assert all(map_data["competitive"] is None for map_data in maps)


def test_get_maps_serves_csv_when_blizzard_is_unreachable(client: TestClient):
    with patch(
        "httpx2.AsyncClient.get",
        side_effect=httpx2.ConnectError("connection refused"),
    ):
        response = client.get("/maps")

    assert response.status_code == status.HTTP_200_OK
    assert {map_data["key"] for map_data in response.json()} == {str(m) for m in MapKey}


@pytest.mark.asyncio
async def test_get_maps_keeps_remembered_maps_competitive_when_dropdown_is_gone(
    client: TestClient, storage_db: FakeStorage
):
    broken_html = "<html><body><main class='main-content'></main></body></html>"
    await storage_db.set_static_data(
        key=COMPETITIVE_KEYS_STORAGE_KEY,
        data=encode_competitive_keys({"busan"}),
        category=StaticDataCategory.MAPS,
    )

    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(status_code=status.HTTP_200_OK, text=broken_html),
    ):
        response = client.get("/maps")

    assert response.status_code == status.HTTP_200_OK
    by_key = {map_data["key"]: map_data for map_data in response.json()}
    assert by_key["busan"]["competitive"] is True
    assert by_key["anubis"]["competitive"] is False


@pytest.mark.asyncio
async def test_get_maps_remembers_the_scraped_competitive_maps(
    client: TestClient, storage_db: FakeStorage
):
    response = client.get("/maps")

    assert response.status_code == status.HTTP_200_OK
    remembered = decode_competitive_keys(
        await storage_db.get_static_data(COMPETITIVE_KEYS_STORAGE_KEY)
    )
    assert "busan" in remembered
