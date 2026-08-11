import inspect
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.config import settings
from app.domain.ports.storage import StaticDataCategory
from app.domain.services import BaseService
from app.infrastructure.logger import logger
from app.monitoring.metrics import (
    background_refresh_triggered_total,
    stale_responses_total,
    storage_hits_total,
)

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class StaticFetchConfig:
    """Parameter object grouping all inputs needed for a static SWR fetch.

    Pass a single ``StaticFetchConfig`` to ``StaticDataService.get_or_fetch``
    instead of passing each field as a separate keyword argument.
    """

    storage_key: str
    fetcher: Callable[[], Any]
    cache_key: str
    cache_ttl: int
    staleness_threshold: int
    entity_type: str
    parser: Callable[[Any], Any] | None = field(default=None)
    result_filter: Callable[[Any], Any] | None = field(default=None)
    fetch_fallback: Callable[[], Any] | None = field(default=None)
    """Opt-in local stand-in for ``fetcher``, used only when the fetch fails.

    Left unset — the default — a failing fetch propagates and the request
    fails, which is the only correct outcome for an entity whose data exists
    solely on Blizzard's side (heroes, roles, gamemodes): there is nothing to
    serve instead. Only an entity backed by a complete local source may set
    this, so that a Blizzard outage on a cold start degrades to that source
    rather than failing the request.

    It returns a raw source in the same shape ``fetcher`` would, so the result
    still goes through ``parser``. A result produced this way is *degraded*: it
    is never written to persistent storage and is cached only briefly, so it is
    never mistaken for the authoritative value.
    """


