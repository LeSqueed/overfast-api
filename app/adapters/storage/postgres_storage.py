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
_SIZED_TABLES = [
    "player_profiles",
    "static_data",
    "hero_stats_snapshots",
    "hero_stats_snapshot_runs",
]

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

    @staticmethod
    def _to_utc(timestamp: int) -> datetime.datetime:
        """Convert a Unix timestamp to a timezone-aware UTC datetime."""
        return datetime.datetime.fromtimestamp(timestamp, tz=datetime.UTC)

    @track_storage_operation("hero_stats_snapshots", "set")
    async def store_hero_stats_snapshots(
        self, captured_at: int, rows: list[dict]
    ) -> None:
        """Store a batch of hero stats snapshot rows in a single transaction.

        Re-storing a (platform, gamemode, region, map, tier, hero, captured_at)
        combination overwrites the previous values — a repeated or resumed run
        refreshes its own rows instead of duplicating the grid.

        Args:
            captured_at: Unix timestamp shared by every row.
            rows: Dicts with platform, gamemode, region, map, tier, hero,
                pickrate and winrate keys.
        """
        if not rows:
            return
        captured_at_dt = self._to_utc(captured_at)
        async with self._pool.acquire() as conn, conn.transaction():  # type: ignore[union-attr]
            await conn.executemany(
                """INSERT INTO hero_stats_snapshots
                   (captured_at, platform, gamemode, region, map, tier,
                    hero, pickrate, winrate, banrate)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                   ON CONFLICT (platform, gamemode, region, map, tier, hero,
                                captured_at)
                   DO UPDATE SET pickrate = EXCLUDED.pickrate,
                                 winrate = EXCLUDED.winrate,
                                 banrate = EXCLUDED.banrate""",
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

    @track_storage_operation("hero_stats_snapshot_runs", "set")
    async def claim_hero_stats_snapshot_run(
        self, captured_at: int, lease_seconds: int
    ) -> bool:
        """Atomically claim the snapshot run slot ``captured_at``.

        The claim succeeds when the slot is free, or when an unfinished run was
        started more than ``lease_seconds`` ago — that run's worker is gone and
        the snapshot may be resumed under the same timestamp. A single
        INSERT ... ON CONFLICT DO UPDATE ... RETURNING does the whole
        check-and-take, so two workers racing on the same slot cannot both win.

        Returns:
            True when the caller owns the run, False when it must stand down.
        """
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(
                """INSERT INTO hero_stats_snapshot_runs (captured_at, started_at)
                   VALUES ($1, NOW())
                   ON CONFLICT (captured_at) DO UPDATE
                       SET started_at = NOW()
                       WHERE hero_stats_snapshot_runs.completed_at IS NULL
                         AND hero_stats_snapshot_runs.started_at
                             < NOW() - MAKE_INTERVAL(secs => $2::float8)
                   RETURNING captured_at""",
                self._to_utc(captured_at),
                float(lease_seconds),
            )
        return row is not None

    @track_storage_operation("hero_stats_snapshot_runs", "set")
    async def complete_hero_stats_snapshot_run(
        self, captured_at: int, row_count: int, skipped_count: int
    ) -> None:
        """Mark the run slot ``captured_at`` as finished.

        Args:
            captured_at: Unix timestamp identifying the run.
            row_count: Number of snapshot rows the run stored.
            skipped_count: Number of grid combinations the run could not fetch.
        """
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            await conn.execute(
                """UPDATE hero_stats_snapshot_runs
                   SET completed_at = NOW(), row_count = $2, skipped_count = $3
                   WHERE captured_at = $1""",
                self._to_utc(captured_at),
                row_count,
                skipped_count,
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
    @track_storage_operation("hero_stats_snapshot_runs", "reap")
    async def reap_abandoned_hero_stats_snapshot_runs(
        self, lease_seconds: int
    ) -> list[int]:
        """Complete abandoned runs, stamping the row count they actually wrote."""
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            rows = await conn.fetch(
                """UPDATE hero_stats_snapshot_runs AS r
                   SET completed_at = NOW(),
                       row_count = (
                           SELECT COUNT(*)
                           FROM hero_stats_snapshots AS s
                           WHERE s.captured_at = r.captured_at
                       )
                   WHERE r.completed_at IS NULL
                     AND r.started_at < NOW() - MAKE_INTERVAL(secs => $1::bigint)
                   RETURNING r.captured_at""",
                lease_seconds,
            )

        reaped = [int(row["captured_at"].timestamp()) for row in rows]
        if reaped:
            logger.warning(
                "Reaped {} abandoned hero stats snapshot run(s): {}",
                len(reaped),
                reaped,
            )
        return reaped

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
        offset: int = 0,
    ) -> list[dict]:
        """Get hero stats history for a filter combination.

        ``platform`` and ``gamemode`` are required. ``region``, ``map_``,
        ``tier`` and ``heroes`` are optional filters. ``heroes`` accepts a list
        of hero keys and matches any of them; ``None`` and an empty list both
        mean "no hero filter". ``since``/``until`` bound ``captured_at``.

        Any optional dimension left unspecified is aggregated over rather than
        returned as one row per value: omitting ``region`` averages the rates
        across every region (returning ``region`` as "all"), and omitting
        ``map_`` does the same across maps. This keeps an "all filters" request
        at one row per (captured_at, tier, hero) instead of the full grid, which
        would otherwise exceed any row ceiling.

        ``limit`` is clamped to ``1..MAX_HERO_STATS_HISTORY_ROWS`` and pushed
        into the query as a ``LIMIT``, so the result set is always bounded even
        when only ``platform`` and ``gamemode`` are given. ``offset`` is clamped
        to ``>= 0`` and pushed down as an ``OFFSET``, so paging happens in the
        database rather than by over-fetching and discarding rows here.

        Returns list of dicts with 'captured_at' (int Unix ts), 'platform',
        'gamemode', 'region', 'map', 'tier', 'hero', 'pickrate', 'winrate'
        and 'banrate', ordered by captured_at, then region and map when they
        are filtered on, then tier then hero, all ascending. Aggregated rows
        report the omitted dimension as "all".
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
        limit_placeholder = len(params)
        params.append(max(0, offset))

        where_clause = " AND ".join(conditions)

        # Dimensions are either filtered on (kept as-is) or aggregated over
        # (collapsed to 'all' with AVG rates). tier is always a grouping column:
        # a filter narrows it, and omitting it returns one row per division
        # (including the pre-combined 'all' rows) just as the endpoint promises.
        select_parts = ["captured_at", "platform", "gamemode"]
        group_parts = ["captured_at", "platform", "gamemode"]
        order_parts = ["captured_at"]

        if region is None:
            select_parts.append("'all' AS region")
        else:
            select_parts.append("region")
            group_parts.append("region")
            order_parts.append("region")

        if map_ is None:
            select_parts.append("'all' AS map")
        else:
            select_parts.append("map")
            group_parts.append("map")
            order_parts.append("map")

        aggregating = region is None or map_ is None
        rate_parts = (
            [
                "AVG(pickrate) AS pickrate",
                "AVG(winrate) AS winrate",
                "AVG(banrate) AS banrate",
            ]
            if aggregating
            else ["pickrate", "winrate", "banrate"]
        )
        select_parts.extend(["tier", "hero", *rate_parts])
        group_parts.extend(["tier", "hero"])
        order_parts.extend(["tier", "hero"])

        query = (
            f"SELECT {', '.join(select_parts)} "  # noqa: S608
            "FROM hero_stats_snapshots "
            f"WHERE {where_clause} "
            f"GROUP BY {', '.join(group_parts)} "
            f"ORDER BY {', '.join(order_parts)} "
            f"LIMIT ${limit_placeholder} OFFSET ${len(params)}"
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

        Timestamps whose run is recorded as unfinished are excluded, so a
        partially written grid is never reported. Timestamps with no run
        recorded at all (snapshots taken before run tracking existed) are kept.

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
            "SELECT DISTINCT captured_at FROM hero_stats_snapshots s "  # noqa: S608
            f"WHERE {where_clause} "
            "AND NOT EXISTS (SELECT 1 FROM hero_stats_snapshot_runs r "
            "WHERE r.captured_at = s.captured_at AND r.completed_at IS NULL) "
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
                "TRUNCATE static_data, player_profiles, hero_stats_snapshots, "
                "hero_stats_snapshot_runs"
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
