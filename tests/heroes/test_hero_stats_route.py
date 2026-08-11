import time
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import status

from app.api.routers.heroes import MAX_HISTORY_OFFSET
from app.config import settings
from app.domain.enums import (
    CompetitiveDivisionFilter,
    CompetitiveDivisionHistoryFilter,
    PlayerGamemode,
    PlayerPlatform,
    PlayerRegion,
    Role,
)
from app.domain.exceptions import ParserInternalError, ParserParsingError

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from tests.fake_storage import FakeStorage

_BASE_PARAMS = {
    "platform": PlayerPlatform.PC,
    "gamemode": PlayerGamemode.COMPETITIVE,
    "region": PlayerRegion.EUROPE,
}

_COMPETITIVE_PARAMS = {
    "platform": PlayerPlatform.PC,
    "gamemode": PlayerGamemode.COMPETITIVE,
    "region": PlayerRegion.EUROPE,
}


@pytest.fixture(scope="module", autouse=True)
def _setup_hero_stats_test(hero_stats_response_mock: Mock):
    with patch("httpx2.AsyncClient.get", return_value=hero_stats_response_mock):
        yield


def test_get_hero_stats_missing_parameters(client: TestClient):
    response = client.get("/heroes/stats")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_get_hero_stats_success(client: TestClient):
    response = client.get("/heroes/stats", params=_BASE_PARAMS)

    assert response.status_code == status.HTTP_200_OK
    assert len(response.json()) > 0


def test_get_hero_stats_response_shape(client: TestClient):
    response = client.get("/heroes/stats", params=_BASE_PARAMS)

    first = response.json()[0]

    assert set(first.keys()) == {"hero", "pickrate", "winrate", "banrate"}
    assert isinstance(first["hero"], str)
    assert isinstance(first["pickrate"], float)
    assert isinstance(first["winrate"], float)
    assert isinstance(first["banrate"], float)


