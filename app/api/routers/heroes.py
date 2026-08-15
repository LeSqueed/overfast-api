"""Heroes endpoints router : heroes list, heroes details, etc."""

import time
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, status

from app.api.dependencies import HeroServiceDep
from app.api.enums import RouteTag
from app.api.helpers import (
    apply_swr_headers,
    build_cache_key,
    get_human_readable_duration,
    routes_responses,
)
from app.api.models.heroes import (
    BadRequestErrorMessage,
    Hero,
    HeroParserErrorMessage,
    HeroShort,
    HeroStatsHistoryPoint,
    HeroStatsSummary,
)
from app.config import settings
from app.domain.enums import (
    CompetitiveDivisionFilter,
    CompetitiveDivisionHistoryFilter,
    HeroGamemode,
    Locale,
    PlayerGamemode,
    PlayerPlatform,
    PlayerRegion,
    Role,
    SubRole,
)
from app.domain.ports.storage import MAX_HERO_STATS_HISTORY_ROWS


def _apply_history_cache_headers(response: Response, data: list) -> None:
    """Advertise the cache only when the response was actually cached.

    An empty series is deliberately not written to the API cache, so promising
    a multi-hour ``max-age`` for it would let any proxy in front of nginx keep
    answering "no data" across the snapshot run that fills it — the same trap
    the storage-side guard avoids, one layer further out.
    """
    if not data:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Cache-Status"] = "miss"
        return

    apply_swr_headers(response, settings.hero_stats_history_cache_timeout, False, 0)


router = APIRouter()

# Shape of a map key, shared by every endpoint taking a "map" filter. Map keys
# are lowercase-hyphenated by construction, so obvious garbage can be rejected
# locally instead of burning shared Blizzard throttle budget on a request that
# can only fail. Deliberately a shape check and not the MapKey enum: a map
# released after our last CSV update must still reach Blizzard.
MAP_KEY_PATTERN = r"^[a-z0-9-]{1,50}$"

# ``/heroes/stats/history`` public paging and windowing contract.
#
# Snapshots cover the whole platform x region x map x division x hero grid once
# a day, so a request carrying only the two mandatory filters still matches
# hundreds of thousands of rows. A one-week default window plus a 1000-row
# default page keeps such a request cheap, while `since`/`until`/`offset` leave
# every stored row reachable.
DEFAULT_HISTORY_WINDOW = 7 * 24 * 3600
DEFAULT_HISTORY_LIMIT = 1000

# ~2x the current roster, so every hero can be listed explicitly with room for
# future releases, while a repeated `heroes` parameter can't be used to build an
# arbitrarily large bind parameter.
MAX_HISTORY_HEROES = 100

# Deepest row a single page may start at. Storage pages natively, so every
# stored row is reachable and this is purely a cost bound: PostgreSQL still
# walks and discards every skipped row, so an unbounded `offset` would let one
# request scan the whole table. A query already fixes platform and gamemode,
# leaving roughly 25k rows per day across regions x maps x divisions x heroes,
# so a million rows is well over a month of paging before narrowing the window
# with `since`/`until` becomes the cheaper way to reach further back.
MAX_HISTORY_OFFSET = 1_000_000

# 2100-01-01T00:00:00Z. Beyond any plausible snapshot, and small enough that
# PostgreSQL's TO_TIMESTAMP() never overflows on the way to the query.
MAX_HISTORY_TIMESTAMP = 4_102_444_800

# The history and dates endpoints read persistent storage only — they never
# call Blizzard, so the Blizzard-specific responses can't happen for them.
storage_routes_responses: dict[int | str, dict[str, Any]] = {
    status_code: response
    for status_code, response in routes_responses.items()
    if status_code
    not in (status.HTTP_503_SERVICE_UNAVAILABLE, status.HTTP_504_GATEWAY_TIMEOUT)
}