class StaticDataService(BaseService):
    """SWR orchestration for static content backed by the ``static_data`` persistent storage table.

    Staleness is determined by a configurable time threshold.  Concrete static
    services (heroes, maps, gamemodes, roles) call ``get_or_fetch`` with a
    ``StaticFetchConfig`` — no subclass-level overrides are needed for the
    storage layer.

    Note: Valkey API-cache *reads* happen at the Nginx/Lua layer before FastAPI
    is reached; this service only ever *writes* to the API cache.
    """

    async def get_or_fetch(self, config: StaticFetchConfig) -> tuple[Any, bool, int]:
        """SWR orchestration for static data.

        Returns:
            ``(data, is_stale, age_seconds)`` tuple.  ``age_seconds`` is the
            number of seconds since the data was last stored in persistent storage (0 on
            a cold-start fetch).
        """
        stored = await self._load_from_storage(config.storage_key)
        if stored is not None:
            storage_hits_total.labels(result="hit").inc()
            return await self._serve_from_storage(stored, config)

        storage_hits_total.labels(result="miss").inc()
        return await self._cold_fetch(config)

    async def _load_from_storage(self, storage_key: str) -> dict[str, Any] | None:
        """Load raw source from the ``static_data`` table. Returns ``None`` on miss."""
        result = await self.storage.get_static_data(storage_key)
        return (
            {
                "raw": result["data"],
                "updated_at": result["updated_at"],
            }
            if result
            else None
        )

    async def _serve_from_storage(
        self, stored: dict[str, Any], config: StaticFetchConfig
    ) -> tuple[Any, bool, int]:
        """Serve data from a persistent storage hit, triggering a background refresh if stale.

        The stored ``raw`` value is always re-parsed with the current parser (for
        Blizzard HTML sources) or re-fetched from the local source (for CSV sources) so
        that code-only changes (e.g. new fields added to the parser) take effect
        immediately on restart without waiting for the staleness threshold.
        """
        data = await self._parse_stored(stored["raw"], config)
        age = int(time.time()) - stored["updated_at"]
        is_stale = age >= config.staleness_threshold
        filtered = self._apply_filter(data, config.result_filter)

        if is_stale:
            logger.info(
                "[SWR] {} stale (age={}s, threshold={}s) — serving + triggering refresh",
                config.entity_type,
                age,
                config.staleness_threshold,
            )
            await self._enqueue_refresh(
                config.entity_type,
                config.storage_key,
            )
            stale_responses_total.inc()
            background_refresh_triggered_total.labels(
                entity_type=config.entity_type
            ).inc()
            # Preserve the original stored_at so Age is computed correctly by nginx/Lua.
            # Use the full cache_ttl (not stale_cache_timeout) so X-Cache-TTL reflects the
            # real remaining lifetime of the entry, not just the short SWR window.
            await self._update_api_cache(
                config.cache_key,
                filtered,
                config.cache_ttl,
                stored_at=stored["updated_at"],
                staleness_threshold=config.staleness_threshold,
                stale_while_revalidate=settings.stale_cache_timeout,
            )
        else:
            logger.info(
                "[SWR] {} fresh (age={}s) — serving from persistent storage",
                config.entity_type,
                age,
            )
            # Preserve the original stored_at so Age is computed correctly by nginx/Lua.
            # Without this, every Valkey re-write resets stored_at to now, making Age ≈ 0.
            await self._update_api_cache(
                config.cache_key,
                filtered,
                config.cache_ttl,
                stored_at=stored["updated_at"],
                staleness_threshold=config.staleness_threshold,
            )

        return filtered, is_stale, age

    async def _parse_stored(self, raw: str, config: StaticFetchConfig) -> Any:
        """Produce structured data from ``raw`` stored source.

        - If ``config.parser`` is set: the stored ``raw`` is HTML (or a JSON-encoded
          multi-source dict); apply the parser directly.
        - If ``config.parser`` is not set: the source is a CSV file; re-call
          ``fetcher()`` to get always-current data (fast local I/O).
        """
        if config.parser is not None:
            return config.parser(raw)

        # CSV sources: re-read from file rather than using the stored JSON.
        if inspect.iscoroutinefunction(config.fetcher):
            return await config.fetcher()
        return config.fetcher()

    @staticmethod
    def _apply_filter(data: Any, result_filter: Callable[[Any], Any] | None) -> Any:
        """Apply ``result_filter`` to ``data`` if provided, otherwise return as-is."""
        return result_filter(data) if result_filter is not None else data

    async def _fetch_source(self, config: StaticFetchConfig) -> tuple[Any, bool]:
        """Fetch the raw source, falling back locally when the fetch fails.

        Returns:
            ``(raw, is_degraded)``. ``is_degraded`` is True when the fetch
            failed and ``config.fetch_fallback`` supplied a local stand-in.

        Raises:
            Exception: Whatever the fetcher raised, when no
                ``fetch_fallback`` is configured. Entities without a local
                source must still fail loudly rather than serve nothing.
        """
        try:
            if inspect.iscoroutinefunction(config.fetcher):
                return await config.fetcher(), False
            return config.fetcher(), False
        except Exception as exc:
            if config.fetch_fallback is None:
                raise
            # Deliberately broad: a fetch fails as an HTTP error, a timeout, a
            # rate-limit rejection or a raw socket error depending on how far it
            # got, and this entity has declared a local source good enough to
            # serve in all of those cases. The result is flagged degraded rather
            # than swallowed, so it never reaches persistent storage.
            logger.warning(
                "[SWR] {} fetch failed ({}) — falling back to the local source",
                config.entity_type,
                exc,
            )
            return config.fetch_fallback(), True

    async def _fetch_and_store(self, config: StaticFetchConfig) -> Any:
        """Fetch from source, persist raw source to persistent storage, update Valkey, return filtered data.

        A degraded fetch (see ``StaticFetchConfig.fetch_fallback``) skips the
        persistent storage write and is cached for ``stale_cache_timeout`` only,
        so the outage is never recorded as the authoritative value and the very
        next request past that window retries the real source.
        """
        raw, degraded = await self._fetch_source(config)

        data = config.parser(raw) if config.parser is not None else raw

        if not degraded:
            # Store the raw source so re-parses on storage hits always use current parser code.
            # For HTML sources (parser set): raw is the HTML string.
            # For CSV sources (no parser): raw is already the parsed data; serialise as JSON.
            raw_to_store = (
                raw
                if config.parser is not None
                else json.dumps(raw, separators=(",", ":"))
            )
            await self._store_in_storage(
                config.storage_key, raw_to_store, config.entity_type
            )

        filtered = self._apply_filter(data, config.result_filter)
        await self._update_api_cache(
            config.cache_key,
            filtered,
            settings.stale_cache_timeout if degraded else config.cache_ttl,
            staleness_threshold=config.staleness_threshold,
        )

        return filtered

    async def _cold_fetch(self, config: StaticFetchConfig) -> tuple[Any, bool, int]:
        """Fetch from source on cold start, persist to storage and Valkey."""
        logger.info(
            "[SWR] {} not in storage — fetching from source", config.entity_type
        )
        filtered = await self._fetch_and_store(config)
        return filtered, False, 0

    async def _store_in_storage(
        self, storage_key: str, raw: str, entity_type: str
    ) -> None:
        """Persist raw source string to the ``static_data`` table (zstd-compressed BYTEA)."""
        try:
            await self.storage.set_static_data(
                key=storage_key,
                data=raw,
                category=StaticDataCategory(entity_type),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SWR] Storage write failed for {}: {}", storage_key, exc)
