"""Helpers for exercising the real PostgresStorage against a throwaway database.

Shared by the PostgresStorage integration suite and the StoragePort contract
suite, so both create their scratch database the same way and skip — never
fail — identically when no PostgreSQL server is reachable.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

import asyncpg
import pytest

from app.adapters.storage.postgres_storage import PostgresStorage
from app.config import settings
from app.infrastructure.metaclasses import Singleton

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

CONNECT_TIMEOUT = 5.0


async def _create_database(name: str) -> None:
    """Create a database of our own, so the app's real tables are never touched."""
    conn = await asyncpg.connect(dsn=settings.postgres_dsn, timeout=CONNECT_TIMEOUT)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop_database(name: str) -> None:
    """Drop the throwaway database, disconnecting any pool that outlived a test."""
    conn = await asyncpg.connect(dsn=settings.postgres_dsn, timeout=CONNECT_TIMEOUT)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


@contextmanager
def throwaway_database() -> Iterator[str]:
    """Yield the name of a database created here and dropped on exit.

    Skips the caller when the server is unreachable (no stack running, wrong
    credentials, missing database): tests using a real database are additive
    signal, never a prerequisite for running the suite.
    """
    name = f"overfast_test_{uuid.uuid4().hex[:12]}"
    try:
        asyncio.run(_create_database(name))
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL is not reachable: {exc!r}")

    try:
        yield name
    finally:
        asyncio.run(_drop_database(name))


@asynccontextmanager
async def postgres_storage(database: str) -> AsyncIterator[PostgresStorage]:
    """Yield a real PostgresStorage bound to ``database``, schema applied and empty."""
    Singleton.clear_all()

    with patch.object(settings, "postgres_db", database):
        storage = PostgresStorage()
        await storage.initialize()

    await storage.clear_all_data()

    try:
        yield storage
    finally:
        await storage.close()
