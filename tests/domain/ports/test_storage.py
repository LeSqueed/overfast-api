"""Tests for the StoragePort contract.

Each test runs once per implementation of the port — see ``conftest.py`` for the
``storage_db`` parametrization — so the in-memory fake and the real PostgreSQL
adapter are held to exactly the same behaviour.
"""

import json
import time
from typing import Any, ClassVar

import pytest

from app.domain.ports.storage import StaticDataCategory


class TestStaticData:
    """Test static data storage operations"""

    @pytest.mark.asyncio
    async def test_set_and_get_static_data(self, storage_db):
        raw = json.dumps({"key": "hero-ana", "name": "Ana", "role": "support"})
        await storage_db.set_static_data(
            key="hero-ana",
            data=raw,
            category=StaticDataCategory.HERO,
            data_version=1,
        )

        result = await storage_db.get_static_data("hero-ana")

        assert result is not None
        assert result["data"] == raw
        assert result["category"] == "hero"
        assert result["data_version"] == 1
        assert result["updated_at"] > 0

    @pytest.mark.asyncio
    async def test_stored_data_is_read_back_as_a_string(self, storage_db):
        """``data`` is a raw string going in and coming back out."""
        await storage_db.set_static_data(
            key="hero-ana",
            data='{"name": "Ana"}',
            category=StaticDataCategory.HERO,
        )

        result = await storage_db.get_static_data("hero-ana")

        assert isinstance(result["data"], str)

    @pytest.mark.asyncio
    async def test_set_static_data_rejects_a_non_string_payload(self, storage_db):
        """Passing an object instead of a raw string fails, it is not stored."""
        payload: Any = {"name": "Ana"}

        with pytest.raises(AttributeError):
            await storage_db.set_static_data(
                key="hero-ana",
                data=payload,
                category=StaticDataCategory.HERO,
            )

    @pytest.mark.asyncio
    async def test_get_nonexistent_static_data(self, storage_db):
        result = await storage_db.get_static_data("nonexistent-key")

        assert result is None

    @pytest.mark.asyncio
    async def test_update_static_data(self, storage_db):
        await storage_db.set_static_data(
            key="hero-mercy",
            data='{"version": 1}',
            category=StaticDataCategory.HERO,
            data_version=1,
        )
        first = await storage_db.get_static_data("hero-mercy")

        await storage_db.set_static_data(
            key="hero-mercy",
            data='{"version": 2}',
            category=StaticDataCategory.HERO,
            data_version=2,
        )

        updated = await storage_db.get_static_data("hero-mercy")
        assert updated["data"] == '{"version": 2}'
        assert updated["data_version"] == 2  # noqa: PLR2004
        assert updated["updated_at"] >= first["updated_at"]


class TestPlayerProfiles:
    """Test player profile storage operations"""

    @pytest.mark.asyncio
    async def test_set_and_get_player_profile_with_summary(self, storage_db):
        player_id = "TeKrop-2217"
        html = "<html>Player profile data</html>"
        summary = {
            "name": "TeKrop",
            "isPublic": True,
            "lastUpdated": 1678536999,
            "url": "abc123",
        }
        await storage_db.set_player_profile(
            player_id=player_id, html=html, summary=summary
        )

        result = await storage_db.get_player_profile(player_id)

        assert result is not None
        assert result["html"] == html
        assert result["summary"] == summary
        assert result["updated_at"] > 0

    @pytest.mark.asyncio
    async def test_set_and_get_player_profile_without_summary(self, storage_db):
        player_id = "Player-1234"
        await storage_db.set_player_profile(
            player_id=player_id, html="<html/>", summary=None
        )

        result = await storage_db.get_player_profile(player_id)

        assert result is not None
        assert "url" in result["summary"]
        assert "lastUpdated" in result["summary"]
        assert result["last_updated_blizzard"] is None

    @pytest.mark.asyncio
    async def test_get_nonexistent_player_profile(self, storage_db):
        result = await storage_db.get_player_profile("NonExistent-9999")

        assert result is None

    @pytest.mark.asyncio
    async def test_update_player_profile(self, storage_db):
        player_id = "UpdateTest-1111"
        await storage_db.set_player_profile(
            player_id=player_id,
            html="<html>v1</html>",
            summary={"lastUpdated": 1000000},
        )
        first = await storage_db.get_player_profile(player_id)

        await storage_db.set_player_profile(
            player_id=player_id,
            html="<html>v2</html>",
            summary={"lastUpdated": 2000000},
        )

        updated = await storage_db.get_player_profile(player_id)
        assert updated["html"] == "<html>v2</html>"
        assert updated["summary"]["lastUpdated"] == 2000000  # noqa: PLR2004
        assert updated["updated_at"] >= first["updated_at"]

    @pytest.mark.asyncio
    async def test_get_player_id_by_battletag(self, storage_db):
        player_id = "Player-1234"
        battletag = "TestPlayer-5678"
        await storage_db.set_player_profile(
            player_id=player_id,
            html="<html/>",
            summary={"url": player_id, "lastUpdated": 123},
            battletag=battletag,
        )

        actual = await storage_db.get_player_id_by_battletag(battletag)

        assert actual == player_id

    @pytest.mark.asyncio
    async def test_get_player_id_by_battletag_not_found(self, storage_db):
        actual = await storage_db.get_player_id_by_battletag("Unknown-9999")

        assert actual is None


