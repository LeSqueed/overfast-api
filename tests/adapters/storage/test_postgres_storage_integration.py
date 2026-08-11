"""Integration tests exercising the real PostgresStorage SQL against a live database.

Every other storage test asserts against ``AsyncMock`` and inspects the generated
SQL as strings, so a statement that is syntactically fine but semantically wrong
still passes. These tests apply ``app/adapters/storage/schema.sql`` to a
throwaway database and round-trip actual behaviour instead.

The whole module skips — never fails — when no PostgreSQL is reachable, so
contributors running the suite without the compose stack are not blocked.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING
from unittest.mock import patch

import asyncpg
import pytest
import pytest_asyncio

from app.adapters.storage.postgres_storage import PostgresStorage
from app.config import settings
from app.infrastructure.metaclasses import Singleton

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

pytestmark = pytest.mark.integration

CONNECT_TIMEOUT = 5.0
LEASE_SECONDS = 3600

PLATFORM = "pc"
GAMEMODE = "competitive"

CAPTURED_AT = 1_767_225_600  # 2026-01-01T00:00:00Z
CAPTURED_AT_LATER = CAPTURED_AT + 86_400
CAPTURED_AT_LATEST = CAPTURED_AT + 172_800


async def _create_throwaway_database(name: str) -> None:
    """Create a database of our own, so the app's real tables are never touched."""
    conn = await asyncpg.connect(dsn=settings.postgres_dsn, timeout=CONNECT_TIMEOUT)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop_throwaway_database(name: str) -> None:
    """Drop the throwaway database, disconnecting any pool that outlived a test."""
    conn = await asyncpg.connect(dsn=settings.postgres_dsn, timeout=CONNECT_TIMEOUT)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def throwaway_database() -> Iterator[str]:
    """Yield the name of a database created for this module and dropped after it.

    Skips the module when the server is unreachable (no stack running, wrong
    credentials, missing database): these tests are additive signal, never a
    prerequisite for running the suite.
    """
    name = f"overfast_integration_{uuid.uuid4().hex[:12]}"
    try:
        asyncio.run(_create_throwaway_database(name))
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL is not reachable: {exc!r}")

    yield name

    asyncio.run(_drop_throwaway_database(name))


@pytest_asyncio.fixture
async def pg_storage(throwaway_database: str) -> AsyncIterator[PostgresStorage]:
    """Real PostgresStorage bound to the throwaway database, with schema.sql applied."""
    Singleton.clear_all()

    with patch.object(settings, "postgres_db", throwaway_database):
        storage = PostgresStorage()
        await storage.initialize()

    await storage.clear_all_data()

    yield storage

    await storage.close()


def _row(**overrides: str | float | None) -> dict:
    """Build one snapshot row, defaulting every grid column to a fixed value."""
    return {
        "platform": PLATFORM,
        "gamemode": GAMEMODE,
        "region": "europe",
        "map": "all-maps",
        "tier": "all",
        "hero": "ana",
        "pickrate": 22.3,
        "winrate": 43.1,
        "banrate": 1.9,
        **overrides,
    }


async def _fetch_run(
    storage: PostgresStorage, captured_at: int
) -> asyncpg.Record | None:
    """Read a snapshot run row straight from the database."""
    async with storage._pool.acquire() as conn:
        return await conn.fetchrow(
            """SELECT started_at, completed_at, row_count, skipped_count
               FROM hero_stats_snapshot_runs WHERE captured_at = $1""",
            PostgresStorage._to_utc(captured_at),
        )


async def _store_snapshots_of_two_ages(storage: PostgresStorage) -> None:
    """Store one snapshot well outside a one-hour window and one well inside it."""
    now = int(time.time())
    await storage.store_hero_stats_snapshots(now - 90_000, [_row(hero="ana")])
    await storage.store_hero_stats_snapshots(now - 100, [_row(hero="genji")])