def _resolve_history_since(since: int | None, until: int | None) -> int:
    """Return the effective ``since`` bound, defaulting to a bounded window.

    When the caller supplies no lower bound, the default window ends at
    ``until`` (or now) so that asking only for an upper bound still returns the
    week *before* it rather than an empty range.

    Raises:
        HTTPException: 400 if the requested window is inverted.
    """
    if since is None:
        window_end = until if until is not None else int(time.time())
        return max(0, window_end - DEFAULT_HISTORY_WINDOW)

    if until is not None and since > until:
        msg = (
            f"'since' ({since}) must not be greater than 'until' ({until}): "
            "the requested time window is empty."
        )
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg)

    return since


@router.get(
    "",
    responses=routes_responses,
    tags=[RouteTag.HEROES],
    summary="Get a list of heroes",
    description=(
        "Get a list of Overwatch heroes, which can be filtered using roles, subroles or gamemodes. "
        f"<br />**Cache TTL : {get_human_readable_duration(settings.heroes_path_cache_timeout)}.**"
    ),
    operation_id="list_heroes",
    response_model=list[HeroShort],
)
async def list_heroes(
    request: Request,
    response: Response,
    service: HeroServiceDep,
    role: Annotated[Role | SubRole | None, Query(title="Role filter")] = None,
    locale: Annotated[
        Locale, Query(title="Locale to be displayed")
    ] = Locale.ENGLISH_US,
    gamemode: Annotated[HeroGamemode | None, Query(title="Gamemode filter")] = None,
) -> Any:
    data, is_stale, age = await service.list_heroes(
        locale=locale, role=role, gamemode=gamemode, cache_key=build_cache_key(request)
    )
    apply_swr_headers(response, settings.heroes_path_cache_timeout, is_stale, age)
    return data


@router.get(
    "/stats",
    responses={
        **routes_responses,
        status.HTTP_400_BAD_REQUEST: {
            "model": BadRequestErrorMessage,
            "description": "Bad Request Error",
        },
    },
    tags=[RouteTag.HEROES],
    summary="Get hero stats",
    description=(
        "Get hero statistics usage, filtered by platform, region, role, etc."
        "Only Role Queue gamemodes are concerned."
        f"<br />**Cache TTL : {get_human_readable_duration(settings.hero_stats_cache_timeout)}.**"
    ),
    operation_id="get_hero_stats",
    response_model=list[HeroStatsSummary],
)
async def get_hero_stats(
    request: Request,
    response: Response,
    service: HeroServiceDep,
    platform: Annotated[
        PlayerPlatform, Query(title="Player platform filter", examples=["pc"])
    ],
    gamemode: Annotated[
        PlayerGamemode,
        Query(
            title="Gamemode",
            description="Filter on a specific gamemode.",
            examples=["competitive"],
        ),
    ],
    region: Annotated[
        PlayerRegion,
        Query(
            title="Region",
            description="Filter on a specific player region.",
            examples=["europe"],
        ),
    ],
    role: Annotated[
        Role | None, Query(title="Role filter", examples=["support"])
    ] = None,
    map_: Annotated[
        str | None,
        Query(
            alias="map",
            title="Map key filter",
            pattern=MAP_KEY_PATTERN,
            examples=["hanaoka"],
        ),
    ] = None,
    competitive_division: Annotated[
        CompetitiveDivisionFilter | None,
        Query(
            title="Competitive division filter",
            examples=["diamond"],
        ),
    ] = None,
    order_by: Annotated[
        str,
        Query(
            title="Ordering field and the way it's arranged (asc[ending]/desc[ending])",
            pattern=r"^(hero|winrate|pickrate):(asc|desc)$",
        ),
    ] = "hero:asc",
) -> Any:
    data, is_stale, age = await service.get_hero_stats(
        platform=platform,
        gamemode=gamemode,
        region=region,
        role=role,
        map_filter=map_,
        competitive_division=competitive_division,
        order_by=order_by,
        cache_key=build_cache_key(request),
    )
    apply_swr_headers(
        response,
        settings.hero_stats_cache_timeout,
        is_stale,
        age,
    )
    return data