class TestStorageStats:
    """Test storage statistics"""

    @pytest.mark.asyncio
    async def test_get_stats_returns_counts(self, storage_db):
        await storage_db.set_static_data(
            key="map-ilios",
            data='{"name": "Ilios"}',
            category=StaticDataCategory.MAPS,
        )
        await storage_db.set_player_profile(
            player_id="Stats-1234", html="<html/>", summary={"name": "Stats"}
        )

        stats = await storage_db.get_stats()

        assert stats["static_data_count"] == 1
        assert stats["player_profiles_count"] == 1
        assert "size_bytes" in stats

    @pytest.mark.asyncio
    async def test_get_stats_counts_hero_stats_snapshots(self, storage_db):
        rows = [
            {
                "platform": "pc",
                "gamemode": "competitive",
                "region": "europe",
                "map": "busan",
                "tier": "all",
                "hero": hero,
                "pickrate": 5.0,
                "winrate": 50.0,
            }
            for hero in ("ana", "genji")
        ]
        await storage_db.store_hero_stats_snapshots(1700000001, rows)

        stats = await storage_db.get_stats()

        assert stats["hero_stats_snapshots_count"] == 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_get_stats_empty_database(self, storage_db):
        stats = await storage_db.get_stats()

        assert stats["static_data_count"] == 0
        assert stats["player_profiles_count"] == 0
        assert stats["size_bytes"] >= 0


