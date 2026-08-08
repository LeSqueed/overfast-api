"""Tests for the StoragePort contract — exercised via FakeStorage"""

import time

import pytest

from app.domain.ports.storage import StaticDataCategory


class TestStaticData:
    """Test static data storage operations"""

    @pytest.mark.asyncio
    async def test_set_and_get_static_data(self, storage_db):
        test_data = {"key": "hero-ana", "name": "Ana", "role": "support"}

        await storage_db.set_static_data(
            key="hero-ana",
            data=test_data,
            category=StaticDataCategory.HERO,
            data_version=1,
        )

        result = await storage_db.get_static_data("hero-ana")

        assert result is not None
        assert result["category"] == "hero"
        assert result["data_version"] == 1
        assert result["data"] == test_data
        assert result["updated_at"] > 0

    @pytest.mark.asyncio
    async def test_get_nonexistent_static_data(self, storage_db):
        result = await storage_db.get_static_data("nonexistent-key")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_static_data(self, storage_db):
        await storage_db.set_static_data(
            key="hero-mercy",
            data={"version": 1},
            category=StaticDataCategory.HERO,
            data_version=1,
        )
        first = await storage_db.get_static_data("hero-mercy")

        await storage_db.set_static_data(
            key="hero-mercy",
            data={"version": 2},
            category=StaticDataCategory.HERO,
            data_version=2,
        )
        updated = await storage_db.get_static_data("hero-mercy")

        assert updated["data"]["version"] == 2  # noqa: PLR2004
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
            key="map-ilios", data={"name": "Ilios"}, category=StaticDataCategory.MAPS
        )
        await storage_db.set_player_profile(
            player_id="Stats-1234", html="<html/>", summary={"name": "Stats"}
        )

        stats = await storage_db.get_stats()
        assert stats["static_data_count"] == 1
        assert stats["player_profiles_count"] == 1
        assert "size_bytes" in stats

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
            hero="ana",
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
            hero="ana",
        )

        assert [r["captured_at"] for r in result] == [1700000001, 1700000002]
        assert [r["pickrate"] for r in result] == [6.0, 7.0]

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
            hero="ana",
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
            hero="ana",
            since=1700000000,
            until=1700001000,
        )

        assert len(result) == 4  # noqa: PLR2004
        assert {r["map"] for r in result} == {"busan", "dorado"}
        assert all(r["platform"] == "pc" for r in result)

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
            hero="ana",
        )
        assert len(remaining) == 1
        assert remaining[0]["captured_at"] == now - 10


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
        test_data = {
            "name": "Lúcio",
            "emoji": "🎵🎶",
            "description": "Héros de soutien",
        }
        await storage_db.set_static_data(
            key="hero-lucio", data=test_data, category=StaticDataCategory.HERO
        )
        result = await storage_db.get_static_data("hero-lucio")
        assert result["data"]["name"] == "Lúcio"
        assert result["data"]["emoji"] == "🎵🎶"