def test_get_hero_stats_invalid_platform(client: TestClient):
    response = client.get(
        "/heroes/stats",
        params={**_BASE_PARAMS, "platform": "invalid_platform"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_get_hero_stats_invalid_gamemode(client: TestClient):
    response = client.get(
        "/heroes/stats",
        params={**_BASE_PARAMS, "gamemode": "invalid_gamemode"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_get_hero_stats_invalid_region(client: TestClient):
    response = client.get(
        "/heroes/stats",
        params={**_BASE_PARAMS, "region": "invalid_region"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize("role", [r.value for r in Role])
def test_get_hero_stats_filter_by_role(client: TestClient, role: str):
    response = client.get("/heroes/stats", params={**_BASE_PARAMS, "role": role})

    assert response.status_code == status.HTTP_200_OK
    assert len(response.json()) > 0


def test_get_hero_stats_filter_by_invalid_role(client: TestClient):
    response = client.get("/heroes/stats", params={**_BASE_PARAMS, "role": "invalid"})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize(
    "division",
    list(CompetitiveDivisionFilter),
)
def test_get_hero_stats_filter_by_competitive_division(
    client: TestClient, division: str
):
    response = client.get(
        "/heroes/stats",
        params={**_COMPETITIVE_PARAMS, "competitive_division": division},
    )

    assert response.status_code == status.HTTP_200_OK


def test_get_hero_stats_filter_by_invalid_competitive_division(client: TestClient):
    response = client.get(
        "/heroes/stats",
        params={**_COMPETITIVE_PARAMS, "competitive_division": "invalid"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize(
    "order_by",
    [
        "hero:asc",
        "hero:desc",
        "pickrate:asc",
        "pickrate:desc",
        "winrate:asc",
        "winrate:desc",
    ],
)
def test_get_hero_stats_order_by(client: TestClient, order_by: str):
    response = client.get(
        "/heroes/stats", params={**_BASE_PARAMS, "order_by": order_by}
    )

    assert response.status_code == status.HTTP_200_OK
    assert len(response.json()) > 0


@pytest.mark.parametrize(
    "order_by",
    ["invalid", "hero", "hero:invalid", "invalid:asc", "pickrate:asc:extra"],
)
def test_get_hero_stats_invalid_order_by(client: TestClient, order_by: str):
    response = client.get(
        "/heroes/stats", params={**_BASE_PARAMS, "order_by": order_by}
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_get_hero_stats_order_by_pickrate_desc_is_sorted(client: TestClient):
    response = client.get(
        "/heroes/stats",
        params={**_BASE_PARAMS, "order_by": "pickrate:desc"},
    )

    data = response.json()

    assert response.status_code == status.HTTP_200_OK
    pickrates = [hero["pickrate"] for hero in data]
    assert pickrates == sorted(pickrates, reverse=True)


def test_get_hero_stats_order_by_hero_asc_is_sorted(client: TestClient):
    response = client.get(
        "/heroes/stats",
        params={**_BASE_PARAMS, "order_by": "hero:asc"},
    )

    data = response.json()

    assert response.status_code == status.HTTP_200_OK
    heroes = [hero["hero"] for hero in data]
    assert heroes == sorted(heroes)


def test_get_hero_stats_blizzard_error(client: TestClient):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            text="Service Unavailable",
        ),
    ):
        response = client.get("/heroes/stats", params=_BASE_PARAMS)

    assert response.status_code == status.HTTP_504_GATEWAY_TIMEOUT
    assert response.json() == {
        "error": "Couldn't get Blizzard page (HTTP 503 error) : Service Unavailable",
    }


def test_get_hero_stats_internal_error(client: TestClient):
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats",
        return_value=([{"invalid_key": "invalid_value"}], False, 0),
    ):
        response = client.get("/heroes/stats", params=_BASE_PARAMS)

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.json() == {"error": settings.internal_server_error_message}


def test_get_hero_stats_released_hero_not_in_csv(client: TestClient):
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats",
        return_value=(
            [
                {
                    "hero": "brand-new-hero",
                    "pickrate": 3.3,
                    "winrate": 48.0,
                    "banrate": None,
                }
            ],
            False,
            0,
        ),
    ):
        response = client.get("/heroes/stats", params=_BASE_PARAMS)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()[0]["hero"] == "brand-new-hero"


def test_get_hero_stats_blizzard_forbidden_error(client: TestClient):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(
            status_code=status.HTTP_403_FORBIDDEN,
            text="403 Forbidden",
        ),
    ):
        response = client.get("/heroes/stats", params=_BASE_PARAMS)

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert (
        "Blizzard is temporarily rate limiting this API. Please retry after"
        in response.json()["error"]
    )


def test_get_hero_stats_blizzard_forbidden_error_and_caching(client: TestClient):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(status_code=status.HTTP_403_FORBIDDEN, text="403 Forbidden"),
    ):
        response1 = client.get("/heroes/stats", params=_BASE_PARAMS)
    response2 = client.get("/heroes/stats", params=_BASE_PARAMS)

    assert (
        response1.status_code
        == response2.status_code
        == status.HTTP_503_SERVICE_UNAVAILABLE
    )
    assert response1.json() == response2.json()
    assert (
        "Blizzard is temporarily rate limiting this API. Please retry after"
        in response1.json()["error"]
    )


def test_get_hero_stats_parser_parsing_error(client: TestClient):
    cause = ParserParsingError("unexpected JSON structure")
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats",
        side_effect=ParserInternalError(
            "https://overwatch.blizzard.com/en-us/rates/data/", cause
        ),
    ):
        response = client.get("/heroes/stats", params=_BASE_PARAMS)

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.json() == {"error": settings.internal_server_error_message}


@pytest.mark.parametrize(
    "map_filter", ["Hanaoka", "busan!", "../../etc/passwd", "a" * 51]
)
def test_get_hero_stats_malformed_map_is_rejected_locally(
    client: TestClient, map_filter: str
):
    with patch("httpx2.AsyncClient.get") as blizzard_get:
        response = client.get(
            "/heroes/stats", params={**_BASE_PARAMS, "map": map_filter}
        )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    blizzard_get.assert_not_called()


def test_get_hero_stats_unknown_but_well_formed_map_reaches_blizzard(
    client: TestClient,
):
    response = client.get(
        "/heroes/stats", params={**_BASE_PARAMS, "map": "brand-new-map"}
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "brand-new-map" in response.json()["error"]


# ---------------------------------------------------------------------------
# /heroes/stats/history
# ---------------------------------------------------------------------------

_CAPTURED_AT = 1700000000

_HISTORY_ROW = {
    "captured_at": _CAPTURED_AT,
    "platform": "pc",
    "gamemode": "competitive",
    "region": "europe",
    "map": "busan",
    "tier": "all",
    "hero": "ana",
    "pickrate": 5.5,
    "winrate": 52.3,
    "banrate": None,
}


def _single_await(mock: AsyncMock) -> Any:
    """Return the recorded call of a mock awaited exactly once."""
    mock.assert_awaited_once()
    return cast("Any", mock.await_args)


def _snapshot_row(**overrides: object) -> dict:
    """Build a snapshot row for FakeStorage (no captured_at — it's per batch)."""
    row = {
        "platform": "pc",
        "gamemode": "competitive",
        "region": "europe",
        "map": "busan",
        "tier": "all",
        "hero": "ana",
        "pickrate": 5.5,
        "winrate": 52.3,
        "banrate": 1.2,
    }
    return {**row, **overrides}


def test_get_hero_stats_history_missing_parameters(client: TestClient):
    response = client.get("/heroes/stats/history")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_get_hero_stats_history_success(client: TestClient):
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats_history",
        return_value=[
            _HISTORY_ROW,
            {**_HISTORY_ROW, "captured_at": 1700003600, "winrate": 53.1},
        ],
    ) as mock_history:
        response = client.get(
            "/heroes/stats/history",
            params={
                "platform": "pc",
                "gamemode": "competitive",
                "region": "europe",
                "map": "busan",
                "competitive_division": "all",
                "heroes": ["ana"],
                "since": 1700000000,
                "until": 1700003600,
            },
        )

    assert response.status_code == status.HTTP_200_OK
    mock_history.assert_awaited_once_with(
        platform="pc",
        gamemode="competitive",
        region="europe",
        map_key="busan",
        tier="all",
        heroes=["ana"],
        since=1700000000,
        until=1700003600,
        limit=1000,
        offset=0,
        cache_key=(
            "/heroes/stats/history?platform=pc&gamemode=competitive&region=europe"
            "&map=busan&competitive_division=all&heroes=ana"
            "&since=1700000000&until=1700003600"
        ),
    )
    data = response.json()
    assert len(data) == 2  # noqa: PLR2004
    assert set(data[0].keys()) == {
        "captured_at",
        "platform",
        "gamemode",
        "region",
        "map",
        "competitive_division",
        "hero",
        "pickrate",
        "winrate",
        "banrate",
    }
    assert data[1]["winrate"] == 53.1  # noqa: PLR2004


def test_get_hero_stats_history_serialises_captured_at_as_epoch_int(
    client: TestClient,
):
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats_history",
        return_value=[_HISTORY_ROW],
    ):
        response = client.get(
            "/heroes/stats/history",
            params={"platform": "pc", "gamemode": "competitive"},
        )

    captured_at = response.json()[0]["captured_at"]

    assert captured_at == _CAPTURED_AT
    assert isinstance(captured_at, int)


def test_get_hero_stats_history_optional_filters(client: TestClient):
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats_history",
        return_value=[],
    ) as mock_history:
        response = client.get(
            "/heroes/stats/history",
            params={
                "platform": "pc",
                "gamemode": "competitive",
                "since": 1700000000,
                "until": 1700003600,
            },
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == []
    mock_history.assert_awaited_once_with(
        platform="pc",
        gamemode="competitive",
        region=None,
        map_key=None,
        tier=None,
        heroes=None,
        since=1700000000,
        until=1700003600,
        limit=1000,
        offset=0,
        cache_key=(
            "/heroes/stats/history?platform=pc&gamemode=competitive"
            "&since=1700000000&until=1700003600"
        ),
    )


def test_get_hero_stats_history_defaults_since_to_a_bounded_window(
    client: TestClient,
):
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats_history",
        return_value=[],
    ) as mock_history:
        response = client.get(
            "/heroes/stats/history",
            params={"platform": "pc", "gamemode": "competitive", "until": 1700000000},
        )

    assert response.status_code == status.HTTP_200_OK
    assert _single_await(mock_history).kwargs["since"] == _CAPTURED_AT - 7 * 24 * 3600


