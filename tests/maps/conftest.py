from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest
from fastapi import status

from tests.helpers import read_html_file

if TYPE_CHECKING:
    from _pytest.fixtures import SubRequest


@pytest.fixture(scope="package")
def rates_maps_html_data() -> str | None:
    return read_html_file("rates_map_dropdown.html")


@pytest.fixture(scope="module", autouse=True)
def _setup_maps_test(rates_maps_html_data: str | None):
    if rates_maps_html_data is None:
        yield
        return
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(status_code=status.HTTP_200_OK, text=rates_maps_html_data),
    ):
        yield