@pytest.mark.asyncio
async def test_store_hero_stats_snapshots_round_trips_every_column(
    pg_storage: PostgresStorage,
):
    rows = [
        _row(hero="ana"),
        _row(hero="genji", pickrate=10.4, winrate=49.8, banrate=None),
    ]
    await pg_storage.store_hero_stats_snapshots(CAPTURED_AT, rows)

    result = await pg_storage.get_hero_stats_history(
        platform=PLATFORM, gamemode=GAMEMODE
    )

    assert [
        (
            row["captured_at"],
            row["platform"],
            row["gamemode"],
            row["region"],
            row["map"],
            row["tier"],
            row["hero"],
            row["pickrate"],
            row["winrate"],
            row["banrate"],
        )
        for row in result
    ] == [
        (
            CAPTURED_AT,
            PLATFORM,
            GAMEMODE,
            "europe",
            "all-maps",
            "all",
            "ana",
            22.3,
            43.1,
            1.9,
        ),
        (
            CAPTURED_AT,
            PLATFORM,
            GAMEMODE,
            "europe",
            "all-maps",
            "all",
            "genji",
            10.4,
            49.8,
            None,
        ),
    ]


@pytest.mark.asyncio
async def test_get_hero_stats_history_treats_an_empty_heroes_list_as_no_filter(
    pg_storage: PostgresStorage,
):
    await pg_storage.store_hero_stats_snapshots(
        CAPTURED_AT, [_row(hero="ana"), _row(hero="genji")]
    )

    result = await pg_storage.get_hero_stats_history(
        platform=PLATFORM, gamemode=GAMEMODE, heroes=[]
    )

    assert [row["hero"] for row in result] == ["ana", "genji"]


@pytest.mark.asyncio
async def test_get_hero_stats_history_keeps_only_the_listed_heroes(
    pg_storage: PostgresStorage,
):
    await pg_storage.store_hero_stats_snapshots(
        CAPTURED_AT, [_row(hero="ana"), _row(hero="genji"), _row(hero="mercy")]
    )

    result = await pg_storage.get_hero_stats_history(
        platform=PLATFORM, gamemode=GAMEMODE, heroes=["genji", "mercy"]
    )

    assert [row["hero"] for row in result] == ["genji", "mercy"]


@pytest.mark.asyncio
async def test_get_hero_stats_history_orders_by_captured_at_map_tier_then_hero(
    pg_storage: PostgresStorage,
):
    await pg_storage.store_hero_stats_snapshots(
        CAPTURED_AT_LATER, [_row(map="busan", tier="master", hero="ana")]
    )
    await pg_storage.store_hero_stats_snapshots(
        CAPTURED_AT,
        [
            _row(map="ilios", tier="all", hero="genji"),
            _row(map="busan", tier="master", hero="zenyatta"),
            _row(map="busan", tier="master", hero="ana"),
            _row(map="busan", tier="diamond", hero="zenyatta"),
        ],
    )

    result = await pg_storage.get_hero_stats_history(
        platform=PLATFORM, gamemode=GAMEMODE
    )

    assert [
        (row["captured_at"], row["map"], row["tier"], row["hero"]) for row in result
    ] == [
        (CAPTURED_AT, "busan", "diamond", "zenyatta"),
        (CAPTURED_AT, "busan", "master", "ana"),
        (CAPTURED_AT, "busan", "master", "zenyatta"),
        (CAPTURED_AT, "ilios", "all", "genji"),
        (CAPTURED_AT_LATER, "busan", "master", "ana"),
    ]


@pytest.mark.asyncio
async def test_get_hero_stats_history_bounds_captured_at_with_since_and_until(
    pg_storage: PostgresStorage,
):
    for captured_at in (CAPTURED_AT, CAPTURED_AT_LATER, CAPTURED_AT_LATEST):
        await pg_storage.store_hero_stats_snapshots(captured_at, [_row()])

    result = await pg_storage.get_hero_stats_history(
        platform=PLATFORM,
        gamemode=GAMEMODE,
        since=CAPTURED_AT_LATER,
        until=CAPTURED_AT_LATER,
    )

    assert [row["captured_at"] for row in result] == [CAPTURED_AT_LATER]


@pytest.mark.asyncio
async def test_store_hero_stats_snapshots_overwrites_a_repeated_grid_cell(
    pg_storage: PostgresStorage,
):
    await pg_storage.store_hero_stats_snapshots(
        CAPTURED_AT, [_row(pickrate=1.1, winrate=2.2, banrate=3.3)]
    )

    await pg_storage.store_hero_stats_snapshots(
        CAPTURED_AT, [_row(pickrate=9.9, winrate=8.8, banrate=None)]
    )

    result = await pg_storage.get_hero_stats_history(
        platform=PLATFORM, gamemode=GAMEMODE
    )
    assert [(row["pickrate"], row["winrate"], row["banrate"]) for row in result] == [
        (9.9, 8.8, None)
    ]


