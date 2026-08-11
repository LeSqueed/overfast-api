import argparse
import asyncio
import itertools
from unittest.mock import Mock, call, patch

import pytest
from fastapi import status
from httpx2 import AsyncClient

from app.config import settings
from app.domain.enums import HeroKey
from tests.helpers import players_ids
from tests.update_test_fixtures import (  # sourcery skip: dont-import-test-modules
    MAPS_FIXTURE_FILEPATH,
)
from tests.update_test_fixtures import (
    main as update_test_fixtures_main,
)

# A page the maps transform can actually trim down, so the maps flag exercises
# its real code path instead of the "dropdown not found" fallback.
BLIZZARD_PAGE_HTML = (
    "<html><body><main class='main-content'>"
    '<div id="herostats-filter-map"><select id="filter-map-select"></select></div>'
    "</main></body></html>"
)


@pytest.fixture(scope="module", autouse=True)
def _setup_update_test_fixtures_test():
    with (
        patch(
            "tests.update_test_fixtures.save_fixture_file",
            return_value=Mock(),
        ),
        patch("app.infrastructure.logger.logger.debug"),
    ):
        yield


test_data_path = f"{settings.test_fixtures_root_path}/html"
heroes_calls = [
    ("Updating {}{}...", test_data_path, "/heroes.html"),
    *[("Updating {}{}...", test_data_path, f"/heroes/{hero}.html") for hero in HeroKey],
]
players_calls = [
    ("Updating {}{}...", test_data_path, f"/players/{player}.html")
    for player in players_ids
]
home_calls = [("Updating {}{}...", test_data_path, "/home.html")]
maps_calls = [("Updating {}{}...", test_data_path, MAPS_FIXTURE_FILEPATH)]

# Every CLI flag of update_test_fixtures.parse_parameters, mapped to the log
# lines it is expected to produce. The Namespace is built from these keys, so a
# flag added to the script without being added here raises AttributeError
# instead of silently defaulting to on — which is what Mock() used to do.
FLAG_CALLS: dict[str, list[tuple[str, str, str]]] = {
    "heroes": heroes_calls,
    "home": home_calls,
    "maps": maps_calls,
    "players": players_calls,
}

FLAG_COMBINATIONS = [
    combination
    for size in range(1, len(FLAG_CALLS) + 1)
    for combination in itertools.combinations(FLAG_CALLS, size)
]


def _parameters(*enabled_flags: str) -> argparse.Namespace:
    """Build the parsed-args Namespace, with every flag outside ``enabled_flags`` off."""
    return argparse.Namespace(**{flag: flag in enabled_flags for flag in FLAG_CALLS})


@pytest.mark.parametrize("enabled_flags", FLAG_COMBINATIONS, ids="-".join)
def test_update_with_different_options(enabled_flags: tuple[str, ...]):
    expected_calls = [
        call(*args) for flag in enabled_flags for args in FLAG_CALLS[flag]
    ]
    unexpected_calls = [
        call(*args)
        for flag, flag_calls in FLAG_CALLS.items()
        if flag not in enabled_flags
        for args in flag_calls
    ]
    logger_info_mock = Mock()
    logger_error_mock = Mock()

    with (
        patch(
            "tests.update_test_fixtures.parse_parameters",
            return_value=_parameters(*enabled_flags),
        ),
        patch.object(
            AsyncClient,
            "get",
            return_value=Mock(
                status_code=status.HTTP_200_OK,
                text=BLIZZARD_PAGE_HTML,
            ),
        ),
        patch(
            "app.infrastructure.logger.logger.info",
            logger_info_mock,
        ),
        patch(
            "app.infrastructure.logger.logger.error",
            logger_error_mock,
        ),
    ):
        asyncio.run(update_test_fixtures_main())

    assert all(
        expected in logger_info_mock.call_args_list for expected in expected_calls
    )
    assert all(
        unexpected not in logger_info_mock.call_args_list
        for unexpected in unexpected_calls
    )
    logger_error_mock.assert_not_called()


def test_update_with_blizzard_error():
    logger_error_mock = Mock()

    with (
        patch(
            "tests.update_test_fixtures.parse_parameters",
            return_value=_parameters("maps"),
        ),
        patch.object(
            AsyncClient,
            "get",
            return_value=Mock(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                text="BLIZZARD_ERROR",
            ),
        ),
        patch(
            "app.infrastructure.logger.logger.info",
            Mock(),
        ),
        patch(
            "app.infrastructure.logger.logger.error",
            logger_error_mock,
        ),
    ):
        asyncio.run(update_test_fixtures_main())

    logger_error_mock.assert_called_with(
        "Error while getting the page : {}",
        "BLIZZARD_ERROR",
    )
