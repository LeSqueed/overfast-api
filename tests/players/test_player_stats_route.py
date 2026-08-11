from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest
from fastapi import status
from httpx2 import TimeoutException

from app.config import settings
from app.domain.enums import HeroKeyCareerFilter, PlayerGamemode, PlayerPlatform

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

# Profile of a player who played a hero released before heroes.csv caught up
_BRAND_NEW_HERO_CAREER_STATS = [
    {
        "category": "combat",
        "label": "Combat",
        "stats": [{"key": "eliminations", "label": "Eliminations", "value": 10}],
    }
]

_PROFILE_WITH_BRAND_NEW_HERO = {
    "summary": {"username": "TeKrop"},
    "stats": {
        PlayerPlatform.PC.value: {
            PlayerGamemode.QUICKPLAY.value: {
                "heroes_comparisons": {},
                "career_stats": {"brand-new-hero": _BRAND_NEW_HERO_CAREER_STATS},
            },
            PlayerGamemode.COMPETITIVE.value: None,
        },
        PlayerPlatform.CONSOLE.value: None,
    },
}


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
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
def test_get_player_stats(client: TestClient, uri: str):
    response = client.get(
        f"/players/TeKrop-2217{uri}",
        params={"gamemode": PlayerGamemode.QUICKPLAY},
    )

    assert response.status_code == status.HTTP_200_OK
    assert len(response.json().keys()) > 0


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
def test_get_player_stats_valid_hero(client: TestClient, uri: str):
    response = client.get(
        f"/players/TeKrop-2217{uri}",
        params={
            "gamemode": PlayerGamemode.QUICKPLAY,
            "hero": HeroKeyCareerFilter.ANA,
        },
    )

    assert response.status_code == status.HTTP_200_OK
    assert set(response.json().keys()) == {HeroKeyCareerFilter.ANA}


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
@pytest.mark.parametrize(
    "hero",
    ["invalid_hero", "Ana", "ana!", "", "a" * 51],
)
def test_get_player_stats_malformed_hero(client: TestClient, uri: str, hero: str):
    response = client.get(
        f"/players/TeKrop-2217{uri}",
        params={
            "gamemode": PlayerGamemode.QUICKPLAY,
            "hero": hero,
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
def test_get_player_stats_unknown_hero(client: TestClient, uri: str):
    response = client.get(
        f"/players/TeKrop-2217{uri}",
        params={
            "gamemode": PlayerGamemode.QUICKPLAY,
            "hero": "anaa",
        },
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == {
        "error": (
            "Hero key 'anaa' is unknown and this player has no statistics for it. "
            "Please check the list of available hero keys."
        )
    }


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
def test_get_player_stats_unknown_hero_never_writes_api_cache(
    client: TestClient, uri: str
):
    """The rejected key must never land in the API cache.

    nginx serves ``api-cache:<request_uri>`` with a 200 without consulting the
    app, so a cached empty body would keep answering 200 for a whole TTL.
    """
    with patch(
        "app.adapters.cache.valkey_cache.ValkeyCache.update_api_cache"
    ) as update_api_cache_mock:
        response = client.get(
            f"/players/TeKrop-2217{uri}",
            params={
                "gamemode": PlayerGamemode.QUICKPLAY,
                "hero": "anaa",
            },
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    update_api_cache_mock.assert_not_called()


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
def test_get_player_stats_unknown_hero_never_writes_api_cache_from_storage(
    client: TestClient, uri: str
):
    """Same guarantee on the persistent storage fast-path.

    The first call stores the profile and exhausts the two mocked Blizzard
    responses, so the second call can only succeed by reading it back from
    storage — a live call would raise StopIteration and fail this test.
    """
    client.get(
        f"/players/TeKrop-2217{uri}",
        params={"gamemode": PlayerGamemode.QUICKPLAY},
    )

    with patch(
        "app.adapters.cache.valkey_cache.ValkeyCache.update_api_cache"
    ) as update_api_cache_mock:
        response = client.get(
            f"/players/TeKrop-2217{uri}",
            params={
                "gamemode": PlayerGamemode.QUICKPLAY,
                "hero": "anaa",
            },
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    update_api_cache_mock.assert_not_called()


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
def test_get_player_stats_known_hero_writes_api_cache(client: TestClient, uri: str):
    """Positive control : a valid hero still gets its API cache entry."""
    with patch(
        "app.adapters.cache.valkey_cache.ValkeyCache.update_api_cache"
    ) as update_api_cache_mock:
        response = client.get(
            f"/players/TeKrop-2217{uri}",
            params={
                "gamemode": PlayerGamemode.QUICKPLAY,
                "hero": HeroKeyCareerFilter.ANA,
            },
        )

    assert response.status_code == status.HTTP_200_OK
    update_api_cache_mock.assert_called_once()


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(
    ("uri", "patch_target", "expected_hero_stats"),
    [
        (
            "/stats",
            "app.domain.services.player_service.parse_player_profile_html",
            _BRAND_NEW_HERO_CAREER_STATS,
        ),
        (
            "/stats/career",
            "app.domain.parsers.player_career_stats.parse_player_profile_html",
            {"combat": {"eliminations": 10}},
        ),
    ],
)
def test_get_player_stats_hero_not_in_csv_but_played(
    client: TestClient,
    uri: str,
    patch_target: str,
    expected_hero_stats: list | dict,
):
    """A hero missing from heroes.csv but played is accepted, never rejected."""
    with patch(patch_target, return_value=_PROFILE_WITH_BRAND_NEW_HERO):
        response = client.get(
            f"/players/TeKrop-2217{uri}",
            params={
                "gamemode": PlayerGamemode.QUICKPLAY,
                "hero": "brand-new-hero",
            },
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"brand-new-hero": expected_hero_stats}


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(
    ("uri", "patch_target", "hero_stats"),
    [
        (
            "/stats",
            "app.domain.services.player_service.PlayerService.get_player_stats",
            [
                {
                    "category": "combat",
                    "label": "Combat",
                    "stats": [
                        {"key": "eliminations", "label": "Eliminations", "value": 42},
                    ],
                },
            ],
        ),
        (
            "/stats/career",
            "app.domain.services.player_service.PlayerService.get_player_career_stats",
            {"combat": {"eliminations": 42}},
        ),
    ],
)
def test_get_player_stats_released_hero_not_in_csv(
    client: TestClient,
    uri: str,
    patch_target: str,
    hero_stats: list | dict,
):
    with patch(patch_target, return_value=({"brand-new-hero": hero_stats}, False, 0)):
        response = client.get(
            f"/players/TeKrop-2217{uri}",
            params={
                "gamemode": PlayerGamemode.QUICKPLAY,
                "hero": "brand-new-hero",
            },
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"brand-new-hero": hero_stats}


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
def test_get_player_stats_missing_gamemode(client: TestClient, uri: str):
    response = client.get(f"/players/TeKrop-2217{uri}")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
def test_get_player_stats_invalid_gamemode(client: TestClient, uri: str):
    response = client.get(
        f"/players/TeKrop-2217{uri}",
        params={"gamemode": "invalid_gamemode"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
def test_get_player_stats_valid_platform(client: TestClient, uri: str):
    response = client.get(
        f"/players/TeKrop-2217{uri}",
        params={
            "gamemode": PlayerGamemode.QUICKPLAY,
            "platform": PlayerPlatform.PC,
        },
    )

    assert response.status_code == status.HTTP_200_OK
    assert len(response.json().keys()) > 0


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
def test_get_player_stats_empty_platform(client: TestClient, uri: str):
    response = client.get(
        f"/players/TeKrop-2217{uri}",
        params={
            "gamemode": PlayerGamemode.QUICKPLAY,
            "platform": PlayerPlatform.CONSOLE,
        },
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {}


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
def test_get_player_stats_invalid_platform(client: TestClient, uri: str):
    response = client.get(
        f"/players/TeKrop-2217{uri}",
        params={
            "gamemode": PlayerGamemode.QUICKPLAY,
            "platform": "invalid_platform",
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
def test_get_player_stats_blizzard_error(client: TestClient, uri: str):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            text="Service Unavailable",
        ),
    ):
        response = client.get(
            f"/players/TeKrop-2217{uri}",
            params={"gamemode": PlayerGamemode.QUICKPLAY},
        )

    assert response.status_code == status.HTTP_504_GATEWAY_TIMEOUT
    assert response.json() == {
        "error": "Couldn't get Blizzard page (HTTP 503 error) : Service Unavailable",
    }


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
def test_get_player_stats_blizzard_timeout(client: TestClient, uri: str):
    with patch(
        "httpx2.AsyncClient.get",
        side_effect=TimeoutException(
            "HTTPSConnectionPool(host='overwatch.blizzard.com', port=443): "
            "Read timed out. (read timeout=10)",
        ),
    ):
        response = client.get(
            f"/players/TeKrop-2217{uri}",
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
@pytest.mark.parametrize(("uri"), [("/stats"), ("/stats/career")])
def test_get_player_stats_blizzard_forbidden_error(client: TestClient, uri: str):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(
            status_code=status.HTTP_403_FORBIDDEN,
            text="403 Forbidden",
        ),
    ):
        response = client.get(
            f"/players/TeKrop-2217{uri}",
            params={"gamemode": PlayerGamemode.QUICKPLAY},
        )

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert (
        "Blizzard is temporarily rate limiting this API. Please retry after"
        in response.json()["error"]
    )


@pytest.mark.parametrize("player_html_data", ["TeKrop-2217"], indirect=True)
@pytest.mark.parametrize(
    ("uri", "patch_target"),
    [
        (
            "/stats",
            "app.domain.services.player_service.PlayerService.get_player_stats",
        ),
        (
            "/stats/career",
            "app.domain.services.player_service.PlayerService.get_player_career_stats",
        ),
    ],
)
def test_get_player_stats_internal_error(
    client: TestClient, uri: str, patch_target: str
):
    with patch(
        patch_target,
        return_value=(
            {
                "ana": [{"category": "invalid_value", "stats": [{"key": "test"}]}],
            },
            False,
            0,
        ),
    ):
        response = client.get(
            f"/players/TeKrop-2217{uri}",
            params={"gamemode": PlayerGamemode.QUICKPLAY},
        )

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.json() == {"error": settings.internal_server_error_message}
