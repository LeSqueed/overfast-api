"""Fixtures for the StoragePort contract suite.

Each test in this package runs once per implementation of the port: the
in-memory ``FakeStorage`` the rest of the suite is built on, and the real
``PostgresStorage``. A contract the fake honours but the adapter cannot (or the
other way round) then fails here instead of only in production.

The PostgreSQL parameter skips — never fails, never silently passes — when no
server is reachable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from tests.adapters.storage.pg_testing import postgres_storage, throwaway_database
from tests.fake_storage import FakeStorage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from app.domain.ports.storage import StoragePort

FAKE = "fake"
POSTGRES = "postgres"


@pytest.fixture(scope="module")
def _postgres_database() -> Iterator[str]:
    """Name of a database created for this package and dropped after it."""
    with throwaway_database() as name:
        yield name


@pytest.fixture(params=[FAKE, POSTGRES])
def contract_database(request: pytest.FixtureRequest) -> str | None:
    """Database backing this run, or ``None`` when it runs against the fake.

    Deliberately synchronous: creating the database has to happen outside the
    asyncio runner that drives ``storage_db``. Deliberately lazy: the fake's run
    never asks for a database, so it still executes when none is reachable.
    """
    if request.param == FAKE:
        return None
    return request.getfixturevalue("_postgres_database")


@pytest_asyncio.fixture
async def storage_db(contract_database: str | None) -> AsyncIterator[StoragePort]:
    """Override the suite-wide FakeStorage fixture with one storage per parameter."""
    if contract_database is None:
        fake = FakeStorage()
        await fake.initialize()
        yield fake
        await fake.close()
        return

    async with postgres_storage(contract_database) as storage:
        yield storage
