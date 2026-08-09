from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import status

from app.config import settings
from app.domain.enums import MapGamemode

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


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