@router.get(
    "/stats/history",
    responses={
        **storage_routes_responses,
        status.HTTP_400_BAD_REQUEST: {
            "model": BadRequestErrorMessage,
            "description": "Bad Request Error",
        },
    },
    tags=[RouteTag.HEROES],
    summary="Get hero stats history",
    description=(
        "Get historical hero pickrate/winrate snapshots."
        "<br />`platform` and `gamemode` are required; `region`, `map`, "
        "`competitive_division` and `heroes` are optional filters, and "
        "`since`/`until` bound the snapshot timestamps."
        "<br />`heroes` accepts one or more hero keys (repeated query "
        f"parameter, up to {MAX_HISTORY_HEROES}), returning only matching heroes."
        "<br />Each point carries the full context (platform, gamemode, "
        "region, map, competitive_division, hero, captured_at) so clients can "
        "group or filter the series themselves."
        "<br />`captured_at` is a Unix timestamp in seconds, the same "
        "representation as `since`/`until` and `/heroes/stats/dates`."
        "<br />Note: each `captured_at` is the time *we* recorded the "
        "reading; Blizzard does not expose the window it aggregates over."
        "<br />**Results are paginated and time-bounded.** Without `limit` at "
        f"most {DEFAULT_HISTORY_LIMIT} points are returned, and without "
        f"`since` only the last {DEFAULT_HISTORY_WINDOW // 86400} days (ending "
        "at `until`, when given) are considered. Page through older or larger "
        "ranges with `offset` and an explicit `since`."
        f"<br />**Cache TTL : {get_human_readable_duration(settings.hero_stats_history_cache_timeout)}.**"
    ),
    operation_id="get_hero_stats_history",
    response_model=list[HeroStatsHistoryPoint],
)
async def get_hero_stats_history(
    request: Request,
    response: Response,
    service: HeroServiceDep,
    platform: Annotated[
        PlayerPlatform, Query(title="Player platform filter", examples=["pc"])
    ],
    gamemode: Annotated[
        PlayerGamemode,
        Query(
            title="Gamemode",
            description="Filter on a specific gamemode.",
            examples=["competitive"],
        ),
    ],
    region: Annotated[
        PlayerRegion | None,
        Query(
            title="Region",
            description="Optional filter on a specific player region.",
            examples=["europe"],
        ),
    ] = None,
    map_: Annotated[
        str | None,
        Query(
            alias="map",
            title="Map key filter",
            pattern=MAP_KEY_PATTERN,
            examples=["busan"],
        ),
    ] = None,
    competitive_division: Annotated[
        CompetitiveDivisionHistoryFilter | None,
        Query(
            title="Competitive division filter",
            description=(
                "Optional competitive division, or 'all' for the combined "
                "snapshot across every division. Omitting it returns every "
                "division *and* the combined snapshot."
            ),
            examples=["gold"],
        ),
    ] = None,
    heroes: Annotated[
        list[str] | None,
        Query(
            title="Hero key filter",
            description="One or more hero keys (repeated query parameter).",
            max_length=MAX_HISTORY_HEROES,
            examples=[["ana", "genji"]],
        ),
    ] = None,
    since: Annotated[
        int | None,
        Query(
            title="Lower bound (Unix timestamp)",
            description=(
                "Oldest snapshot timestamp to consider. Defaults to "
                f"{DEFAULT_HISTORY_WINDOW // 86400} days before `until` "
                "(or before now)."
            ),
            ge=0,
            le=MAX_HISTORY_TIMESTAMP,
            examples=[1700000000],
        ),
    ] = None,
    until: Annotated[
        int | None,
        Query(
            title="Upper bound (Unix timestamp)",
            description="Most recent snapshot timestamp to consider.",
            ge=0,
            le=MAX_HISTORY_TIMESTAMP,
            examples=[1700003600],
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            title="Maximum number of points to return",
            ge=1,
            le=MAX_HERO_STATS_HISTORY_ROWS,
            examples=[DEFAULT_HISTORY_LIMIT],
        ),
    ] = DEFAULT_HISTORY_LIMIT,
    offset: Annotated[
        int,
        Query(
            title="Number of points to skip",
            description=(
                "Offset into the ordered result set. Paging is done by storage, "
                "so any page up to this bound returns real data when it exists."
            ),
            ge=0,
            le=MAX_HISTORY_OFFSET,
            examples=[0],
        ),
    ] = 0,
) -> Any:
    effective_since = _resolve_history_since(since, until)

    data = await service.get_hero_stats_history(
        platform=str(platform),
        gamemode=str(gamemode),
        region=str(region) if region else None,
        map_key=str(map_) if map_ else None,
        tier=str(competitive_division) if competitive_division else None,
        heroes=heroes,
        since=effective_since,
        until=until,
        limit=limit,
        offset=offset,
        cache_key=build_cache_key(request),
    )
    _apply_history_cache_headers(response, data)
    return data


