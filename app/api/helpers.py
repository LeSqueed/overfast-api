"""API Helpers module"""

from functools import cache
from typing import TYPE_CHECKING, Any

from fastapi import status

from app.api.models.errors import (
    BlizzardErrorMessage,
    BlizzardRateLimitErrorMessage,
    InternalServerErrorMessage,
    RateLimitErrorMessage,
)
from app.config import settings

if TYPE_CHECKING:
    from fastapi import Request, Response

# Typical routes responses to return
success_responses: dict[int | str, dict[str, Any]] = {
    status.HTTP_200_OK: {
        "description": "Successful Response",
        "headers": {
            settings.cache_ttl_header: {
                "description": "The TTL value for the cached response, in seconds",
                "schema": {
                    "type": "string",
                    "example": "600",
                },
            },
            "Age": {
                "description": (
                    "Number of seconds since the response payload was generated "
                    "(RFC 7234 §5.1). Present on FastAPI-served responses; "
                    "also set by nginx for Valkey cache hits."
                ),
                "schema": {
                    "type": "string",
                    "example": "42",
                },
            },
            "Cache-Control": {
                "description": (
                    "Standard caching directives (RFC 7234 + RFC 5861). "
                    "``max-age=0, must-revalidate`` forces any shared cache "
                    "(browser, proxy) to revalidate against the server before "
                    "every use, so it can never hold a response past the moment "
                    "the server's cache is invalidated (e.g. when a daily "
                    "snapshot publishes). The server's SWR envelope (Valkey, "
                    "served by nginx) still absorbs repeated requests."
                ),
                "schema": {
                    "type": "string",
                    "example": "public, max-age=0, must-revalidate",
                },
            },
            "X-Cache-Status": {
                "description": (
                    "Indicates whether the response was served from a fresh "
                    "cache entry (``hit``) or a stale one while a background "
                    "refresh is in-flight (``stale``)."
                ),
                "schema": {
                    "type": "string",
                    "enum": ["hit", "stale"],
                    "example": "hit",
                },
            },
        },
    },
}

routes_responses: dict[int | str, dict[str, Any]] = {
    **success_responses,
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": RateLimitErrorMessage,
        "description": "API Rate Limit Error",
        "headers": {
            settings.retry_after_header: {
                "description": "Indicates how long to wait before making a new request",
                "schema": {
                    "type": "string",
                    "example": "1",
                },
            }
        },
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": BlizzardRateLimitErrorMessage,
        "description": "Blizzard Rate Limit Error",
        "headers": {
            settings.retry_after_header: {
                "description": "Indicates how long to wait before making a new request",
                "schema": {
                    "type": "string",
                    "example": "5",
                },
            }
        },
    },
    status.HTTP_500_INTERNAL_SERVER_ERROR: {
        "model": InternalServerErrorMessage,
        "description": "Internal Server Error",
    },
    status.HTTP_504_GATEWAY_TIMEOUT: {
        "model": BlizzardErrorMessage,
        "description": "Blizzard Server Error",
    },
}


@cache
def get_human_readable_duration(duration: int) -> str:
    # Define the time units
    days, remainder = divmod(duration, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    # Build the human-readable string
    duration_parts = []
    if days > 0:
        duration_parts.append(f"{days} day{'s' if days > 1 else ''}")
    if hours > 0:
        duration_parts.append(f"{hours} hour{'s' if hours > 1 else ''}")
    if minutes > 0:
        duration_parts.append(f"{minutes} minute{'s' if minutes > 1 else ''}")

    if not duration_parts:
        return "less than a minute"

    return ", ".join(duration_parts)


def build_cache_key(request: Request) -> str:
    """Build a canonical cache key from the request URL path + query string.

    Uses the raw query string (``request.url.query``) to preserve the original
    percent-encoding, so the key matches what nginx stores in
    ``api-cache:<request_uri>`` exactly.
    """
    qs = request.url.query
    return f"{request.url.path}?{qs}" if qs else request.url.path


def apply_swr_headers(
    response: Response,
    cache_ttl: int,
    is_stale: bool,
    age_seconds: int = 0,
) -> None:
    """Add standard SWR and cache metadata headers to the response.

    Sets ``Cache-Control`` (RFC 5861), ``Age`` (RFC 7234), ``X-Cache-Status``,
    and the non-standard ``X-Cache-TTL`` on every FastAPI-served response.

    ``Cache-Control`` is ``public, max-age=0, must-revalidate``: a shared cache
    must revalidate against the server before every use, so it can never hold a
    response past the moment the server invalidates it (e.g. when a daily
    snapshot publishes). The server-side SWR envelope (Valkey, served by nginx)
    still absorbs repeated requests — the browser always asks the server, and the
    server answers from cache.

    ``cache_ttl`` remains the server-side TTL written into the Valkey envelope; it
    no longer leaks into the response header.
    """
    response.headers[settings.cache_ttl_header] = str(cache_ttl)
    response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
    if age_seconds > 0:
        response.headers["Age"] = str(age_seconds)
    if is_stale:
        response.headers["X-Cache-Status"] = "stale"
    else:
        response.headers["X-Cache-Status"] = "hit"
