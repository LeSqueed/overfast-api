"""Heroes endpoints router : heroes list, heroes details, etc."""

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request, Response, status

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
    HeroGamemode,
    HeroKey,
    Locale,
    MapKey,
    PlayerGamemode,
    PlayerPlatform,
    PlayerRegion,
    Role,
    SubRole,
)

router = APIRouter()


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
    apply_swr_headers(
        response,
        settings.heroes_path_cache_timeout,
        is_stale,
        age,
        staleness_threshold=settings.heroes_staleness_threshold,
    )
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
        MapKey | None, Query(alias="map", title="Map key filter", examples=["hanaoka"])
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
        **routes_responses,
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
        "`tier` and `hero` are optional filters, and `since`/`until` bound "
        "the snapshot timestamps."
        "<br />Each point carries the full context (platform, gamemode, "
        "region, map, tier, hero, captured_at) so clients can group or "
        "filter the series themselves."
        "<br />Note: each `captured_at` is the time *we* recorded the "
        "reading; Blizzard does not expose the window it aggregates over."
    ),
    operation_id="get_hero_stats_history",
    response_model=list[HeroStatsHistoryPoint],
)
async def get_hero_stats_history(
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
        MapKey | None,
        Query(alias="map", title="Map key filter", examples=["busan"]),
    ] = None,
    tier: Annotated[
        str | None,
        Query(
            title="Competitive tier",
            description=(
                "Optional competitive division, or 'all' for the combined "
                "snapshot. Only tiers present in captured snapshots are returned."
            ),
            examples=["gold", "all"],
        ),
    ] = None,
    hero: Annotated[
        HeroKey | None, Query(title="Hero key filter", examples=["ana"])
    ] = None,
    since: Annotated[
        int | None,
        Query(title="Lower bound (Unix timestamp)", ge=0),
    ] = None,
    until: Annotated[
        int | None,
        Query(title="Upper bound (Unix timestamp)", ge=0),
    ] = None,
) -> Any:
    data = await service.get_hero_stats_history(
        platform=str(platform),
        gamemode=str(gamemode),
        region=str(region) if region else None,
        map_key=str(map_) if map_ else None,
        tier=tier,
        hero=str(hero) if hero else None,
        since=since,
        until=until,
    )
    apply_swr_headers(
        response,
        settings.hero_stats_cache_timeout,
        False,
        0,
    )
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
    hero_key: Annotated[HeroKey, Path(title="Key name of the hero")],
    locale: Annotated[
        Locale, Query(title="Locale to be displayed")
    ] = Locale.ENGLISH_US,
) -> Any:
    data, is_stale, age = await service.get_hero(
        hero_key=str(hero_key), locale=locale, cache_key=build_cache_key(request)
    )
    apply_swr_headers(
        response,
        settings.hero_path_cache_timeout,
        is_stale,
        age,
        staleness_threshold=settings.heroes_staleness_threshold,
    )
    return data
