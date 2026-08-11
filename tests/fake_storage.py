"""In-memory FakeStorage implementing StoragePort — used in tests only."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from app.domain.ports.storage import MAX_HERO_STATS_HISTORY_ROWS

if TYPE_CHECKING:
    from app.domain.ports.storage import StaticDataCategory


class FakeStorage:
    """
    In-memory storage stub that satisfies ``StoragePort``.

    All data lives in plain dicts — no DB, no compression, no I/O.
    Provides the same interface as ``PostgresStorage`` so unit tests
    run without a real database.
    """

    def __init__(self) -> None:
        self._static: dict[str, dict] = {}
        self._profiles: dict[str, dict] = {}
        self._battletag_index: dict[str, str] = {}
        self._hero_stats_snapshots: list[dict] = []
        self._hero_stats_snapshot_runs: dict[int, dict] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    # ------------------------------------------------------------------ #
    # Static data
    # ------------------------------------------------------------------ #

    async def get_static_data(self, key: str) -> dict | None:
        entry = self._static.get(key)
        if entry is None:
            return None
        return {**entry, "data": entry["data"].decode("utf-8")}

    async def set_static_data(
        self,
        key: str,
        data: str,
        category: StaticDataCategory,
        data_version: int = 1,
    ) -> None:
        # Encoded on the way in and decoded on the way out, mirroring the
        # adapter's zstd(data.encode("utf-8")) round-trip: ``data`` is a raw
        # string, anything else fails here exactly as it fails against
        # PostgreSQL, and a read always returns a ``str``.
        now = int(time.time())
        existing = self._static.get(key)
        self._static[key] = {
            "data": data.encode("utf-8"),
            "category": str(category),
            "data_version": data_version,
            "updated_at": now,
            "created_at": existing["created_at"] if existing else now,
        }

    # ------------------------------------------------------------------ #
    # Player profiles
    # ------------------------------------------------------------------ #

    async def get_player_profile(self, player_id: str) -> dict | None:
        profile = self._profiles.get(player_id)
        if profile is None:
            return None
        summary = profile.get("summary") or {}
        if not summary:
            summary = {
                "url": player_id,
                "lastUpdated": profile.get("last_updated_blizzard"),
            }
        return {**profile, "summary": summary}

    async def get_player_id_by_battletag(self, battletag: str) -> str | None:
        return self._battletag_index.get(battletag)

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
        now = int(time.time())
        existing = self._profiles.get(player_id)
        self._profiles[player_id] = {
            "html": html,
            "summary": summary or {},
            "battletag": battletag or (existing["battletag"] if existing else None),
            "name": name or (existing["name"] if existing else None),
            "last_updated_blizzard": last_updated_blizzard,
            "updated_at": now,
            "created_at": existing["created_at"] if existing else now,
            "data_version": data_version,
        }
        if battletag:
            self._battletag_index[battletag] = player_id

    # ------------------------------------------------------------------ #
    # Hero stats snapshots
    # ------------------------------------------------------------------ #

    @staticmethod
    def _snapshot_key(row: dict) -> tuple:
        """Grid-cell identity of a snapshot row — the adapter's unique index."""
        return (
            row["platform"],
            row["gamemode"],
            row["region"],
            row["map"],
            row["tier"],
            row["hero"],
            row["captured_at"],
        )

    async def store_hero_stats_snapshots(
        self, captured_at: int, rows: list[dict]
    ) -> None:
        """Upsert rows, mirroring the adapter's ON CONFLICT DO UPDATE.

        ``banrate`` is normalised to ``None`` when absent, because the adapter
        writes ``banrate = EXCLUDED.banrate`` unconditionally: a row that stops
        reporting a banrate clears the stored one instead of keeping it.
        """
        by_key = {self._snapshot_key(row): row for row in self._hero_stats_snapshots}
        for row in rows:
            new_row = {**row, "captured_at": captured_at, "banrate": row.get("banrate")}
            existing = by_key.get(self._snapshot_key(new_row))
            if existing is None:
                self._hero_stats_snapshots.append(new_row)
                by_key[self._snapshot_key(new_row)] = new_row
            else:
                existing.update(new_row)

    async def claim_hero_stats_snapshot_run(
        self, captured_at: int, lease_seconds: int
    ) -> bool:
        run = self._hero_stats_snapshot_runs.get(captured_at)
        if run is not None:
            if (
                run["completed_at"] is not None
                or run["started_at"] > time.time() - lease_seconds
            ):
                return False
            # Taking over a stale lease only restarts the clock, mirroring the
            # adapter's ON CONFLICT DO UPDATE SET started_at = NOW(): the
            # counts of the run being resumed are left untouched.
            run["started_at"] = time.time()
            return True
        self._hero_stats_snapshot_runs[captured_at] = {
            "started_at": time.time(),
            "completed_at": None,
            "row_count": 0,
            "skipped_count": 0,
        }
        return True

    async def complete_hero_stats_snapshot_run(
        self, captured_at: int, row_count: int, skipped_count: int
    ) -> None:
        run = self._hero_stats_snapshot_runs.get(captured_at)
        if run is None:
            return
        run.update(
            completed_at=time.time(),
            row_count=row_count,
            skipped_count=skipped_count,
        )

    async def reap_abandoned_hero_stats_snapshot_runs(
        self, lease_seconds: int
    ) -> list[int]:
        cutoff = time.time() - lease_seconds
        reaped = []
        for captured_at, run in self._hero_stats_snapshot_runs.items():
            if run["completed_at"] is not None or run["started_at"] >= cutoff:
                continue
            run["completed_at"] = time.time()
            run["row_count"] = sum(
                1
                for row in self._hero_stats_snapshots
                if row["captured_at"] == captured_at
            )
            reaped.append(captured_at)
        return sorted(reaped)

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
        matching = [
            row
            for row in self._hero_stats_snapshots
            if row["platform"] == platform
            and row["gamemode"] == gamemode
            and (region is None or row["region"] == region)
            and (map_ is None or row["map"] == map_)
            and (tier is None or row["tier"] == tier)
            and (not heroes or row["hero"] in heroes)
            and (since is None or row["captured_at"] >= since)
            and (until is None or row["captured_at"] <= until)
        ]
        matching.sort(
            key=lambda row: (row["captured_at"], row["map"], row["tier"], row["hero"])
        )
        effective_limit = (
            MAX_HERO_STATS_HISTORY_ROWS
            if limit is None
            else max(1, min(limit, MAX_HERO_STATS_HISTORY_ROWS))
        )
        effective_offset = max(0, offset)
        return [
            {
                "captured_at": row["captured_at"],
                "platform": row["platform"],
                "gamemode": row["gamemode"],
                "region": row["region"],
                "map": row["map"],
                "tier": row["tier"],
                "hero": row["hero"],
                "pickrate": row["pickrate"],
                "winrate": row["winrate"],
                "banrate": row.get("banrate"),
            }
            for row in matching[effective_offset : effective_offset + effective_limit]
        ]

    async def get_hero_stats_history_dates(
        self,
        platform: str,
        gamemode: str,
        region: str | None = None,
        map_: str | None = None,
        tier: str | None = None,
    ) -> list[int]:
        dates = {
            row["captured_at"]
            for row in self._hero_stats_snapshots
            if row["platform"] == platform
            and row["gamemode"] == gamemode
            and (region is None or row["region"] == region)
            and (map_ is None or row["map"] == map_)
            and (tier is None or row["tier"] == tier)
        }
        return sorted(
            (date for date in dates if not self._run_unfinished(date)), reverse=True
        )

    def _run_unfinished(self, captured_at: int) -> bool:
        """Whether a run was claimed for ``captured_at`` but never completed."""
        run = self._hero_stats_snapshot_runs.get(captured_at)
        return run is not None and run["completed_at"] is None

    async def delete_old_hero_stats_snapshots(self, max_age_seconds: int) -> int:
        cutoff = time.time() - max_age_seconds
        to_delete = [
            row for row in self._hero_stats_snapshots if row["captured_at"] < cutoff
        ]
        for row in to_delete:
            self._hero_stats_snapshots.remove(row)
        return len(to_delete)

    # ------------------------------------------------------------------ #
    # Maintenance
    # ------------------------------------------------------------------ #

    async def delete_old_player_profiles(self, max_age_seconds: int) -> int:
        cutoff = time.time() - max_age_seconds
        to_delete = [
            pid for pid, p in self._profiles.items() if p["updated_at"] < cutoff
        ]
        for pid in to_delete:
            bt = self._profiles[pid].get("battletag")
            if bt:
                self._battletag_index.pop(bt, None)
            del self._profiles[pid]
        return len(to_delete)

    async def clear_all_data(self) -> None:
        self._static.clear()
        self._profiles.clear()
        self._battletag_index.clear()
        self._hero_stats_snapshots.clear()
        self._hero_stats_snapshot_runs.clear()

    # ------------------------------------------------------------------ #
    # Statistics
    # ------------------------------------------------------------------ #

    async def get_stats(self) -> dict:
        now = time.time()
        ages = sorted(now - p["updated_at"] for p in self._profiles.values())
        n = len(ages)
        return {
            "size_bytes": 0,
            "static_data_count": len(self._static),
            "player_profiles_count": n,
            "hero_stats_snapshots_count": len(self._hero_stats_snapshots),
            "player_profile_age_p50": ages[n // 2] if ages else 0,
            "player_profile_age_p90": ages[min(int(n * 0.9), n - 1)] if ages else 0,
            "player_profile_age_p99": ages[min(int(n * 0.99), n - 1)] if ages else 0,
        }
