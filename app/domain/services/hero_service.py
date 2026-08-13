"""Hero domain service — heroes list, hero detail, hero stats"""

import json
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from app.config import settings
from app.domain.enums import (
    CompetitiveDivisionFilter,
    CompetitiveDivisionHistoryFilter,
    Locale,
    MapKey,
    PlayerGamemode,
    PlayerPlatform,
    PlayerRegion,
    SubRole,
)
from app.domain.exceptions import (
    InvalidGamemodeFilterError,
    ParserBlizzardError,
    ParserInternalError,
    ParserParsingError,
)
from app.domain.parsers.hero import fetch_hero_html, parse_hero_html
from app.domain.parsers.hero_stats_summary import parse_hero_stats_summary
from app.domain.parsers.heroes import (
    fetch_heroes_html,
    filter_heroes,
    parse_heroes_html,
)
from app.domain.parsers.heroes_hitpoints import parse_heroes_hitpoints
from app.domain.parsers.maps import (
    COMPETITIVE_KEYS_STORAGE_KEY,
    decode_competitive_keys,
    parse_trusted_rates_maps_html,
)
from app.domain.services.static_data_service import StaticDataService, StaticFetchConfig
from app.infrastructure.logger import logger

if TYPE_CHECKING:
    from app.domain.enums import (
        HeroGamemode,
        Role,
    )

# Cached verdicts of the "does Blizzard serve stats for this map key?" probe.
# Accepted keys are stable, so the verdict is kept for a long while; rejected
# ones are re-probed after a few snapshot runs, in case Blizzard enables the map
# shortly after listing it in the dropdown.
# Note these TTLs are upper bounds only: ValkeyCache.evict_volatile_data() drops
# every key but the unknown-player ones on shutdown, so a restart costs one
# re-probe per new map. That is cheap enough not to warrant an exemption.
MAP_KEY_PROBE_CACHE_PREFIX = "map-key-probe"
MAP_KEY_PROBE_ACCEPTED_TTL = 30 * 24 * 3600
MAP_KEY_PROBE_REJECTED_TTL = 7 * 24 * 3600

_MAP_KEY_ACCEPTED_VALUE = b"1"
_MAP_KEY_REJECTED_VALUE = b"0"

# Width of a snapshot slot, in seconds. Every run of the snapshot cron is
# rounded down to a slot boundary so that repeated, concurrent or resumed runs
# of the same scheduled tick share one ``captured_at`` and overwrite each
# other's rows instead of writing a second grid. One day matches the default
# daily cron (``hero_stats_snapshot_cron``); lower it if the cron is ever set to
# fire more than once a day.
HERO_STATS_SNAPSHOT_SLOT_SECONDS = 86400


def hero_stats_snapshot_slot(now: float | None = None) -> int:
    """Return the snapshot slot timestamp covering ``now`` (default: current time)."""
    reference = int(time.time() if now is None else now)
    return reference - reference % HERO_STATS_SNAPSHOT_SLOT_SECONDS


@dataclass(frozen=True)
class SnapshotRunResult:
    """Outcome of one hero stats snapshot run.

    Attributes:
        captured_at: Slot timestamp shared by every row of the run.
        rows_stored: Number of rows successfully persisted.
        rows_lost: Number of rows dropped because a storage flush failed.
        combinations_total: Number of grid combinations the run walked.
        combinations_failed: Number of combinations that could not be fetched.
    """

    captured_at: int
    rows_stored: int
    rows_lost: int
    combinations_total: int
    combinations_failed: int


