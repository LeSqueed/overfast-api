"""Map domain service — maps list"""

from app.config import settings
from app.domain.parsers.maps import (
    COMPETITIVE_KEYS_STORAGE_KEY,
    competitive_keys_of,
    decode_competitive_keys,
    encode_competitive_keys,
    fetch_rates_html,
    parse_maps_html,
    unavailable_rates_html,
)
from app.domain.ports.storage import StaticDataCategory
from app.domain.services.static_data_service import StaticDataService, StaticFetchConfig
from app.infrastructure.logger import logger


class MapService(StaticDataService):
    """Domain service for maps data."""

    def _maps_config(
        self,
        cache_key: str,
        known_competitive: frozenset[str],
        gamemode: str | None = None,
    ) -> tuple[StaticFetchConfig, set[str]]:
        """Build a StaticFetchConfig for the maps list.

        The local CSV is the authoritative maps list. The Blizzard hero stats
        page (which hosts the map dropdown) is scraped only to enrich it: it
        flags which maps are in the competitive rotation and may surface a map
        the CSV doesn't know about yet. ``parse_maps_html`` degrades to
        ``known_competitive`` when the scrape is unusable, so a Blizzard markup
        change never fails a maps request nor demotes a known competitive map.
        The raw HTML is persisted so code changes to the parser take effect on
        the next request after restart.

        ``fetch_fallback`` extends that guarantee to the fetch itself: on a cold
        start with Blizzard unreachable there is no stored HTML to re-parse, and
        without it the request would fail even though the whole CSV list sits
        locally. It is set here and nowhere else — heroes, roles and gamemodes
        have no local source, so their fetch failures must stay fatal.

        Returns:
            The config, and the set the parser records the resolved competitive
            keys into — pass it to :meth:`_remember_competitive_keys` once the
            fetch is done so a promotion outlives this request.
        """
        observed: set[str] = set()

        async def _fetch() -> str:
            return await fetch_rates_html(self.blizzard_client)

        def _parse(raw: str) -> list[dict]:
            maps = parse_maps_html(raw, known_competitive)
            observed.update(competitive_keys_of(maps))
            return maps

        def _filter(data: list[dict]) -> list[dict]:
            if not gamemode:
                return data
            gamemode_val = gamemode.value if hasattr(gamemode, "value") else gamemode
            return [m for m in data if gamemode_val in m.get("gamemodes", [])]

        config = StaticFetchConfig(
            storage_key="maps:rates",
            fetcher=_fetch,
            fetch_fallback=unavailable_rates_html,
            parser=_parse,
            result_filter=_filter if gamemode else None,
            cache_key=cache_key,
            cache_ttl=settings.csv_cache_timeout,
            staleness_threshold=settings.maps_staleness_threshold,
            entity_type="maps",
        )
        return config, observed

    async def list_maps(
        self,
        gamemode: str | None,
        cache_key: str,
    ) -> tuple[list[dict], bool, int]:
        """Return the maps list (with optional gamemode filter).

        Stores the full (unfiltered) maps list in persistent storage.
        """
        known = await self._load_competitive_keys()
        config, observed = self._maps_config(cache_key, known or frozenset(), gamemode)
        result = await self.get_or_fetch(config)
        await self._remember_competitive_keys(known, observed)
        return result

    async def refresh_list(self) -> None:
        """Fetch fresh maps list, persist to storage and update API cache.

        Called by the background worker — bypasses the SWR layer.
        """
        known = await self._load_competitive_keys()
        config, observed = self._maps_config("/maps", known or frozenset())
        await self._fetch_and_store(config)
        await self._remember_competitive_keys(known, observed)

    async def _load_competitive_keys(self) -> frozenset[str] | None:
        """Load the map keys ever observed in the competitive rotation.

        Returns None — never an empty set — when storage can't be read, so the
        caller can tell "nothing remembered" apart from "we don't know what is
        remembered" and refrain from overwriting the set with a partial view.
        """
        try:
            stored = await self.storage.get_static_data(COMPETITIVE_KEYS_STORAGE_KEY)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[maps] Competitive map keys unreadable: {}", exc)
            return None

        return decode_competitive_keys(stored)

    async def _remember_competitive_keys(
        self, known: frozenset[str] | None, observed: set[str]
    ) -> None:
        """Union the observed competitive keys into the persisted set.

        The set only ever grows: the stored value is re-read and unioned rather
        than replaced, so a concurrent writer's promotions survive too. Nothing
        is written when the initial read failed (``known`` is None), since the
        keys observed under that blind spot are not a superset of what is
        stored. Storage problems are logged and swallowed — remembering a
        promotion must never fail a maps request.
        """
        if known is None or observed <= known:
            return

        try:
            stored = await self.storage.get_static_data(COMPETITIVE_KEYS_STORAGE_KEY)
            latest = decode_competitive_keys(stored)
            merged = latest | observed
            if merged != latest:
                await self.storage.set_static_data(
                    key=COMPETITIVE_KEYS_STORAGE_KEY,
                    data=encode_competitive_keys(merged),
                    category=StaticDataCategory.MAPS,
                )
                logger.info(
                    "[maps] Remembered {} newly competitive map(s): {}",
                    len(merged - latest),
                    sorted(merged - latest),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[maps] Could not persist competitive map keys: {}", exc)