class TestHeroStatsSnapshots:
    """Test hero stats snapshot storage operations"""

    @pytest.mark.asyncio
    async def test_store_and_query_history(self, storage_db):
        captured_at = 1700000000
        rows = [
            {
                "platform": "pc",
                "gamemode": "competitive",
                "region": "europe",
                "map": "busan",
                "tier": "gold",
                "hero": "ana",
                "pickrate": 5.5,
                "winrate": 52.3,
            },
            {
                "platform": "console",
                "gamemode": "competitive",
                "region": "europe",
                "map": "busan",
                "tier": "gold",
                "hero": "ana",
                "pickrate": 6.0,
                "winrate": 53.0,
            },
        ]
        await storage_db.store_hero_stats_snapshots(captured_at, rows)

        result = await storage_db.get_hero_stats_history(
            platform="pc",
            gamemode="competitive",
            region="europe",
            map_="busan",
            tier="gold",
            heroes=["ana"],
        )

        assert len(result) == 1
        assert result[0]["captured_at"] == captured_at
        assert result[0]["pickrate"] == 5.5  # noqa: PLR2004
        assert result[0]["winrate"] == 52.3  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_history_ordered_by_captured_at(self, storage_db):
        base = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "tier": "all",
            "hero": "ana",
            "pickrate": 5.0,
            "winrate": 50.0,
        }
        await storage_db.store_hero_stats_snapshots(
            1700000002, [{**base, "pickrate": 7.0}]
        )
        await storage_db.store_hero_stats_snapshots(
            1700000001, [{**base, "pickrate": 6.0}]
        )

        result = await storage_db.get_hero_stats_history(
            platform="pc",
            gamemode="competitive",
            region="europe",
            map_="busan",
            tier="all",
            heroes=["ana"],
        )

        assert [r["captured_at"] for r in result] == [1700000001, 1700000002]
        assert [r["pickrate"] for r in result] == [6.0, 7.0]

    @pytest.mark.asyncio
    async def test_history_filters_multiple_heroes(self, storage_db):
        base = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "tier": "all",
            "hero": "ana",
            "pickrate": 5.0,
            "winrate": 50.0,
        }
        await storage_db.store_hero_stats_snapshots(
            1700000001,
            [
                {**base},
                {**base, "hero": "genji"},
                {**base, "hero": "mercy"},
            ],
        )

        result = await storage_db.get_hero_stats_history(
            platform="pc",
            gamemode="competitive",
            region="europe",
            map_="busan",
            tier="all",
            heroes=["ana", "genji"],
        )

        assert {r["hero"] for r in result} == {"ana", "genji"}

    @pytest.mark.asyncio
    async def test_history_empty_heroes_list_returns_every_hero(self, storage_db):
        base = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "tier": "all",
            "hero": "ana",
            "pickrate": 5.0,
            "winrate": 50.0,
        }
        await storage_db.store_hero_stats_snapshots(
            1700000001,
            [{**base}, {**base, "hero": "genji"}, {**base, "hero": "mercy"}],
        )

        result = await storage_db.get_hero_stats_history(
            platform="pc",
            gamemode="competitive",
            heroes=[],
        )

        assert {r["hero"] for r in result} == {"ana", "genji", "mercy"}

    @pytest.mark.asyncio
    async def test_history_ordered_by_captured_at_map_tier_hero(self, storage_db):
        base = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "tier": "gold",
            "hero": "ana",
            "pickrate": 5.0,
            "winrate": 50.0,
        }
        await storage_db.store_hero_stats_snapshots(
            1700000002,
            [
                {**base, "map": "dorado", "tier": "gold", "hero": "ana"},
                {**base, "map": "busan", "tier": "silver", "hero": "ana"},
                {**base, "map": "busan", "tier": "gold", "hero": "zenyatta"},
                {**base, "map": "busan", "tier": "gold", "hero": "ana"},
            ],
        )
        await storage_db.store_hero_stats_snapshots(
            1700000001, [{**base, "map": "dorado", "tier": "silver", "hero": "mercy"}]
        )

        result = await storage_db.get_hero_stats_history(
            platform="pc",
            gamemode="competitive",
        )

        assert [(r["captured_at"], r["map"], r["tier"], r["hero"]) for r in result] == [
            (1700000001, "dorado", "silver", "mercy"),
            (1700000002, "busan", "gold", "ana"),
            (1700000002, "busan", "gold", "zenyatta"),
            (1700000002, "busan", "silver", "ana"),
            (1700000002, "dorado", "gold", "ana"),
        ]

    @pytest.mark.asyncio
    async def test_history_limit_caps_returned_rows(self, storage_db):
        base = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "tier": "all",
            "hero": "ana",
            "pickrate": 5.0,
            "winrate": 50.0,
        }
        for captured_at in (1700000001, 1700000002, 1700000003):
            await storage_db.store_hero_stats_snapshots(captured_at, [{**base}])

        result = await storage_db.get_hero_stats_history(
            platform="pc",
            gamemode="competitive",
            limit=2,
        )

        assert [r["captured_at"] for r in result] == [1700000001, 1700000002]

    @pytest.mark.asyncio
    async def test_history_offset_skips_leading_rows(self, storage_db):
        base = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "tier": "all",
            "hero": "ana",
            "pickrate": 5.0,
            "winrate": 50.0,
        }
        for captured_at in (1700000001, 1700000002, 1700000003):
            await storage_db.store_hero_stats_snapshots(captured_at, [{**base}])

        result = await storage_db.get_hero_stats_history(
            platform="pc",
            gamemode="competitive",
            offset=1,
        )

        assert [r["captured_at"] for r in result] == [1700000002, 1700000003]

    @pytest.mark.asyncio
    async def test_history_limit_and_offset_page_without_gaps_or_repeats(
        self, storage_db
    ):
        base = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "tier": "all",
            "hero": "ana",
            "pickrate": 5.0,
            "winrate": 50.0,
        }
        captured = [1700000001, 1700000002, 1700000003, 1700000004, 1700000005]
        for captured_at in captured:
            await storage_db.store_hero_stats_snapshots(captured_at, [{**base}])

        pages = [
            await storage_db.get_hero_stats_history(
                platform="pc",
                gamemode="competitive",
                limit=2,
                offset=offset,
            )
            for offset in (0, 2, 4, 6)
        ]

        assert [[r["captured_at"] for r in page] for page in pages] == [
            [1700000001, 1700000002],
            [1700000003, 1700000004],
            [1700000005],
            [],
        ]

    @pytest.mark.asyncio
    async def test_history_dates_returns_distinct_desc(self, storage_db):
        base = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "tier": "all",
            "hero": "ana",
            "pickrate": 5.0,
            "winrate": 50.0,
        }
        await storage_db.store_hero_stats_snapshots(1700000003, [{**base}])
        await storage_db.store_hero_stats_snapshots(1700000001, [{**base}])
        await storage_db.store_hero_stats_snapshots(1700000002, [{**base}])
        await storage_db.store_hero_stats_snapshots(
            1700000002, [{**base, "hero": "genji"}]
        )

        result = await storage_db.get_hero_stats_history_dates(
            platform="pc",
            gamemode="competitive",
            region="europe",
            map_="busan",
            tier="all",
        )

        assert result == [1700000003, 1700000002, 1700000001]

    @pytest.mark.asyncio
    async def test_history_dates_filtered_by_region(self, storage_db):
        base = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "tier": "all",
            "hero": "ana",
            "pickrate": 5.0,
            "winrate": 50.0,
        }
        await storage_db.store_hero_stats_snapshots(1700000001, [{**base}])
        await storage_db.store_hero_stats_snapshots(
            1700000002, [{**base, "region": "asia"}]
        )

        result = await storage_db.get_hero_stats_history_dates(
            platform="pc",
            gamemode="competitive",
            region="asia",
            map_="busan",
            tier="all",
        )

        assert result == [1700000002]

    @pytest.mark.asyncio
    async def test_history_since_until_filters(self, storage_db):
        base = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "tier": "gold",
            "hero": "ana",
            "pickrate": 5.0,
            "winrate": 50.0,
        }
        await storage_db.store_hero_stats_snapshots(
            1700000001, [{**base, "pickrate": 1.0}]
        )
        await storage_db.store_hero_stats_snapshots(
            1700000002, [{**base, "pickrate": 2.0}]
        )
        await storage_db.store_hero_stats_snapshots(
            1700000003, [{**base, "pickrate": 3.0}]
        )

        result = await storage_db.get_hero_stats_history(
            platform="pc",
            gamemode="competitive",
            region="europe",
            map_="busan",
            tier="gold",
            heroes=["ana"],
            since=1700000002,
            until=1700000003,
        )

        assert [r["captured_at"] for r in result] == [1700000002, 1700000003]

    @pytest.mark.asyncio
    async def test_all_maps_month_query(self, storage_db):
        busan = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "tier": "all",
            "hero": "ana",
            "pickrate": 5.0,
            "winrate": 50.0,
        }
        dorado = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "dorado",
            "tier": "all",
            "hero": "ana",
            "pickrate": 6.0,
            "winrate": 51.0,
        }
        await storage_db.store_hero_stats_snapshots(1700000001, [{**busan}, {**dorado}])
        await storage_db.store_hero_stats_snapshots(
            1700000061, [{**busan, "pickrate": 5.5}, {**dorado, "pickrate": 6.5}]
        )

        result = await storage_db.get_hero_stats_history(
            platform="pc",
            gamemode="competitive",
            region="europe",
            tier="all",
            heroes=["ana"],
            since=1700000000,
            until=1700001000,
        )

        assert len(result) == 4  # noqa: PLR2004
        assert {r["map"] for r in result} == {"busan", "dorado"}
        assert all(r["platform"] == "pc" for r in result)

    @pytest.mark.asyncio
    async def test_restoring_a_grid_cell_overwrites_it(self, storage_db):
        base = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "tier": "all",
            "hero": "ana",
            "pickrate": 5.0,
            "winrate": 50.0,
        }
        await storage_db.store_hero_stats_snapshots(1700000001, [{**base}])

        await storage_db.store_hero_stats_snapshots(
            1700000001, [{**base, "pickrate": 9.0, "winrate": 60.0, "banrate": 1.0}]
        )

        result = await storage_db.get_hero_stats_history(
            platform="pc", gamemode="competitive"
        )
        assert len(result) == 1
        assert result[0]["pickrate"] == 9.0  # noqa: PLR2004
        assert result[0]["winrate"] == 60.0  # noqa: PLR2004
        assert result[0]["banrate"] == 1.0

    @pytest.mark.asyncio
    async def test_restoring_a_grid_cell_clears_a_dropped_banrate(self, storage_db):
        """A cell whose banrate is no longer reported loses the stored one."""
        base = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "tier": "all",
            "hero": "ana",
            "pickrate": 5.0,
            "winrate": 50.0,
        }
        await storage_db.store_hero_stats_snapshots(
            1700000001, [{**base, "banrate": 9.9}]
        )

        await storage_db.store_hero_stats_snapshots(1700000001, [{**base}])

        result = await storage_db.get_hero_stats_history(
            platform="pc", gamemode="competitive"
        )
        assert [r["banrate"] for r in result] == [None]

    @pytest.mark.asyncio
    async def test_delete_old_snapshots(self, storage_db):
        now = int(time.time())
        base = {
            "platform": "pc",
            "gamemode": "competitive",
            "region": "europe",
            "map": "busan",
            "tier": "gold",
            "hero": "ana",
            "pickrate": 5.0,
            "winrate": 50.0,
        }
        await storage_db.store_hero_stats_snapshots(now - 100, [{**base}])
        await storage_db.store_hero_stats_snapshots(now - 10, [{**base}])

        deleted = await storage_db.delete_old_hero_stats_snapshots(60)

        assert deleted == 1
        remaining = await storage_db.get_hero_stats_history(
            platform="pc",
            gamemode="competitive",
            region="europe",
            map_="busan",
            tier="gold",
            heroes=["ana"],
        )
        assert len(remaining) == 1
        assert remaining[0]["captured_at"] == now - 10