class HeroService(StaticDataService):
    """Domain service for hero data: list, detail, and usage statistics."""

    # ------------------------------------------------------------------
    # Heroes list  (GET /heroes)
    # ------------------------------------------------------------------

    def _heroes_list_config(
        self,
        locale: Locale,
        cache_key: str,
        role: Role | SubRole | None = None,
        gamemode: HeroGamemode | None = None,
    ) -> StaticFetchConfig:
        """Build a StaticFetchConfig for the heroes list."""

        async def _fetch() -> str:
            return await fetch_heroes_html(self.blizzard_client, locale)

        def _parse(html: str) -> list[dict]:
            try:
                return parse_heroes_html(html)
            except ParserParsingError as exc:
                blizzard_url = (
                    f"{settings.blizzard_host}/{locale}{settings.heroes_path}"
                )
                raise ParserInternalError(blizzard_url, exc) from exc

        return StaticFetchConfig(
            storage_key=f"heroes:{locale}",
            fetcher=_fetch,
            parser=_parse,
            result_filter=(
                (lambda data: filter_heroes(data, role, gamemode))
                if (role or gamemode)
                else None
            ),
            cache_key=cache_key,
            cache_ttl=settings.heroes_path_cache_timeout,
            staleness_threshold=settings.heroes_staleness_threshold,
            entity_type="heroes",
        )

    async def list_heroes(
        self,
        locale: Locale,
        role: Role | SubRole | None,
        gamemode: HeroGamemode | None,
        cache_key: str,
    ) -> tuple[list[dict], bool, int]:
        """Return the heroes list (with optional role/gamemode filters).

        Stores raw Blizzard HTML per locale in persistent storage so that
        code changes to the parser take effect on the next request after restart.
        """
        return await self.get_or_fetch(
            self._heroes_list_config(locale, cache_key, role, gamemode)
        )

    async def refresh_list(self, locale: Locale) -> None:
        """Fetch fresh heroes list, persist to storage and update API cache.

        Called by the background worker — bypasses the SWR layer so that
        fresh data is always fetched from Blizzard regardless of stored age.
        """
        locale_str = locale.value
        cache_key = (
            f"/heroes?locale={locale_str}" if locale != Locale.ENGLISH_US else "/heroes"
        )
        await self._fetch_and_store(self._heroes_list_config(locale, cache_key))

    # ------------------------------------------------------------------
    # Single hero  (GET /heroes/{hero_key})
    # ------------------------------------------------------------------

    def _hero_detail_config(
        self, hero_key: str, locale: Locale, cache_key: str
    ) -> StaticFetchConfig:
        """Build a StaticFetchConfig for a single hero detail."""

        async def _fetch() -> str:
            hero_html = await fetch_hero_html(self.blizzard_client, hero_key, locale)
            # Validate hero exists before making the second Blizzard request.
            # parse_hero_html raises ParserBlizzardError (404) for unknown heroes,
            # which propagates to the API layer's registered OverfastError handler.
            parse_hero_html(hero_html, locale)
            heroes_html = await fetch_heroes_html(self.blizzard_client, locale)
            return json.dumps(
                {"hero_html": hero_html, "heroes_html": heroes_html},
                separators=(",", ":"),
            )

        def _parse(raw: str) -> dict:
            sources = json.loads(raw)
            try:
                hero_data = parse_hero_html(sources["hero_html"], locale)
                heroes_list = parse_heroes_html(sources["heroes_html"])
                heroes_hitpoints = parse_heroes_hitpoints()
                return _merge_hero_data(
                    hero_data, heroes_list, heroes_hitpoints, hero_key
                )
            except ParserParsingError as exc:
                blizzard_url = f"{settings.blizzard_host}/{locale}{settings.heroes_path}{hero_key}/"
                raise ParserInternalError(blizzard_url, exc) from exc

        return StaticFetchConfig(
            storage_key=f"hero:{hero_key}:{locale}",
            fetcher=_fetch,
            parser=_parse,
            cache_key=cache_key,
            cache_ttl=settings.hero_path_cache_timeout,
            staleness_threshold=settings.heroes_staleness_threshold,
            entity_type="hero",
        )

    async def get_hero(
        self,
        hero_key: str,
        locale: Locale,
        cache_key: str,
    ) -> tuple[dict, bool, int]:
        """Return full hero details merged with portrait and hitpoints.

        Stores a JSON-encoded dict of raw HTML sources per ``hero_key:locale``
        in persistent storage so that code changes to the parser take effect
        on the next request after restart.
        """
        return await self.get_or_fetch(
            self._hero_detail_config(hero_key, locale, cache_key)
        )

    async def refresh_single(self, hero_key: str, locale: Locale) -> None:
        """Fetch fresh hero detail, persist to storage and update API cache.

        Called by the background worker — bypasses the SWR layer.
        """
        locale_str = locale.value
        cache_key = (
            f"/heroes/{hero_key}?locale={locale_str}"
            if locale != Locale.ENGLISH_US
            else f"/heroes/{hero_key}"
        )
        await self._fetch_and_store(
            self._hero_detail_config(hero_key, locale, cache_key)
        )

    # ------------------------------------------------------------------
    # Hero stats summary  (GET /heroes/stats)
    # ------------------------------------------------------------------

    async def get_hero_stats(
        self,
        platform: PlayerPlatform,
        gamemode: PlayerGamemode,
        region: PlayerRegion,
        role: Role | None,
        map_filter: str | None,
        competitive_division: CompetitiveDivisionFilter | None,
        order_by: str,
        cache_key: str,
    ) -> tuple[list[dict], bool, int]:
        """Return hero usage statistics — Valkey-only cache, no persistent storage.

        Stats change frequently and have too many parameter combinations to
        store in persistent storage. The Valkey API cache (populated here, served by nginx)
        is sufficient.
        """

        for gamemode_filter in await self._get_hero_stats_gamemode_filters(gamemode):
            try:
                data = await self._get_hero_stats(
                    platform,
                    gamemode,
                    gamemode_filter,
                    region,
                    role,
                    map_filter,
                    competitive_division,
                    order_by,
                )
                working_filter = gamemode_filter
                break  # filter worked — stop retrying (data may legitimately be empty)
            except InvalidGamemodeFilterError as exc:
                # Blizzard may have changed the filter value; try the next candidate.
                gamemode_filter_exception = exc
        else:
            # All filter candidates exhausted without a successful call.
            blizzard_url = f"{settings.blizzard_host}{settings.hero_stats_path}"
            raise ParserInternalError(
                blizzard_url, gamemode_filter_exception
            ) from gamemode_filter_exception

        await self.cache.set_gamemode_filter(gamemode, working_filter)
        await self._update_api_cache(
            cache_key,
            data,
            settings.hero_stats_cache_timeout,
        )
        return data, False, 0

    # ------------------------------------------------------------------
    # Hero stats history  (per-map/per-tier snapshots)
    # ------------------------------------------------------------------

    async def snapshot_hero_stats(
        self, captured_at: int | None = None
    ) -> SnapshotRunResult:
        """Fetch hero pickrate/winrate for the full grid and store snapshots.

        Grid: every platform x gamemode x region x map x tier combination.
        Rows are flushed to storage incrementally (per map) so progress is
        persisted even if the run is interrupted, and all rows of a run share
        the same ``captured_at`` timestamp. Re-running the same slot rewrites
        those rows rather than appending a second grid, so an interrupted run
        can simply be run again.

        Args:
            captured_at: Slot timestamp to store the rows under. Defaults to
                the slot covering the current time.

        Returns:
            The run outcome, including how much of the grid was covered.
        """
        slot = hero_stats_snapshot_slot() if captured_at is None else captured_at
        total_stored = 0
        total_lost = 0
        combinations_total = 0
        combinations_failed = 0
        rows: list[dict] = []
        current_map_key: str | None = None
        map_keys = await self._competitive_map_keys()
        for (
            platform,
            gamemode,
            region,
            map_key,
            tier,
        ) in self._hero_stats_snapshot_grid(map_keys):
            combinations_total += 1
            if current_map_key is not None and map_key != current_map_key:
                stored = await self._flush_hero_stats_snapshot(slot, rows)
                total_stored += stored
                total_lost += len(rows) - stored
                rows = []
            current_map_key = map_key

            try:
                stats = await self._fetch_hero_stats_for_snapshot(
                    platform, gamemode, region, map_key, tier
                )
            except (
                InvalidGamemodeFilterError,
                ParserInternalError,
                ParserBlizzardError,
                ParserParsingError,
            ) as exc:
                combinations_failed += 1
                logger.warning(
                    "[hero-stats-snapshot] Skipping {}/{}/{}/{}/{}: {}",
                    platform,
                    gamemode,
                    region,
                    map_key,
                    tier,
                    exc,
                )
                continue
            except Exception as exc:  # noqa: BLE001
                # After the client's retries this combination still failed
                # (e.g. a lingering timeout); skip it rather than abort the
                # whole grid over a single combination.
                combinations_failed += 1
                logger.warning(
                    "[hero-stats-snapshot] Skipping {}/{}/{}/{}/{} after retries: {}",
                    platform,
                    gamemode,
                    region,
                    map_key,
                    tier,
                    exc,
                )
                continue
            for stat in stats:
                rows.extend(
                    [
                        {
                            "platform": platform.value,
                            "gamemode": gamemode.value,
                            "region": region.value,
                            "map": map_key,
                            "tier": tier,
                            "hero": stat["hero"],
                            "pickrate": stat["pickrate"],
                            "winrate": stat["winrate"],
                            "banrate": stat.get("banrate"),
                        }
                    ]
                )

        stored = await self._flush_hero_stats_snapshot(slot, rows)
        total_stored += stored
        total_lost += len(rows) - stored

        return SnapshotRunResult(
            captured_at=slot,
            rows_stored=total_stored,
            rows_lost=total_lost,
            combinations_total=combinations_total,
            combinations_failed=combinations_failed,
        )

    async def _flush_hero_stats_snapshot(
        self, captured_at: int, rows: list[dict]
    ) -> int:
        """Persist one snapshot chunk and log progress.

        A storage failure only costs this chunk: it is reported and 0 rows are
        returned, so the rest of the run — including the maps already collected
        — is kept rather than aborting the whole grid walk.

        Returns:
            Number of rows persisted (0 when the flush failed).
        """
        if not rows:
            return 0
        try:
            await self.storage.store_hero_stats_snapshots(captured_at, rows)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[hero-stats-snapshot] Failed to store {} rows for timestamp {}: {}",
                len(rows),
                captured_at,
                exc,
            )
            return 0
        logger.info(
            "[hero-stats-snapshot] Stored {} rows for timestamp {}",
            len(rows),
            captured_at,
        )
        return len(rows)

    async def get_hero_stats_history(
        self,
        platform: str,
        gamemode: str,
        region: str | None = None,
        map_key: str | None = None,
        tier: str | None = None,
        heroes: list[str] | None = None,
        since: int | None = None,
        until: int | None = None,
        limit: int | None = None,
        offset: int = 0,
        cache_key: str | None = None,
    ) -> list[dict]:
        """Return historical pickrate/winrate snapshots matching the filters.

        Paging is pushed down to storage, whose ordering is total, so a page is
        a plain ``limit``/``offset`` query: no row is fetched to be discarded,
        and every stored row is reachable however deep the page.

        Args:
            platform: Platform value (e.g. "pc").
            gamemode: Gamemode value (e.g. "competitive").
            region: Optional region value (e.g. "europe").
            map_key: Optional map key (e.g. "busan").
            tier: Optional competitive division (e.g. "gold") or "all".
            heroes: Optional list of hero keys (e.g. ["ana", "genji"]).
            since: Optional lower bound (Unix ts).
            until: Optional upper bound (Unix ts).
            limit: Optional page size. None means the storage ceiling.
            offset: Number of leading rows storage should skip.
            cache_key: Optional API cache key to populate with the result.

        Returns:
            List of dicts with captured_at, platform, gamemode, region, map,
            tier, hero, pickrate, winrate.
        """
        page = await self.storage.get_hero_stats_history(
            platform=platform,
            gamemode=gamemode,
            region=region,
            map_=map_key,
            tier=tier,
            heroes=heroes,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        rows = [self._to_public_history_row(row) for row in page]
        await self._cache_history_response(cache_key, rows)
        return rows

    async def get_hero_stats_history_dates(
        self,
        platform: str,
        gamemode: str,
        region: str | None = None,
        map_key: str | None = None,
        tier: str | None = None,
        cache_key: str | None = None,
    ) -> list[int]:
        """List distinct snapshot timestamps matching the filters.

        Args:
            platform: Platform value (e.g. "pc").
            gamemode: Gamemode value (e.g. "competitive").
            region: Optional region value (e.g. "europe").
            map_key: Optional map key (e.g. "busan").
            tier: Optional competitive division (e.g. "gold") or "all".
            cache_key: Optional API cache key to populate with the result.

        Returns:
            List of int Unix timestamps, most recent first.
        """
        dates = await self.storage.get_hero_stats_history_dates(
            platform=platform,
            gamemode=gamemode,
            region=region,
            map_=map_key,
            tier=tier,
        )
        await self._cache_history_response(cache_key, dates)
        return dates

    @staticmethod
    def _to_public_history_row(row: dict) -> dict:
        """Rename the storage column to the name the API contract publishes.

        Storage keys the competitive division as ``tier``; the response model
        publishes ``competitive_division``. The renaming has to happen here
        rather than in the model, because the cached payload nginx serves
        verbatim never passes through the model — so an alias would make the
        cached copy and the app-served copy disagree on the field name.
        """
        return {
            key if key != "tier" else "competitive_division": value
            for key, value in row.items()
        }

    async def _cache_history_response(
        self, cache_key: str | None, data: list[Any]
    ) -> None:
        """Populate the nginx-served API cache — never with an empty result.

        These endpoints advertise ``Cache-Control``/``X-Cache-Status``, so the
        key nginx reads (``api-cache:<request_uri>``) has to actually be
        written; otherwise every request is a full storage scan claiming to be
        a cache hit.

        nginx serves whatever it finds under that key verbatim, without
        consulting the app, so an empty result is deliberately *not* cached: a
        window that has no data yet must not keep answering "no data" for the
        whole TTL, including after the daily snapshot fills it in. That TTL is
        the multi-hour history timeout, not the hourly live-stats one, which
        makes swallowing an empty result correspondingly more important.
        """
        if not cache_key or not data:
            return
        await self._update_api_cache(
            cache_key, data, settings.hero_stats_history_cache_timeout
        )

    def _hero_stats_snapshot_grid(self, map_keys: list[str]) -> list[tuple]:
        """Build the full snapshot grid: platform x gamemode x region x map x tier.

        Only competitive gamemode is tracked per product decision. Tiers are
        every CompetitiveDivisionHistoryFilter value, which is exactly the
        domain the history endpoints accept as a `competitive_division` filter.

        Args:
            map_keys: Competitive map keys to snapshot (from the scraped maps
                list, or the CSV MapKey enum as a fallback).
        """
        tiers = [tier.value for tier in CompetitiveDivisionHistoryFilter]
        grid: list[tuple] = []
        for platform in PlayerPlatform:
            for region in PlayerRegion:
                for map_key in map_keys:
                    for tier in tiers:
                        grid.extend(
                            [
                                (
                                    platform,
                                    PlayerGamemode.COMPETITIVE,
                                    region,
                                    map_key,
                                    tier,
                                )
                            ]
                        )
        return grid

    async def _competitive_map_keys(self) -> list[str]:
        """Return the competitive map keys to snapshot.

        The CSV ``MapKey`` enum is the baseline. The scraped competitive map
        list stored by MapService (``maps:rates``) narrows it down to the maps
        actually in rotation, and may add a map the CSV doesn't know about yet —
        but only once Blizzard has confirmed it serves stats for that key (see
        :meth:`_blizzard_accepts_map_key`). Anything wrong with the stored
        scrape (absent, malformed, unparseable, or failing the known-map
        quorum) degrades to the full CSV list.

        The accumulated competitive keys (``maps:competitive``) are unioned in,
        so a map that was in rotation yesterday and is missing from today's
        dropdown is still snapshotted and its history stays continuous.
        """
        csv_keys = [map_key.value for map_key in MapKey]

        scraped_maps = await self._stored_competitive_maps()
        if scraped_maps is None:
            return csv_keys

        scraped_keys = [map_dict["key"] for map_dict in scraped_maps]
        remembered = await self._remembered_competitive_keys()
        candidates = [*scraped_keys, *sorted(remembered.difference(scraped_keys))]

        known_keys = set(csv_keys)
        return [
            key
            for key in candidates
            if key in known_keys or await self._blizzard_accepts_map_key(key)
        ]

    async def _remembered_competitive_keys(self) -> frozenset[str]:
        """Return the map keys ever observed in the competitive rotation.

        Degrades to the empty set when storage can't be read: the caller still
        has the scraped list to work from.
        """
        try:
            stored = await self.storage.get_static_data(COMPETITIVE_KEYS_STORAGE_KEY)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[hero-stats-snapshot] Competitive map keys unreadable: {}", exc
            )
            return frozenset()

        return decode_competitive_keys(stored)

    async def _stored_competitive_maps(self) -> list[dict] | None:
        """Return the trusted scraped competitive maps, or None to use the CSV."""
        try:
            stored = await self.storage.get_static_data("maps:rates")
        except Exception:  # noqa: BLE001
            stored = None

        if stored is None:
            return None

        if not isinstance(stored, dict) or not isinstance(stored.get("data"), str):
            logger.warning(
                "[hero-stats-snapshot] Unexpected cached maps value: {!r}",
                stored,
            )
            return None

        return parse_trusted_rates_maps_html(stored["data"])

    async def _blizzard_accepts_map_key(self, map_key: str) -> bool:
        """Check Blizzard actually serves hero stats for a scraped map key.

        A key that exists only in the scraped dropdown is unverified: if the
        stats endpoint doesn't accept it, every snapshot row captured for that
        map would be garbage. ``parse_hero_stats_json`` already raises
        ``ParserBlizzardError`` (HTTP 400) when Blizzard echoes back a
        ``selected`` map different from the requested one, so a single probe
        call reuses that existing signal instead of adding a parallel one.

        Only keys absent from the CSV get here, and the verdict is cached, so a
        new map costs one extra Blizzard call rather than one per snapshot run.
        Inconclusive failures (Blizzard unreachable, unexpected payload) are not
        cached: the map is skipped for this run and re-probed on the next one.
        """
        cache_key = f"{MAP_KEY_PROBE_CACHE_PREFIX}:{map_key}"
        cached_verdict = await self._cached_map_key_verdict(cache_key)
        if cached_verdict is not None:
            return cached_verdict

        try:
            await self._fetch_hero_stats_for_snapshot(
                PlayerPlatform.PC,
                PlayerGamemode.COMPETITIVE,
                PlayerRegion.EUROPE,
                map_key,
                "all",
            )
        except ParserBlizzardError as exc:
            if exc.status_code != HTTPStatus.BAD_REQUEST.value:
                logger.warning(
                    "[hero-stats-snapshot] Could not verify map key {}, skipping: {}",
                    map_key,
                    exc.message,
                )
                return False
            logger.warning(
                "[hero-stats-snapshot] Blizzard rejected scraped map key {}: {}",
                map_key,
                exc.message,
            )
            await self._store_map_key_verdict(cache_key, accepted=False)
            return False
        except (ParserInternalError, ParserParsingError) as exc:
            logger.warning(
                "[hero-stats-snapshot] Could not verify map key {}, skipping: {}",
                map_key,
                exc,
            )
            return False

        logger.info("[hero-stats-snapshot] Adopting new scraped map key {}", map_key)
        await self._store_map_key_verdict(cache_key, accepted=True)
        return True

    async def _cached_map_key_verdict(self, cache_key: str) -> bool | None:
        """Read a cached map key probe verdict, None when there isn't one."""
        try:
            cached = await self.cache.get(cache_key)
        except Exception:  # noqa: BLE001
            return None
        return None if cached is None else cached == _MAP_KEY_ACCEPTED_VALUE

    async def _store_map_key_verdict(self, cache_key: str, *, accepted: bool) -> None:
        """Cache a map key probe verdict so each new map is probed at most once."""
        try:
            await self.cache.set(
                cache_key,
                _MAP_KEY_ACCEPTED_VALUE if accepted else _MAP_KEY_REJECTED_VALUE,
                expire=(
                    MAP_KEY_PROBE_ACCEPTED_TTL
                    if accepted
                    else MAP_KEY_PROBE_REJECTED_TTL
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[hero-stats-snapshot] Could not cache map key verdict {}: {}",
                cache_key,
                exc,
            )

    async def _fetch_hero_stats_for_snapshot(
        self,
        platform: PlayerPlatform,
        gamemode: PlayerGamemode,
        region: PlayerRegion,
        map_key: str,
        tier: str,
    ) -> list[dict]:
        """Fetch and parse hero stats for one grid combination.

        Uses the Blizzard client throttle via parse_hero_stats_summary.
        """
        competitive_division = None if tier == "all" else tier
        for gamemode_filter in await self._get_hero_stats_gamemode_filters(gamemode):
            try:
                data = await parse_hero_stats_summary(
                    self.blizzard_client,
                    platform=platform,
                    gamemode=gamemode,
                    gamemode_filter=gamemode_filter,
                    region=region,
                    map_filter=map_key,
                    competitive_division=competitive_division,
                    order_by="hero:asc",
                )
                break  # filter worked — stop retrying (data may legitimately be empty)
            except InvalidGamemodeFilterError as exc:
                gamemode_filter_exception = exc
        else:
            # All filter candidates exhausted without a successful call.
            blizzard_url = f"{settings.blizzard_host}{settings.hero_stats_path}"
            raise ParserInternalError(
                blizzard_url, gamemode_filter_exception
            ) from gamemode_filter_exception
        return data

    async def _get_hero_stats_gamemode_filters(
        self, gamemode: PlayerGamemode
    ) -> list[str]:
        """Return the ordered candidate filter values to try for a given gamemode.

        The cached working filter (if any) is moved to the front so the correct
        value is tried first, avoiding a redundant Blizzard call on every request.

        Args:
            gamemode: Gamemode for validation

        Returns:
            Filter values ordered with the cached working filter first

        Raises:
            ParserParsingError: If gamemode is not supported
        """
        gamemode_mapping: dict[PlayerGamemode, list[str]] = {
            PlayerGamemode.QUICKPLAY: ["0"],
            PlayerGamemode.COMPETITIVE: ["1", "2"],
        }

        if gamemode not in gamemode_mapping:
            msg = f"{gamemode} is not a supported gamemode filter"
            raise ParserParsingError(msg)

        candidates = gamemode_mapping[gamemode]

        cached_filter = await self.cache.get_gamemode_filter(gamemode)
        if cached_filter and cached_filter in candidates:
            return [cached_filter] + [f for f in candidates if f != cached_filter]

        return candidates

    async def _get_hero_stats(
        self,
        platform: PlayerPlatform,
        gamemode: PlayerGamemode,
        gamemode_filter: str,
        region: PlayerRegion,
        role: Role | None,
        map_filter: str | None,
        competitive_division: CompetitiveDivisionFilter | None,
        order_by: str,
    ) -> list[dict]:
        try:
            data = await parse_hero_stats_summary(
                self.blizzard_client,
                platform=platform,
                gamemode=gamemode,
                gamemode_filter=gamemode_filter,
                region=region,
                role=role,
                map_filter=map_filter,
                competitive_division=competitive_division,
                order_by=order_by,
            )
        except ParserParsingError as exc:
            blizzard_url = f"{settings.blizzard_host}{settings.hero_stats_path}"
            raise ParserInternalError(blizzard_url, exc) from exc

        return data


# ---------------------------------------------------------------------------
# Module-level helpers (kept accessible for tests)
# ---------------------------------------------------------------------------


def _merge_hero_data(
    hero_data: dict,
    heroes_list: list[dict],
    heroes_hitpoints: dict,
    hero_key: str,
) -> dict:
    """Merge data from hero details, heroes list, and heroes hitpoints."""
    try:
        portrait_value = next(
            hero["portrait"] for hero in heroes_list if hero["key"] == hero_key
        )
    except StopIteration:
        portrait_value = None
    else:
        hero_data = dict_insert_value_before_key(
            hero_data, "role", "portrait", portrait_value
        )

    try:
        hitpoints = heroes_hitpoints[hero_key]["hitpoints"]
    except KeyError:
        hitpoints = None
    else:
        hero_data = dict_insert_value_before_key(
            hero_data, "abilities", "hitpoints", hitpoints
        )

    return hero_data


def dict_insert_value_before_key(
    data: dict,
    key: str,
    new_key: str,
    new_value: Any,
) -> dict:
    """Insert ``new_key: new_value`` before ``key`` in ``data``."""
    if key not in data:
        raise KeyError
    pos = list(data.keys()).index(key)
    items = list(data.items())
    items.insert(pos, (new_key, new_value))
    return dict(items)