@router.get(
    "/stats/dates",
    responses=storage_routes_responses,
    tags=[RouteTag.HEROES],
    summary="Get hero stats snapshot dates",
    description=(
        "List the distinct snapshot timestamps (Unix seconds) for which hero "
        "stats data exists, matching the given filters."
        "<br />`platform` and `gamemode` are required; `region`, `map` and "
        "`competitive_division` are optional filters."
        "<br />Dates are ordered most recent first, so the first element is "
        "the latest available snapshot."
        "<br />Every timestamp can be passed straight back as `since`/`until` "
        "on `/heroes/stats/history`."
        f"<br />**Cache TTL : {get_human_readable_duration(settings.hero_stats_history_cache_timeout)}.**"
    ),
    operation_id="get_hero_stats_history_dates",
    response_model=list[int],
)
async def get_hero_stats_history_dates(
    request: Request,
    response: Response,
    service: HeroServiceDep,
    platform: Annotated[
        PlayerPlatform, Query(title="Player platform filter", examples=["pc"])
    ],
    gamemode: Annotated[
        PlayerGamemode,
        Query(
            title="Gamemode",
            description="Filter on a specific gamemode.",
            examples=["competitive"],
        ),
    ],
    region: Annotated[
        PlayerRegion | None,
        Query(
            title="Region",
            description="Optional filter on a specific player region.",
            examples=["europe"],
        ),
    ] = None,
    map_: Annotated[
        str | None,
        Query(
            alias="map",
            title="Map key filter",
            pattern=MAP_KEY_PATTERN,
            examples=["busan"],
        ),
    ] = None,
    competitive_division: Annotated[
        CompetitiveDivisionHistoryFilter | None,
        Query(
            title="Competitive division filter",
            description=(
                "Optional competitive division, or 'all' for the combined "
                "snapshot across every division. Omitting it considers every "
                "division *and* the combined snapshot."
            ),
            examples=["gold"],
        ),
    ] = None,
) -> Any:
    data = await service.get_hero_stats_history_dates(
        platform=str(platform),
        gamemode=str(gamemode),
        region=str(region) if region else None,
        map_key=str(map_) if map_ else None,
        tier=str(competitive_division) if competitive_division else None,
        cache_key=build_cache_key(request),
    )
    _apply_history_cache_headers(response, data)
    return data


@router.get(
    "/{hero_key}",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": HeroParserErrorMessage,
            "description": "Hero Not Found",
        },
        **routes_responses,
    },
    tags=[RouteTag.HEROES],
    summary="Get hero data",
    description=(
        "Get data about an Overwatch hero : description, abilities, stadium powers, story, etc. "
        f"<br />**Cache TTL : {get_human_readable_duration(settings.hero_path_cache_timeout)}.**"
    ),
    operation_id="get_hero",
    response_model=Hero,
)
async def get_hero(
    request: Request,
    response: Response,
    service: HeroServiceDep,
    hero_key: Annotated[str, Path(title="Key name of the hero")],
    locale: Annotated[
        Locale, Query(title="Locale to be displayed")
    ] = Locale.ENGLISH_US,
) -> Any:
    data, is_stale, age = await service.get_hero(
        hero_key=str(hero_key), locale=locale, cache_key=build_cache_key(request)
    )
    apply_swr_headers(response, settings.hero_path_cache_timeout, is_stale, age)
    return data