class TestHeroStatsSnapshotRuns:
    """Test the snapshot run slot contract: claim, complete, and visibility"""

    SNAPSHOT_ROW: ClassVar[dict] = {
        "platform": "pc",
        "gamemode": "competitive",
        "region": "europe",
        "map": "busan",
        "tier": "all",
        "hero": "ana",
        "pickrate": 5.0,
        "winrate": 50.0,
    }

    @pytest.mark.asyncio
    async def test_free_slot_is_claimed(self, storage_db):
        claimed = await storage_db.claim_hero_stats_snapshot_run(1700006400, 21600)

        assert claimed is True

    @pytest.mark.asyncio
    async def test_second_claim_within_the_lease_stands_down(self, storage_db):
        await storage_db.claim_hero_stats_snapshot_run(1700006400, 21600)

        claimed = await storage_db.claim_hero_stats_snapshot_run(1700006400, 21600)

        assert claimed is False

    @pytest.mark.asyncio
    async def test_expired_lease_lets_a_later_worker_resume(self, storage_db):
        await storage_db.claim_hero_stats_snapshot_run(1700006400, 21600)

        claimed = await storage_db.claim_hero_stats_snapshot_run(1700006400, 0)

        assert claimed is True

    @pytest.mark.asyncio
    async def test_resuming_a_run_restarts_its_lease(self, storage_db):
        """The resumed slot is held again: a third worker must stand down."""
        await storage_db.claim_hero_stats_snapshot_run(1700006400, 21600)
        await storage_db.claim_hero_stats_snapshot_run(1700006400, 0)

        claimed = await storage_db.claim_hero_stats_snapshot_run(1700006400, 21600)

        assert claimed is False

    @pytest.mark.asyncio
    async def test_resumed_run_stays_hidden_until_it_completes(self, storage_db):
        """Taking over a stale lease resumes the run, it does not finish it."""
        await storage_db.store_hero_stats_snapshots(1700006400, [{**self.SNAPSHOT_ROW}])
        await storage_db.claim_hero_stats_snapshot_run(1700006400, 21600)

        await storage_db.claim_hero_stats_snapshot_run(1700006400, 0)

        result = await storage_db.get_hero_stats_history_dates(
            platform="pc", gamemode="competitive"
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_completing_a_resumed_run_publishes_its_dates(self, storage_db):
        await storage_db.store_hero_stats_snapshots(1700006400, [{**self.SNAPSHOT_ROW}])
        await storage_db.claim_hero_stats_snapshot_run(1700006400, 21600)
        await storage_db.claim_hero_stats_snapshot_run(1700006400, 0)

        await storage_db.complete_hero_stats_snapshot_run(1700006400, 1, 0)

        result = await storage_db.get_hero_stats_history_dates(
            platform="pc", gamemode="competitive"
        )
        assert result == [1700006400]

    @pytest.mark.asyncio
    async def test_completed_run_is_never_reclaimed(self, storage_db):
        await storage_db.claim_hero_stats_snapshot_run(1700006400, 21600)
        await storage_db.complete_hero_stats_snapshot_run(1700006400, 1500, 3)

        claimed = await storage_db.claim_hero_stats_snapshot_run(1700006400, 0)

        assert claimed is False

    @pytest.mark.asyncio
    async def test_unfinished_run_hides_its_dates(self, storage_db):
        await storage_db.store_hero_stats_snapshots(1700006400, [{**self.SNAPSHOT_ROW}])

        await storage_db.claim_hero_stats_snapshot_run(1700006400, 21600)

        result = await storage_db.get_hero_stats_history_dates(
            platform="pc", gamemode="competitive"
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_completing_a_run_publishes_its_dates(self, storage_db):
        await storage_db.store_hero_stats_snapshots(1700006400, [{**self.SNAPSHOT_ROW}])
        await storage_db.claim_hero_stats_snapshot_run(1700006400, 21600)

        await storage_db.complete_hero_stats_snapshot_run(1700006400, 1, 0)

        result = await storage_db.get_hero_stats_history_dates(
            platform="pc", gamemode="competitive"
        )
        assert result == [1700006400]

    @pytest.mark.asyncio
    async def test_dates_without_any_run_recorded_stay_visible(self, storage_db):
        """Snapshots written before run tracking existed are not hidden."""
        await storage_db.store_hero_stats_snapshots(1700006400, [{**self.SNAPSHOT_ROW}])

        result = await storage_db.get_hero_stats_history_dates(
            platform="pc", gamemode="competitive"
        )

        assert result == [1700006400]

    @pytest.mark.asyncio
    async def test_an_unfinished_run_hides_only_its_own_slot(self, storage_db):
        await storage_db.store_hero_stats_snapshots(1700006400, [{**self.SNAPSHOT_ROW}])
        await storage_db.store_hero_stats_snapshots(1700092800, [{**self.SNAPSHOT_ROW}])
        await storage_db.claim_hero_stats_snapshot_run(1700006400, 21600)
        await storage_db.claim_hero_stats_snapshot_run(1700092800, 21600)
        await storage_db.complete_hero_stats_snapshot_run(1700092800, 1, 0)

        result = await storage_db.get_hero_stats_history_dates(
            platform="pc", gamemode="competitive"
        )

        assert result == [1700092800]

    @pytest.mark.asyncio
    async def test_history_rows_of_an_unfinished_run_are_still_readable(
        self, storage_db
    ):
        """Only the date listing hides a partial grid — the rows themselves remain."""
        await storage_db.store_hero_stats_snapshots(1700006400, [{**self.SNAPSHOT_ROW}])
        await storage_db.claim_hero_stats_snapshot_run(1700006400, 21600)

        result = await storage_db.get_hero_stats_history(
            platform="pc", gamemode="competitive"
        )

        assert len(result) == 1


class TestDataIntegrity:
    """Test that data survives storage round-trips intact"""

    @pytest.mark.asyncio
    async def test_large_html_integrity(self, storage_db):
        large_html = "<html>" + ("x" * 10000) + "</html>"
        await storage_db.set_player_profile(
            player_id="LargeHTML-5555", html=large_html, summary={"name": "Large"}
        )

        result = await storage_db.get_player_profile("LargeHTML-5555")

        assert result["html"] == large_html

    @pytest.mark.asyncio
    async def test_unicode_data_integrity(self, storage_db):
        raw = json.dumps(
            {
                "name": "Lúcio",
                "emoji": "🎵🎶",
                "description": "Héros de soutien",
            },
            ensure_ascii=False,
        )
        await storage_db.set_static_data(
            key="hero-lucio", data=raw, category=StaticDataCategory.HERO
        )

        result = await storage_db.get_static_data("hero-lucio")

        assert result["data"] == raw
        assert json.loads(result["data"])["name"] == "Lúcio"