@pytest.mark.asyncio
async def test_claim_hero_stats_snapshot_run_reserves_the_slot(
    pg_storage: PostgresStorage,
):
    first_claim = await pg_storage.claim_hero_stats_snapshot_run(
        CAPTURED_AT, lease_seconds=LEASE_SECONDS
    )

    second_claim = await pg_storage.claim_hero_stats_snapshot_run(
        CAPTURED_AT, lease_seconds=LEASE_SECONDS
    )

    assert first_claim is True
    assert second_claim is False


@pytest.mark.asyncio
async def test_claim_hero_stats_snapshot_run_takes_over_an_expired_lease(
    pg_storage: PostgresStorage,
):
    await pg_storage.claim_hero_stats_snapshot_run(
        CAPTURED_AT, lease_seconds=LEASE_SECONDS
    )

    result = await pg_storage.claim_hero_stats_snapshot_run(
        CAPTURED_AT, lease_seconds=0
    )

    assert result is True


@pytest.mark.asyncio
async def test_claim_hero_stats_snapshot_run_stands_down_on_a_completed_run(
    pg_storage: PostgresStorage,
):
    await pg_storage.claim_hero_stats_snapshot_run(
        CAPTURED_AT, lease_seconds=LEASE_SECONDS
    )
    await pg_storage.complete_hero_stats_snapshot_run(
        CAPTURED_AT, row_count=1, skipped_count=0
    )

    result = await pg_storage.claim_hero_stats_snapshot_run(
        CAPTURED_AT, lease_seconds=0
    )

    assert result is False


@pytest.mark.asyncio
async def test_complete_hero_stats_snapshot_run_records_its_counts(
    pg_storage: PostgresStorage,
):
    await pg_storage.claim_hero_stats_snapshot_run(
        CAPTURED_AT, lease_seconds=LEASE_SECONDS
    )
    await pg_storage.complete_hero_stats_snapshot_run(
        CAPTURED_AT, row_count=42, skipped_count=3
    )

    run = await _fetch_run(pg_storage, CAPTURED_AT)

    assert run is not None
    assert (run["row_count"], run["skipped_count"]) == (42, 3)
    assert run["completed_at"] is not None


@pytest.mark.asyncio
async def test_get_hero_stats_history_dates_hides_timestamps_of_unfinished_runs(
    pg_storage: PostgresStorage,
):
    await pg_storage.store_hero_stats_snapshots(CAPTURED_AT, [_row()])
    await pg_storage.store_hero_stats_snapshots(CAPTURED_AT_LATER, [_row()])
    await pg_storage.claim_hero_stats_snapshot_run(
        CAPTURED_AT_LATER, lease_seconds=LEASE_SECONDS
    )
    await pg_storage.complete_hero_stats_snapshot_run(
        CAPTURED_AT_LATER, row_count=1, skipped_count=0
    )
    await pg_storage.store_hero_stats_snapshots(CAPTURED_AT_LATEST, [_row()])
    await pg_storage.claim_hero_stats_snapshot_run(
        CAPTURED_AT_LATEST, lease_seconds=LEASE_SECONDS
    )

    result = await pg_storage.get_hero_stats_history_dates(
        platform=PLATFORM, gamemode=GAMEMODE
    )

    assert result == [CAPTURED_AT_LATER, CAPTURED_AT]


@pytest.mark.asyncio
async def test_delete_old_hero_stats_snapshots_reports_the_deleted_count(
    pg_storage: PostgresStorage,
):
    await _store_snapshots_of_two_ages(pg_storage)

    deleted = await pg_storage.delete_old_hero_stats_snapshots(max_age_seconds=3600)

    assert deleted == 1


@pytest.mark.asyncio
async def test_delete_old_hero_stats_snapshots_keeps_rows_inside_the_window(
    pg_storage: PostgresStorage,
):
    await _store_snapshots_of_two_ages(pg_storage)
    await pg_storage.delete_old_hero_stats_snapshots(max_age_seconds=3600)

    result = await pg_storage.get_hero_stats_history(
        platform=PLATFORM, gamemode=GAMEMODE
    )

    assert [row["hero"] for row in result] == ["genji"]
