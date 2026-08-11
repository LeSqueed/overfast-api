"""PostgreSQL storage adapter with zstd compression for player profiles"""

from __future__ import annotations

import asyncio
import datetime
import json
import time
from compression import zstd
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg

from app.config import settings
from app.domain.ports.storage import MAX_HERO_STATS_HISTORY_ROWS
from app.infrastructure.logger import logger
from app.infrastructure.metaclasses import Singleton
from app.monitoring.metrics import (
    storage_connection_errors_total,
    track_storage_operation,
)

if TYPE_CHECKING:
    from app.domain.ports.storage import StaticDataCategory

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()

# Tables whose on-disk footprint is reported by ``get_stats``.
_SIZED_TABLES = ["player_profiles", "static_data", "hero_stats_snapshots"]

type _QueryParam = str | int | list[str]


class PostgresStorage(metaclass=Singleton):
    """
    PostgreSQL storage adapter for persistent data.

    Provides persistent storage for:
    - Static data (heroes, maps, gamemodes, roles) as JSONB
    - Player profiles with zstd-compressed HTML

    Uses Singleton pattern to ensure a single connection pool across the application.
    """

    def __init__(self) -> None:
        self._initialized = False
        self._init_lock = asyncio.Lock()

    @staticmethod
    async def _init_connection(conn: asyncpg.Connection) -> None:
        """Register JSON codec so JSONB columns accept/return Python dicts/lists."""
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    _MAX_POOL_CREATION_ATTEMPTS = 3

    async def initialize(self) -> None:
        """Create the connection pool and ensure schema exists."""
        async with self._init_lock:
            if self._initialized:
                return

            for attempt in range(1, self._MAX_POOL_CREATION_ATTEMPTS + 1):
                try:
                    self._pool: asyncpg.Pool = await asyncpg.create_pool(
                        dsn=settings.postgres_dsn,
                        min_size=settings.postgres_pool_min_size,
                        max_size=settings.postgres_pool_max_size,
                        init=self._init_connection,
                    )
                    break
                except Exception as exc:
                    if attempt == self._MAX_POOL_CREATION_ATTEMPTS:
                        if settings.prometheus_enabled:
                            storage_connection_errors_total.labels(
                                error_type="pool_creation"
                            ).inc()
                        logger.error("Failed to create PostgreSQL pool: {}", exc)
                        raise
                    logger.warning(
                        "PostgreSQL pool creation attempt {}/{} failed: {}. Retrying in 2s…",
                        attempt,
                        self._MAX_POOL_CREATION_ATTEMPTS,
                        exc,
                    )
                    await asyncio.sleep(2)

            await self._create_schema()
            self._initialized = True
            logger.info("PostgreSQL storage initialized")

    async def _create_schema(self) -> None:
        """Create enum type and tables if they don't exist."""
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            await conn.execute(_SCHEMA_SQL)

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()
        self._initialized = False

    # ------------------------------------------------------------------ #
    # Compression helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _compress(data: str) -> bytes:
        return zstd.compress(data.encode("utf-8"))

    @staticmethod
    def _decompress(data: bytes) -> str:
        return zstd.decompress(data).decode("utf-8")

    # ------------------------------------------------------------------ #
    # Static data
    # ------------------------------------------------------------------ #

    @track_storage_operation("static_data", "get")
    async def get_static_data(self, key: str) -> dict | None:
        """Get static data by key. Returns dict with 'data' (decompressed str),
        'category', 'updated_at' (Unix int), 'data_version' or None."""
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(
                """SELECT data, category, updated_at, data_version
                   FROM static_data WHERE key = $1""",
                key,
            )
        if row is None:
            return None

        decompressed_data = self._decompress(row["data"])
        return {
            "data": decompressed_data,
            "category": row["category"],
            "updated_at": int(row["updated_at"].timestamp()),
            "data_version": row["data_version"],
        }

    @track_storage_operation("static_data", "set")
    async def set_static_data(
        self,
        key: str,
        data: str,
        category: StaticDataCategory,
        data_version: int = 1,
    ) -> None:
        """Upsert static data. ``data`` is a raw string (HTML or JSON) compressed with zstd."""
        compressed = self._compress(data)
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            await conn.execute(
                """INSERT INTO static_data (key, data, category, data_version, updated_at)
                   VALUES ($1, $2, $3::static_data_category, $4, NOW())
                   ON CONFLICT (key) DO UPDATE
                   SET data = EXCLUDED.data,
                       category = EXCLUDED.category,
                       data_version = EXCLUDED.data_version,
                       updated_at = NOW()""",
                key,
                compressed,
                category.value,
                data_version,
            )

    # ------------------------------------------------------------------ #
    # Player profiles
    # ------------------------------------------------------------------ #

    @track_storage_operation("player_profiles", "get")
    async def get_player_profile(self, player_id: str) -> dict | None:
        """Get player profile by player_id.

        Returns dict with 'html', 'summary' (dict), 'battletag', 'name',
        'last_updated_blizzard', 'updated_at' (Unix int), 'data_version'
        or None if not found.
        """
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(
                """SELECT battletag, name, html_compressed, summary,
                          last_updated_blizzard, updated_at, data_version
                   FROM player_profiles WHERE player_id = $1""",
                player_id,
            )
        if row is None:
            return None

        summary = row["summary"] if row["summary"] is not None else {}
        if not summary:
            summary = {"url": player_id, "lastUpdated": row["last_updated_blizzard"]}

        return {
            "html": self._decompress(row["html_compressed"]),
            "battletag": row["battletag"],
            "name": row["name"],
            "summary": summary,
            "last_updated_blizzard": row["last_updated_blizzard"],
            "updated_at": int(row["updated_at"].timestamp()),
            "data_version": row["data_version"],
        }

    @track_storage_operation("player_profiles", "get")
    async def get_player_id_by_battletag(self, battletag: str) -> str | None:
        """Get Blizzard ID (player_id) for a given BattleTag."""
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(
                "SELECT player_id FROM player_profiles WHERE battletag = $1",
                battletag,
            )
        return row["player_id"] if row else None

    @track_storage_operation("player_profiles", "set")
    async def set_player_profile(
        self,
        player_id: str,
        html: str,
        summary: dict | None = None,
        battletag: str | None = None,
        name: str | None = None,
        last_updated_blizzard: int | None = None,
        data_version: int = 1,
    ) -> None:
        """Upsert player profile. HTML is zstd-compressed before storage."""
        if summary and last_updated_blizzard is None:
            last_updated_blizzard = summary.get("lastUpdated")

        compressed = self._compress(html)

        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            await conn.execute(
                """INSERT INTO player_profiles
                       (player_id, battletag, name, html_compressed, summary,
                        last_updated_blizzard, data_version, updated_at)
                   VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, NOW())
                   ON CONFLICT (player_id) DO UPDATE
                   SET battletag = COALESCE(EXCLUDED.battletag, player_profiles.battletag),
                       name = COALESCE(EXCLUDED.name, player_profiles.name),
                       html_compressed = EXCLUDED.html_compressed,
                       summary = EXCLUDED.summary,
                       last_updated_blizzard = EXCLUDED.last_updated_blizzard,
                       data_version = EXCLUDED.data_version,
                       updated_at = NOW()""",
                player_id,
                battletag,
                name,
                compressed,
                summary,
                last_updated_blizzard,
                data_version,
            )

    # ------------------------------------------------------------------ #
    # Hero stats snapshots
    # ------------------------------------------------------------------ #

    @track_storage_operation("hero_stats_snapshots", "set")
    async def store_hero_stats_snapshots(
        self, captured_at: int, rows: list[dict]
    ) -> None:
        """Store a batch of hero stats snapshot rows in a single transaction.

        Args:
            captured_at: Unix timestamp shared by every row.
            rows: Dicts with platform, gamemode, region, map, tier, hero,
                pickrate and winrate keys.
        """
        if not rows:
            return
        captured_at_dt = datetime.datetime.fromtimestamp(captured_at, tz=datetime.UTC)
        async with self._pool.acquire() as conn, conn.transaction():  # type: ignore[union-attr]
            await conn.executemany(
                """INSERT INTO hero_stats_snapshots
                   (captured_at, platform, gamemode, region, map, tier,
                    hero, pickrate, winrate, banrate)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
                [
                    (
                        captured_at_dt,
                        row["platform"],
                        row["gamemode"],
                        row["region"],
                        row["map"],
                        row["tier"],
                        row["hero"],
                        row["pickrate"],
                        row["winrate"],
                        row.get("banrate"),
                    )
                    for row in rows
                ],
            )

    @staticmethod
    def _build_snapshot_filters(
        platform: str,
        gamemode: str,
        region: str | None = None,
        map_: str | None = None,
        tier: str | None = None,
        heroes: list[str] | None = None,
        since: int | None = None,
        until: int | None = None,
    ) -> tuple[list[str], list[_QueryParam]]:
        """Build the WHERE conditions and bind parameters for snapshot queries.

        An empty ``heroes`` list means "no hero filter", same as ``None``.

        Returns:
            Tuple of (conditions, params); conditions use ``$n`` placeholders
            matching the position of each param.
        """
        params: list[_QueryParam] = [platform, gamemode]
        conditions = ["platform = $1", "gamemode = $2"]
        if region is not None:
            params.append(region)
            conditions.append(f"region = ${len(params)}")
        if map_ is not None:
            params.append(map_)
            conditions.append(f"map = ${len(params)}")
        if tier is not None:
            params.append(tier)
            conditions.append(f"tier = ${len(params)}")
        if heroes:
            params.append(heroes)
            conditions.append(f"hero = ANY(${len(params)}::text[])")
        if since is not None:
            params.append(since)
            conditions.append(f"captured_at >= TO_TIMESTAMP(${len(params)})")
        if until is not None:
            params.append(until)
            conditions.append(f"captured_at <= TO_TIMESTAMP(${len(params)})")
        return conditions, params

    @staticmethod
    def _clamp_history_limit(limit: int | None) -> int:
        """Clamp a caller-supplied row limit to ``1..MAX_HERO_STATS_HISTORY_ROWS``."""
        if limit is None:
            return MAX_HERO_STATS_HISTORY_ROWS
        return max(1, min(limit, MAX_HERO_STATS_HISTORY_ROWS))

    @track_storage_operation("hero_stats_snapshots", "get")
    async def get_hero_stats_history(
        self,
        platform: str,
        gamemode: str,
        region: str | None = None,
        map_: str | None = None,
        tier: str | None = None,
        heroes: list[str] | None = None,
        since: int | None = None,
        until: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Get hero stats history for a filter combination.

        ``platform`` and ``gamemode`` are required. ``region``, ``map_``,
        ``tier`` and ``heroes`` are optional filters. ``heroes`` accepts a list
        of hero keys and matches any of them; ``None`` and an empty list both
        mean "no hero filter". ``since``/``until`` bound ``captured_at``.

        ``limit`` is clamped to ``1..MAX_HERO_STATS_HISTORY_ROWS`` and pushed
        into the query as a ``LIMIT``, so the result set is always bounded even
        when only ``platform`` and ``gamemode`` are given.

        Returns list of dicts with 'captured_at' (int Unix ts), 'platform',
        'gamemode', 'region', 'map', 'tier', 'hero', 'pickrate', 'winrate',
        ordered by captured_at, map, tier then hero, all ascending.
        """
        conditions, params = self._build_snapshot_filters(
            platform=platform,
            gamemode=gamemode,
            region=region,
            map_=map_,
            tier=tier,
            heroes=heroes,
            since=since,
            until=until,
        )
        params.append(self._clamp_history_limit(limit))

        where_clause = " AND ".join(conditions)
        query = (
            "SELECT captured_at, platform, gamemode, region, map, tier, hero, "  # noqa: S608
            "pickrate, winrate, banrate FROM hero_stats_snapshots "
            f"WHERE {where_clause} "
            "ORDER BY captured_at ASC, map ASC, tier ASC, hero ASC "
            f"LIMIT ${len(params)}"
        )

        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            rows = await conn.fetch(query, *params)

        return [
            {
                "captured_at": int(row["captured_at"].timestamp()),
                "platform": row["platform"],
                "gamemode": row["gamemode"],
                "region": row["region"],
                "map": row["map"],
                "tier": row["tier"],
                "hero": row["hero"],
                "pickrate": row["pickrate"],
                "winrate": row["winrate"],
                "banrate": row["banrate"],
            }
            for row in rows
        ]

    @track_storage_operation("hero_stats_snapshots", "get")
    async def get_hero_stats_history_dates(
        self,
        platform: str,
        gamemode: str,
        region: str | None = None,
        map_: str | None = None,
        tier: str | None = None,
    ) -> list[int]:
        """List distinct snapshot timestamps matching the given filters.

        Returns list of int Unix timestamps, most recent first.
        """
        conditions, params = self._build_snapshot_filters(
            platform=platform,
            gamemode=gamemode,
            region=region,
            map_=map_,
            tier=tier,
        )

        where_clause = " AND ".join(conditions)
        query = (
            "SELECT DISTINCT captured_at FROM hero_stats_snapshots "  # noqa: S608
            f"WHERE {where_clause} "
            "ORDER BY captured_at DESC"
        )

        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            rows = await conn.fetch(query, *params)

        return [int(row["captured_at"].timestamp()) for row in rows]

    @track_storage_operation("hero_stats_snapshots", "delete")
    async def delete_old_hero_stats_snapshots(self, max_age_seconds: int) -> int:
        """Delete hero stats snapshots older than max_age_seconds.

        Returns:
            Number of deleted rows.
        """
        cutoff = time.time() - max_age_seconds
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            result = await conn.execute(
                "DELETE FROM hero_stats_snapshots WHERE captured_at < TO_TIMESTAMP($1)",
                cutoff,
            )
        deleted = int(result.split()[-1])
        logger.info(
            "Deleted {} old hero stats snapshots (max_age={}s)",
            deleted,
            max_age_seconds,
        )
        return deleted

    # ------------------------------------------------------------------ #
    # Maintenance
    # ------------------------------------------------------------------ #

    @track_storage_operation("player_profiles", "delete")
    async def delete_old_player_profiles(self, max_age_seconds: int) -> int:
        """Delete player profiles not updated within max_age_seconds.

        Returns:
            Number of deleted rows.
        """
        cutoff = time.time() - max_age_seconds
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            result = await conn.execute(
                "DELETE FROM player_profiles WHERE updated_at < TO_TIMESTAMP($1)",
                cutoff,
            )
        deleted = int(result.split()[-1])
        logger.info(
            "Deleted {} old player profiles (max_age={}s)", deleted, max_age_seconds
        )
        return deleted

    async def clear_all_data(self) -> None:
        """Truncate all tables (for testing)."""
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            await conn.execute(
                "TRUNCATE static_data, player_profiles, hero_stats_snapshots"
            )

    # ------------------------------------------------------------------ #
    # Statistics
    # ------------------------------------------------------------------ #

    async def get_stats(self) -> dict:
        """Return storage statistics for monitoring."""
        stats: dict = {
            "size_bytes": 0,
            "static_data_count": 0,
            "player_profiles_count": 0,
            "hero_stats_snapshots_count": 0,
            "player_profile_age_p50": 0,
            "player_profile_age_p90": 0,
            "player_profile_age_p99": 0,
        }
        try:
            async with self._pool.acquire() as conn:  # type: ignore[union-attr]
                row = await conn.fetchrow("SELECT COUNT(*) AS n FROM static_data")
                stats["static_data_count"] = row["n"]

                row = await conn.fetchrow("SELECT COUNT(*) AS n FROM player_profiles")
                stats["player_profiles_count"] = row["n"]

                row = await conn.fetchrow(
                    "SELECT COUNT(*) AS n FROM hero_stats_snapshots"
                )
                stats["hero_stats_snapshots_count"] = row["n"]

                # Approximate disk size via pg_total_relation_size
                row = await conn.fetchrow(
                    """SELECT COALESCE(SUM(pg_total_relation_size(t.name::regclass)), 0)
                              AS total
                       FROM unnest($1::text[]) AS t(name)""",
                    _SIZED_TABLES,
                )
                stats["size_bytes"] = row["total"] or 0

                # Profile age percentiles
                ages = await conn.fetch(
                    """SELECT EXTRACT(EPOCH FROM (NOW() - updated_at)) AS age
                       FROM player_profiles
                       ORDER BY updated_at DESC
                       LIMIT 1000"""
                )
                if ages:
                    age_list = sorted(float(r["age"]) for r in ages)
                    n = len(age_list)
                    stats["player_profile_age_p50"] = age_list[n // 2]
                    p90_index = min(int(n * 0.9), n - 1)
                    stats["player_profile_age_p90"] = age_list[p90_index]
                    p99_index = min(int(n * 0.99), n - 1)
                    stats["player_profile_age_p99"] = age_list[p99_index]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to collect storage stats: {}", exc)

        return stats
