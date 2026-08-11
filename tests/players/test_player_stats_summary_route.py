from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest
from fastapi import status
from httpx2 import TimeoutException

from app.config import settings
from app.domain.enums import PlayerGamemode, PlayerPlatform

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _setup_player_stats_test(
    player_html_data: str,
    player_search_response_mock: Mock,
):
    with patch(
        "httpx2.AsyncClient.get",
        side_effect=[
            # Players search call first
            player_search_response_mock,
            # Player profile page
            Mock(status_code=status.HTTP_200_OK, text=player_html_data),
        ],
    ):
        yield


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
def test_get_player_stats(client: TestClient):
    response = client.get("/players/TeKrop-2217/stats/summary")

    assert response.status_code == status.HTTP_200_OK
    assert len(response.json().keys()) > 0


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
def test_get_player_stats_summary_valid_gamemode(client: TestClient):
    response = client.get(
        "/players/TeKrop-2217/stats/summary",
        params={"gamemode": PlayerGamemode.QUICKPLAY},
    )

    assert response.status_code == status.HTTP_200_OK
    assert len(response.json().keys()) > 0


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
def test_get_player_stats_summary_invalid_gamemode(client: TestClient):
    response = client.get(
        "/players/TeKrop-2217/stats/summary",
        params={"gamemode": "invalid_gamemode"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
def test_get_player_stats_summary_valid_platform(client: TestClient):
    response = client.get(
        "/players/TeKrop-2217/stats/summary",
        params={"platform": PlayerPlatform.PC},
    )

    assert response.status_code == status.HTTP_200_OK
    assert len(response.json().keys()) > 0


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
def test_get_player_stats_summary_empty_platform(client: TestClient):
    response = client.get(
        "/players/TeKrop-2217/stats/summary",
        params={"platform": PlayerPlatform.CONSOLE},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {}


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
def test_get_player_stats_summary_invalid_platform(client: TestClient):
    response = client.get(
        "/players/TeKrop-2217/stats/summary",
        params={"platform": "invalid_platform"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
def test_get_player_stats_summary_blizzard_error(client: TestClient):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            text="Service Unavailable",
        ),
    ):
        response = client.get(
            "/players/TeKrop-2217/stats/summary",
            params={"gamemode": PlayerGamemode.QUICKPLAY},
        )

    assert response.status_code == status.HTTP_504_GATEWAY_TIMEOUT
    assert response.json() == {
        "error": "Couldn't get Blizzard page (HTTP 503 error) : Service Unavailable",
    }


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
def test_get_player_stats_summary_blizzard_timeout(client: TestClient):
    with patch(
        "httpx2.AsyncClient.get",
        side_effect=TimeoutException(
            "HTTPSConnectionPool(host='overwatch.blizzard.com', port=443): "
            "Read timed out. (read timeout=10)",
        ),
    ):
        response = client.get(
            "/players/TeKrop-2217/stats/summary",
            params={"gamemode": PlayerGamemode.QUICKPLAY},
        )

    assert response.status_code == status.HTTP_504_GATEWAY_TIMEOUT
    assert response.json() == {
        "error": (
            "Couldn't get Blizzard page (HTTP 0 error) : "
            "Blizzard took more than 10 seconds to respond, resulting in a timeout"
        ),
    }


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
def test_get_player_stats_summary_released_hero_not_in_csv(client: TestClient):
    profile_data = {
        "summary": {},
        "stats": {
            "pc": {
                "quickplay": {
                    "career_stats": {
                        "brand-new-hero": [
                            {
                                "category": "game",
                                "label": "Game",
                                "stats": [
                                    {"key": "games_played", "value": 10},
                                    {"key": "games_lost", "value": 4},
                                    {"key": "time_played", "value": 3600},
                                ],
                            },
                            {
                                "category": "combat",
                                "label": "Combat",
                                "stats": [
                                    {"key": "eliminations", "value": 100},
                                    {"key": "deaths", "value": 20},
                                    {"key": "all_damage_done", "value": 5000},
                                ],
                            },
                            {
                                "category": "assists",
                                "label": "Assists",
                                "stats": [
                                    {"key": "offensive_assists", "value": 30},
                                    {"key": "healing_done", "value": 2000},
                                ],
                            },
                        ],
                    },
                },
                "competitive": None,
            },
            "console": None,
        },
    }

    with patch(
        "app.domain.parsers.player_stats.parse_player_profile_html",
        return_value=profile_data,
    ):
        response = client.get("/players/TeKrop-2217/stats/summary")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["heroes"]["brand-new-hero"] == {
        "games_played": 10,
        "games_won": 6,
        "games_lost": 4,
        "time_played": 3600,
        "winrate": 60.0,
        "kda": 6.5,
        "total": {
            "eliminations": 100,
            "assists": 30,
            "deaths": 20,
            "damage": 5000,
            "healing": 2000,
        },
        "average": {
            "eliminations": 16.67,
            "assists": 5.0,
            "deaths": 3.33,
            "damage": 833.33,
            "healing": 333.33,
        },
    }


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
def test_get_player_stats_summary_internal_error(client: TestClient):
    with patch(
        "app.domain.services.player_service.PlayerService.get_player_stats_summary",
        return_value=(
            {
                "general": [{"category": "invalid_value", "stats": [{"key": "test"}]}],
            },
            False,
            0,
        ),
    ):
        response = client.get(
            "/players/TeKrop-2217/stats/summary",
            params={"gamemode": PlayerGamemode.QUICKPLAY},
        )

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.json() == {"error": settings.internal_server_error_message}


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
def test_get_player_stats_summary_blizzard_forbidden_error(client: TestClient):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(
            status_code=status.HTTP_403_FORBIDDEN,
            text="403 Forbidden",
        ),
    ):
        response = client.get(
            "/players/TeKrop-2217/stats/summary",
            params={"gamemode": PlayerGamemode.QUICKPLAY},
        )

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert (
        "Blizzard is temporarily rate limiting this API. Please retry after"
        in response.json()["error"]
    )