def test_get_hero_stats_history_defaults_since_relative_to_now(client: TestClient):
    now = int(time.time())

    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats_history",
        return_value=[],
    ) as mock_history:
        response = client.get(
            "/heroes/stats/history",
            params={"platform": "pc", "gamemode": "competitive"},
        )

    assert response.status_code == status.HTTP_200_OK
    since = _single_await(mock_history).kwargs["since"]
    assert now - 7 * 24 * 3600 <= since <= now - 7 * 24 * 3600 + 10


def test_get_hero_stats_history_unknown_hero_and_map(client: TestClient):
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats_history",
        return_value=[],
    ) as mock_history:
        response = client.get(
            "/heroes/stats/history",
            params={
                "platform": "pc",
                "gamemode": "competitive",
                "map": "not-a-real-map",
                "heroes": ["not-a-real-hero"],
            },
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == []
    call = _single_await(mock_history)
    assert call.kwargs["map_key"] == "not-a-real-map"
    assert call.kwargs["heroes"] == ["not-a-real-hero"]


def test_get_hero_stats_history_released_hero_not_in_csv(client: TestClient):
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats_history",
        return_value=[{**_HISTORY_ROW, "hero": "brand-new-hero"}],
    ):
        response = client.get(
            "/heroes/stats/history",
            params={
                "platform": "pc",
                "gamemode": "competitive",
                "heroes": ["brand-new-hero"],
            },
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()[0]["hero"] == "brand-new-hero"


# ---------------------------------------------------------------------------
# /heroes/stats/history — parameter validation
# ---------------------------------------------------------------------------


def test_get_hero_stats_history_rejects_inverted_window(client: TestClient):
    response = client.get(
        "/heroes/stats/history",
        params={
            "platform": "pc",
            "gamemode": "competitive",
            "since": 1700003600,
            "until": 1700000000,
        },
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "must not be greater than" in response.json()["error"]


def test_get_hero_stats_history_accepts_equal_since_and_until(client: TestClient):
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats_history",
        return_value=[],
    ):
        response = client.get(
            "/heroes/stats/history",
            params={
                "platform": "pc",
                "gamemode": "competitive",
                "since": 1700000000,
                "until": 1700000000,
            },
        )

    assert response.status_code == status.HTTP_200_OK


@pytest.mark.parametrize("bound", ["since", "until"])
def test_get_hero_stats_history_rejects_out_of_range_timestamp(
    client: TestClient, bound: str
):
    response = client.get(
        "/heroes/stats/history",
        params={"platform": "pc", "gamemode": "competitive", bound: 99999999999999},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize("bound", ["since", "until"])
def test_get_hero_stats_history_rejects_negative_timestamp(
    client: TestClient, bound: str
):
    response = client.get(
        "/heroes/stats/history",
        params={"platform": "pc", "gamemode": "competitive", bound: -1},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize("division", list(CompetitiveDivisionHistoryFilter))
def test_get_hero_stats_history_accepts_known_competitive_division(
    client: TestClient, division: str
):
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats_history",
        return_value=[],
    ) as mock_history:
        response = client.get(
            "/heroes/stats/history",
            params={
                "platform": "pc",
                "gamemode": "competitive",
                "competitive_division": division,
            },
        )

    assert response.status_code == status.HTTP_200_OK
    assert _single_await(mock_history).kwargs["tier"] == str(division)


@pytest.mark.parametrize("division", ["goldd", "ultimate", "GOLD", "", "all-divisions"])
def test_get_hero_stats_history_rejects_unknown_competitive_division(
    client: TestClient, division: str
):
    response = client.get(
        "/heroes/stats/history",
        params={
            "platform": "pc",
            "gamemode": "competitive",
            "competitive_division": division,
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize("path", ["/heroes/stats/history", "/heroes/stats/dates"])
def test_history_competitive_division_renders_as_a_single_enum(
    client: TestClient, path: str
):
    spec = client.get("/openapi.json").json()

    parameters = spec["paths"][path]["get"]["parameters"]
    schema = next(
        parameter["schema"]
        for parameter in parameters
        if parameter["name"] == "competitive_division"
    )

    assert schema["anyOf"] == [
        {"$ref": "#/components/schemas/CompetitiveDivisionHistoryFilter"},
        {"type": "null"},
    ]
    assert spec["components"]["schemas"]["CompetitiveDivisionHistoryFilter"][
        "enum"
    ] == [division.value for division in CompetitiveDivisionHistoryFilter]


def test_get_hero_stats_history_rejects_oversized_heroes_list(client: TestClient):
    response = client.get(
        "/heroes/stats/history",
        params={
            "platform": "pc",
            "gamemode": "competitive",
            "heroes": [f"hero-{index}" for index in range(101)],
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_get_hero_stats_history_rejects_malformed_map(client: TestClient):
    response = client.get(
        "/heroes/stats/history",
        params={"platform": "pc", "gamemode": "competitive", "map": "Busan!"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize(
    ("limit", "offset"),
    [(0, 0), (-1, 0), (50001, 0), (10, -1), (10, MAX_HISTORY_OFFSET + 1)],
)
def test_get_hero_stats_history_rejects_out_of_bound_paging(
    client: TestClient, limit: int, offset: int
):
    response = client.get(
        "/heroes/stats/history",
        params={
            "platform": "pc",
            "gamemode": "competitive",
            "limit": limit,
            "offset": offset,
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_get_hero_stats_history_allows_a_page_past_the_storage_row_ceiling(
    client: TestClient,
):
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats_history",
        return_value=[],
    ) as mock_history:
        response = client.get(
            "/heroes/stats/history",
            params={
                "platform": "pc",
                "gamemode": "competitive",
                "limit": 1000,
                "offset": 50000,
            },
        )

    assert response.status_code == status.HTTP_200_OK
    assert _single_await(mock_history).kwargs["offset"] == 50000  # noqa: PLR2004


def test_get_hero_stats_history_forwards_the_page_unchanged_to_the_service(
    client: TestClient,
):
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats_history",
        return_value=[],
    ) as mock_history:
        response = client.get(
            "/heroes/stats/history",
            params={
                "platform": "pc",
                "gamemode": "competitive",
                "limit": 25,
                "offset": 100,
            },
        )

    assert response.status_code == status.HTTP_200_OK
    call = _single_await(mock_history)
    assert call.kwargs["limit"] == 25  # noqa: PLR2004
    assert call.kwargs["offset"] == 100  # noqa: PLR2004


# ---------------------------------------------------------------------------
# /heroes/stats/history — end-to-end through the real service and storage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_hero_stats_history_end_to_end(
    client: TestClient, storage_db: FakeStorage
):
    await storage_db.store_hero_stats_snapshots(
        1700000000, [_snapshot_row(), _snapshot_row(hero="genji", banrate=None)]
    )

    response = client.get(
        "/heroes/stats/history",
        params={"platform": "pc", "gamemode": "competitive", "since": 1699999999},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == [
        {
            "captured_at": 1700000000,
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "competitive_division": "all",
            "hero": "ana",
            "pickrate": 5.5,
            "winrate": 52.3,
            "banrate": 1.2,
        },
        {
            "captured_at": 1700000000,
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "competitive_division": "all",
            "hero": "genji",
            "pickrate": 5.5,
            "winrate": 52.3,
            "banrate": None,
        },
    ]


@pytest.mark.asyncio
async def test_get_hero_stats_history_end_to_end_filters_by_division(
    client: TestClient, storage_db: FakeStorage
):
    await storage_db.store_hero_stats_snapshots(
        1700000000,
        [_snapshot_row(tier="all"), _snapshot_row(tier="gold", winrate=61.0)],
    )

    response = client.get(
        "/heroes/stats/history",
        params={
            "platform": "pc",
            "gamemode": "competitive",
            "competitive_division": "gold",
            "since": 1699999999,
        },
    )

    data = response.json()

    assert response.status_code == status.HTTP_200_OK
    assert len(data) == 1
    assert data[0]["competitive_division"] == "gold"
    assert data[0]["winrate"] == 61.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_hero_stats_history_end_to_end_respects_the_default_window(
    client: TestClient, storage_db: FakeStorage
):
    now = int(time.time())
    await storage_db.store_hero_stats_snapshots(now - 30 * 24 * 3600, [_snapshot_row()])
    await storage_db.store_hero_stats_snapshots(now - 3600, [_snapshot_row()])

    response = client.get(
        "/heroes/stats/history",
        params={"platform": "pc", "gamemode": "competitive"},
    )

    data = response.json()

    assert response.status_code == status.HTTP_200_OK
    assert [point["captured_at"] for point in data] == [now - 3600]


@pytest.mark.asyncio
async def test_get_hero_stats_history_end_to_end_pages_with_limit_and_offset(
    client: TestClient, storage_db: FakeStorage
):
    await storage_db.store_hero_stats_snapshots(
        1700000000,
        [_snapshot_row(hero=hero) for hero in ("ana", "genji", "mercy")],
    )
    params = {"platform": "pc", "gamemode": "competitive", "since": 1699999999}

    first_page = client.get(
        "/heroes/stats/history", params={**params, "limit": 2, "offset": 0}
    )
    second_page = client.get(
        "/heroes/stats/history", params={**params, "limit": 2, "offset": 2}
    )

    assert [point["hero"] for point in first_page.json()] == ["ana", "genji"]
    assert [point["hero"] for point in second_page.json()] == ["mercy"]


@pytest.mark.asyncio
async def test_get_hero_stats_history_dates_end_to_end(
    client: TestClient, storage_db: FakeStorage
):
    await storage_db.store_hero_stats_snapshots(1700000000, [_snapshot_row()])
    await storage_db.store_hero_stats_snapshots(1700003600, [_snapshot_row()])

    response = client.get(
        "/heroes/stats/dates",
        params={"platform": "pc", "gamemode": "competitive"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == [1700003600, 1700000000]


@pytest.mark.asyncio
async def test_get_hero_stats_dates_output_is_accepted_as_history_input(
    client: TestClient, storage_db: FakeStorage
):
    await storage_db.store_hero_stats_snapshots(1700000000, [_snapshot_row()])
    dates = client.get(
        "/heroes/stats/dates", params={"platform": "pc", "gamemode": "competitive"}
    ).json()

    response = client.get(
        "/heroes/stats/history",
        params={
            "platform": "pc",
            "gamemode": "competitive",
            "since": dates[0],
            "until": dates[0],
        },
    )

    assert response.status_code == status.HTTP_200_OK
    assert [point["captured_at"] for point in response.json()] == dates


# ---------------------------------------------------------------------------
# /heroes/stats/history — API cache population
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_hero_stats_history_populates_the_advertised_cache_key(
    client: TestClient, storage_db: FakeStorage
):
    await storage_db.store_hero_stats_snapshots(1700000000, [_snapshot_row()])

    with patch(
        "app.domain.services.hero_service.HeroService._update_api_cache",
        new_callable=AsyncMock,
    ) as mock_cache:
        response = client.get(
            "/heroes/stats/history",
            params={"platform": "pc", "gamemode": "competitive", "since": 1699999999},
        )

    assert response.headers["X-Cache-Status"] == "hit"
    call = _single_await(mock_cache)
    assert call.args[0] == (
        "/heroes/stats/history?platform=pc&gamemode=competitive&since=1699999999"
    )
    assert call.args[1] == storage_db._hero_stats_snapshots
    assert call.args[2] == settings.hero_stats_history_cache_timeout


@pytest.mark.asyncio
async def test_get_hero_stats_history_does_not_cache_an_empty_result(
    client: TestClient,
):
    with patch(
        "app.domain.services.hero_service.HeroService._update_api_cache",
        new_callable=AsyncMock,
    ) as mock_cache:
        response = client.get(
            "/heroes/stats/history",
            params={"platform": "pc", "gamemode": "competitive"},
        )

    assert response.json() == []
    mock_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_hero_stats_dates_does_not_cache_an_empty_result(
    client: TestClient,
):
    with patch(
        "app.domain.services.hero_service.HeroService._update_api_cache",
        new_callable=AsyncMock,
    ) as mock_cache:
        response = client.get(
            "/heroes/stats/dates",
            params={"platform": "pc", "gamemode": "competitive"},
        )

    assert response.json() == []
    mock_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_hero_stats_dates_caches_with_the_history_timeout(
    client: TestClient, storage_db: FakeStorage
):
    await storage_db.store_hero_stats_snapshots(1700000000, [_snapshot_row()])

    with patch(
        "app.domain.services.hero_service.HeroService._update_api_cache",
        new_callable=AsyncMock,
    ) as mock_cache:
        response = client.get(
            "/heroes/stats/dates",
            params={"platform": "pc", "gamemode": "competitive"},
        )

    assert response.status_code == status.HTTP_200_OK
    assert (
        _single_await(mock_cache).args[2] == settings.hero_stats_history_cache_timeout
    )


@pytest.mark.parametrize("path", ["/heroes/stats/history", "/heroes/stats/dates"])
@pytest.mark.asyncio
async def test_history_endpoints_advertise_the_history_cache_timeout(
    client: TestClient, storage_db: FakeStorage, path: str
):
    await storage_db.store_hero_stats_snapshots(1700000000, [_snapshot_row()])

    response = client.get(
        path,
        params={"platform": "pc", "gamemode": "competitive", "since": 1699999999},
    )

    assert response.headers[settings.cache_ttl_header] == str(
        settings.hero_stats_history_cache_timeout
    )


# ---------------------------------------------------------------------------
# /heroes/stats/dates
# ---------------------------------------------------------------------------


def test_get_hero_stats_history_dates_success(client: TestClient):
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats_history_dates",
        return_value=[1786206744, 1786120344],
    ) as mock_dates:
        response = client.get(
            "/heroes/stats/dates",
            params={
                "platform": "pc",
                "gamemode": "competitive",
                "region": "europe",
                "map": "busan",
                "competitive_division": "all",
            },
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == [1786206744, 1786120344]
    mock_dates.assert_awaited_once_with(
        platform="pc",
        gamemode="competitive",
        region="europe",
        map_key="busan",
        tier="all",
        cache_key=(
            "/heroes/stats/dates?platform=pc&gamemode=competitive&region=europe"
            "&map=busan&competitive_division=all"
        ),
    )


def test_get_hero_stats_history_dates_minimal_params(client: TestClient):
    with patch(
        "app.domain.services.hero_service.HeroService.get_hero_stats_history_dates",
        return_value=[],
    ) as mock_dates:
        response = client.get(
            "/heroes/stats/dates",
            params={"platform": "pc", "gamemode": "competitive"},
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == []
    mock_dates.assert_awaited_once_with(
        platform="pc",
        gamemode="competitive",
        region=None,
        map_key=None,
        tier=None,
        cache_key="/heroes/stats/dates?platform=pc&gamemode=competitive",
    )


def test_get_hero_stats_history_dates_rejects_unknown_competitive_division(
    client: TestClient,
):
    response = client.get(
        "/heroes/stats/dates",
        params={
            "platform": "pc",
            "gamemode": "competitive",
            "competitive_division": "goldd",
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_get_hero_stats_history_dates_rejects_malformed_map(client: TestClient):
    response = client.get(
        "/heroes/stats/dates",
        params={"platform": "pc", "gamemode": "competitive", "map": "Busan!"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


# ---------------------------------------------------------------------------
# Documented responses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/heroes/stats/history", "/heroes/stats/dates"])
def test_storage_only_routes_do_not_document_blizzard_errors(
    client: TestClient, path: str
):
    schema = client.get("/openapi.json").json()

    documented = set(schema["paths"][path]["get"]["responses"])

    assert "503" not in documented
    assert "504" not in documented
    assert {"200", "429", "500"} <= documented
