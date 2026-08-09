"""Stateless parser functions for maps data"""

from typing import TYPE_CHECKING

from app.config import settings
from app.domain.utils.csv_reader import read_csv_file

if TYPE_CHECKING:
    from app.domain.ports import BlizzardClientPort

from app.domain.exceptions import ParserParsingError
from app.domain.parsers.utils import (
    parse_html_root,
    safe_get_attribute,
    validate_response_status,
)


def get_static_url_maps(key: str, extension: str = "jpg") -> str:
    """Get URL for a map screenshot"""
    return f"{settings.app_base_url}/static/maps/{key}.{extension}"


def parse_maps_csv() -> list[dict]:
    """
    Parse maps list from CSV file

    Returns:
        List of map dicts with keys: key, name, screenshot, gamemodes, location, country_code
    """
    csv_data = read_csv_file("maps")

    return [
        {
            "key": map_dict["key"],
            "name": map_dict["name"],
            "screenshot": get_static_url_maps(map_dict["key"]),
            "gamemodes": map_dict["gamemodes"].split(","),
            "location": map_dict["location"],
            "country_code": map_dict.get("country_code") or None,
        }
        for map_dict in csv_data
    ]


async def fetch_rates_html(client: BlizzardClientPort) -> str:
    """Fetch the hero stats page HTML (hosts the map filter dropdown).

    Raises:
        HTTPException: If Blizzard returns non-200 status
    """
    url = f"{settings.blizzard_host}{settings.rates_path}"
    response = await client.get(url, headers={"Accept": "text/html"})
    validate_response_status(response)
    return response.text


def parse_rates_maps_html(html: str) -> list[dict]:
    """
    Parse the competitive map list from the hero stats page map dropdown.

    The dropdown (``#filter-map-select``) lists every competitive map, grouped
    by gamemode. This is the authoritative source for which maps are in the
    competitive rotation and gets updated by Blizzard on map releases.

    Returns:
        List of map dicts with keys: key, name, gamemode (lowercase)

    Raises:
        ParserParsingError: If HTML structure is unexpected
    """
    try:
        root_tag = parse_html_root(html)
        map_select = root_tag.css_first("select#filter-map-select")
        if map_select is None:
            msg = "Map filter dropdown (select#filter-map-select) not found"
            raise ParserParsingError(msg)

        maps = []
        for optgroup in map_select.css("optgroup"):
            gamemode = (safe_get_attribute(optgroup, "label") or "").lower()
            for option in optgroup.css("option"):
                key = safe_get_attribute(option, "value")
                if not key or key == "all-maps":
                    continue
                maps.append(
                    {
                        "key": key,
                        "name": safe_get_attribute(option, "data-title") or key,
                        "gamemode": gamemode,
                    }
                )
        if not maps:
            msg = "No competitive maps found in map filter dropdown"
            raise ParserParsingError(msg)

    except (AttributeError, KeyError, IndexError, TypeError) as error:
        msg = f"Failed to parse maps from rates HTML: {error!r}"
        raise ParserParsingError(msg) from error
    else:
        return maps


def parse_maps_html(html: str) -> list[dict]:
    """
    Parse the full maps list, merging the scraped competitive maps with the CSV.

    The scraped competitive map list is the source of truth for which maps are
    competitive (and their gamemode). The CSV enriches each map with
    screenshot, location and country_code; maps not yet in the CSV fall back to
    null values so a newly released map never breaks the API.

    Returns:
        List of map dicts with keys: key, name, screenshot, gamemodes,
        location, country_code, competitive
    """
    competitive_maps = parse_rates_maps_html(html)
    competitive_by_key = {map_dict["key"]: map_dict for map_dict in competitive_maps}

    csv_by_key = {map_dict["key"]: map_dict for map_dict in parse_maps_csv()}

    result = []
    for key in sorted(set(competitive_by_key) | set(csv_by_key)):
        scraped = competitive_by_key.get(key)
        csv_map = csv_by_key.get(key)

        if csv_map is not None:
            entry = {
                "key": key,
                "name": csv_map["name"],
                "screenshot": csv_map["screenshot"],
                "gamemodes": csv_map["gamemodes"],
                "location": csv_map["location"],
                "country_code": csv_map["country_code"],
            }
        else:
            entry = {
                "key": key,
                "name": (scraped["name"] or key) if scraped else key,
                "screenshot": None,
                "gamemodes": [scraped["gamemode"]] if scraped else [],
                "location": None,
                "country_code": None,
            }

        entry["competitive"] = key in competitive_by_key
        result.append(entry)

    return result
