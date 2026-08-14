"""Blizzard HTTP client adapter implementing BlizzardClientPort"""

import asyncio
import time
from typing import TYPE_CHECKING

import httpx2
from fastapi import HTTPException, status

from app.adapters.blizzard.throttle import BlizzardThrottle
from app.config import settings
from app.domain.exceptions import RateLimitedError
from app.infrastructure.logger import logger
from app.infrastructure.metaclasses import Singleton
from app.monitoring.helpers import normalize_blizzard_url
from app.monitoring.metrics import (
    blizzard_request_duration_seconds,
    blizzard_requests_total,
)

if TYPE_CHECKING:
    from app.domain.ports import ThrottlePort

# Transient failures are retried with exponential backoff so one slow request
# does not abort a long-running job (e.g. the hero stats snapshot walk).
# Kept deliberately low: Blizzard fails some requests deterministically — its
# own gateway 504s on them after 60s — and no number of immediate retries wins
# those back, it only multiplies the wait. The snapshot walk gets its second
# chance from a retry pass at the end of the run instead, minutes later, which
# is where a genuinely transient failure actually recovers.
_BLIZZARD_MAX_ATTEMPTS = 3
_BLIZZARD_RETRY_BASE_DELAY = 2.0
_BLIZZARD_RETRY_MAX_DELAY = 60.0


class BlizzardClient(metaclass=Singleton):
    """
    HTTP client for Blizzard API/web requests with adaptive throttling.

    Implements BlizzardClientPort protocol via structural typing (duck typing).
    Protocol compliance is verified by type checkers at injection points.
    """

    def __init__(self):
        self.throttle: ThrottlePort | None = (
            BlizzardThrottle() if settings.throttle_enabled else None
        )
        self.client = httpx2.AsyncClient(
            headers={
                "User-Agent": (
                    f"OverFastAPI v{settings.app_version} - "
                    "https://github.com/TeKrop/overfast-api"
                ),
                "From": "valentin.porchet@proton.me",
            },
            http2=True,
            timeout=10,
            follow_redirects=True,
        )

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx2.Response:
        """Make an HTTP GET request, respecting the adaptive throttle.

        Any failure (timeout, connection error, non-2xx status) is retried up to
        ``_BLIZZARD_MAX_ATTEMPTS`` times with exponential backoff, so a single
        slow response doesn't kill a long-running job like the snapshot walk.
        """
        if self.throttle:
            await self._throttle_wait()

        kwargs: dict = {}
        if headers:
            kwargs["headers"] = headers
        if params:
            kwargs["params"] = params

        normalized_endpoint = normalize_blizzard_url(url)
        for attempt in range(1, _BLIZZARD_MAX_ATTEMPTS + 1):
            try:
                response = await self._execute_request(url, normalized_endpoint, kwargs)
            except Exception as exc:
                if attempt == _BLIZZARD_MAX_ATTEMPTS:
                    raise
                await asyncio.sleep(self._retry_delay(attempt))
                logger.warning(
                    "[BlizzardClient] Request failed (attempt {}/{}), retrying: {}",
                    attempt,
                    _BLIZZARD_MAX_ATTEMPTS,
                    exc,
                )
                continue

            if self.throttle:
                await self.throttle.adjust_delay(response.status_code)

            if response.status_code == status.HTTP_403_FORBIDDEN:
                raise await self._blizzard_rate_limited_error()

            if response.is_success or attempt == _BLIZZARD_MAX_ATTEMPTS:
                return response

            await asyncio.sleep(self._retry_delay(attempt))
            logger.warning(
                "[BlizzardClient] Blizzard returned HTTP {} (attempt {}/{}), retrying",
                response.status_code,
                attempt,
                _BLIZZARD_MAX_ATTEMPTS,
            )

        unreachable_error = "Unreachable"
        raise AssertionError(unreachable_error)

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        """Exponential backoff for the given attempt (1-based)."""
        return min(
            _BLIZZARD_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
            _BLIZZARD_RETRY_MAX_DELAY,
        )

    async def _throttle_wait(self) -> None:
        """Check throttle before request; raise 503 if in penalty period."""
        if not self.throttle:
            return

        try:
            await self.throttle.wait_before_request()
        except RateLimitedError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Blizzard is temporarily rate limiting this API. "
                    f"Please retry after {exc.retry_after} seconds."
                ),
                headers={settings.retry_after_header: str(exc.retry_after)},
            ) from exc

    async def _execute_request(
        self,
        url: str,
        normalized_endpoint: str,
        kwargs: dict,
    ) -> httpx2.Response:
        """Execute the HTTP GET and record metrics."""
        start_time = time.perf_counter()
        try:
            response = await self.client.get(url, **kwargs)
        except httpx2.TimeoutException as error:
            duration = time.perf_counter() - start_time
            self._record_metrics(normalized_endpoint, "timeout", duration)
            raise self._blizzard_response_error(
                status_code=0,
                error="Blizzard took more than 10 seconds to respond, resulting in a timeout",
            ) from error
        except httpx2.RemoteProtocolError as error:
            duration = time.perf_counter() - start_time
            self._record_metrics(normalized_endpoint, "error", duration)
            raise self._blizzard_response_error(
                status_code=0,
                error="Blizzard closed the connection, no data could be retrieved",
            ) from error

        duration = time.perf_counter() - start_time
        self._record_metrics(normalized_endpoint, str(response.status_code), duration)
        return response

    @staticmethod
    def _record_metrics(endpoint: str, status_label: str, duration: float) -> None:
        if settings.prometheus_enabled:
            blizzard_requests_total.labels(endpoint=endpoint, status=status_label).inc()
            blizzard_request_duration_seconds.labels(endpoint=endpoint).observe(
                duration
            )

    async def close(self) -> None:
        """Properly close HTTPX Async Client"""
        await self.client.aclose()

    # Legacy alias for backward compatibility
    async def aclose(self) -> None:
        """Alias for close() - deprecated, use close() instead"""
        await self.close()

    @staticmethod
    def _blizzard_response_error(status_code: int, error: str) -> HTTPException:
        """Retrieve a generic error response when a Blizzard page doesn't load"""
        logger.error(
            "Received an error from Blizzard. HTTP {} : {}",
            status_code,
            error,
        )
        return HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Couldn't get Blizzard page (HTTP {status_code} error) : {error}",
        )

    async def _blizzard_rate_limited_error(self) -> HTTPException:
        """Return 503 when Blizzard is rate limiting us (HTTP 403 received).

        Queries the throttle for the true remaining penalty time so that the
        Retry-After header and detail message reflect how long the caller must
        actually wait, rather than always advertising the full configured
        penalty duration.
        """
        if self.throttle:
            retry_after = await self.throttle.is_rate_limited()
        else:
            retry_after = settings.throttle_penalty_duration
        logger.warning(
            "[BlizzardClient] Rate limited by Blizzard (403) — returning 503 to client"
        )
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Blizzard is temporarily rate limiting this API. "
                f"Please retry after {retry_after} seconds."
            ),
            headers={settings.retry_after_header: str(retry_after)},
        )
