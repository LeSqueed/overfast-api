"""Stateless parser functions for maps data"""

import json
import re
from typing import TYPE_CHECKING

from app.config import settings
from app.domain.enums import MapGamemode, MapKey
from app.domain.exceptions import ParserParsingError
from app.domain.parsers.utils import (
    parse_html_root,
    safe_get_attribute,
    validate_response_status,
)
from app.domain.utils.csv_reader import read_csv_file
from app.infrastructure.logger import logger

if TYPE_CHECKING:
    from collections.abc import Collection

    from app.domain.ports import BlizzardClientPort

# Blizzard sentinel used in the map dropdown / stats endpoint for "no map filter".
ALL_MAPS_FILTER = "all-maps"

# ``static_data`` key holding the accumulated set of map keys ever observed in
# the competitive rotation, as a JSON array of strings. Kept separate from the
# ``maps:rates`` HTML so it survives any single scrape: the scrape may only add
# keys to it, never remove them.
COMPETITIVE_KEYS_STORAGE_KEY = "maps:competitive"

# Sanity thresholds a scraped dropdown must clear before it is trusted, see
# has_known_maps_quorum() for the reasoning behind each of them.
MIN_KNOWN_SCRAPED_MAPS = 10
MIN_KNOWN_SCRAPED_RATIO = 0.5

_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
_MAP_KEY_VALUES = frozenset(map_key.value for map_key in MapKey)
_MAP_GAMEMODE_VALUES = frozenset(gamemode.value for gamemode in MapGamemode)


def get_static_url_maps(key: str, extension: str = "jpg") -> str:
    """Get URL for a map screenshot"""
    return f"{settings.app_base_url}/static/maps/{key}.{extension}"


def parse_maps_csv() -> list[dict]:
    """
    Parse maps list from CSV file

    This is the authoritative maps list: every other source may only enrich it.

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


def slugify_gamemode(label: str) -> str | None:
    """Convert a scraped optgroup label into a ``MapGamemode`` value.

    Blizzard labels the dropdown groups with display names ("Payload Race")
    while the CSV uses slugs ("payload-race"), so a plain ``lower()`` produces
    a value that no ``MapGamemode`` member matches.

    Returns:
        The matching ``MapGamemode`` value, or None when the label doesn't
        correspond to any known gamemode — the map is still kept, it just
        carries no gamemode rather than an unusable one.
    """
    slug = _NON_SLUG_CHARS.sub("-", label.lower()).strip("-")
    return slug if slug in _MAP_GAMEMODE_VALUES else None


def parse_rates_maps_html(html: str) -> list[dict]:
    """
    Parse the competitive map list from the hero stats page map dropdown.

    The dropdown (``#filter-map-select``) lists the maps currently in the
    competitive rotation, grouped by gamemode. It is an *enrichment* source
    only — the CSV ``MapKey`` list stays authoritative — so the scrape may only
    flag known maps as competitive or surface a map the CSV doesn't know about
    yet. Callers should prefer :func:`parse_trusted_rates_maps_html`, which
    sanity-checks the result before it is allowed to influence anything.

    Keys are deduplicated: a map listed under several gamemode groups appears
    once, accumulating every gamemode it was listed under.

    Returns:
        List of map dicts with keys: key, name, gamemodes (``MapGamemode`` values)

    Raises:
        ParserParsingError: If HTML structure is unexpected
    """
    try:
        root_tag = parse_html_root(html)
        map_select = root_tag.css_first("select#filter-map-select")
        if map_select is None:
            msg = "Map filter dropdown (select#filter-map-select) not found"
            raise ParserParsingError(msg)

        maps: dict[str, dict] = {}
        for optgroup in map_select.css("optgroup"):
            gamemode = slugify_gamemode(safe_get_attribute(optgroup, "label") or "")
            for option in optgroup.css("option"):
                key = safe_get_attribute(option, "value")
                if not key or key == ALL_MAPS_FILTER:
                    continue
                map_dict = maps.setdefault(
                    key,
                    {
                        "key": key,
                        "name": safe_get_attribute(option, "data-title") or key,
                        "gamemodes": [],
                    },
                )
                if gamemode is not None and gamemode not in map_dict["gamemodes"]:
                    map_dict["gamemodes"].append(gamemode)

        if not maps:
            msg = "No competitive maps found in map filter dropdown"
            raise ParserParsingError(msg)

    except (AttributeError, KeyError, IndexError, TypeError) as error:
        msg = f"Failed to parse maps from rates HTML: {error!r}"
        raise ParserParsingError(msg) from error
    else:
        return list(maps.values())


def has_known_maps_quorum(scraped_maps: list[dict]) -> bool:
    """Check a scraped dropdown looks like a real map list before trusting it.

    "Parsed something" is not evidence the scrape is sound: a dropdown Blizzard
    restructures into a handful of unrelated entries parses perfectly well and
    would otherwise silently replace the competitive rotation.

    Two thresholds must both hold, on the count of scraped keys that are known
    ``MapKey`` values:

    - at least ``MIN_KNOWN_SCRAPED_MAPS`` known maps — a plausible floor for a
      competitive rotation, which a junk dropdown of a few entries can't reach;
    - at least ``MIN_KNOWN_SCRAPED_RATIO`` of the scraped entries recognised —
      so a dropdown padded with unrecognised entries is rejected even when it
      also happens to contain enough real maps.

    Both are expressed against the *scraped* entries rather than as a fraction
    of the CSV on purpose: Blizzard legitimately rotates maps out (the CSV holds
    every map ever released, far more than are ever in rotation), so a
    "fraction of the CSV present" rule would reject healthy scrapes.
    """
    known_count = _count_known_maps(scraped_maps)
    return (
        known_count >= MIN_KNOWN_SCRAPED_MAPS
        and known_count >= len(scraped_maps) * MIN_KNOWN_SCRAPED_RATIO
    )


def parse_trusted_rates_maps_html(html: str) -> list[dict] | None:
    """Parse the map dropdown, returning None when the result can't be trusted.

    Returns None — after logging a warning — when the dropdown is missing,
    empty, unparseable, or fails :func:`has_known_maps_quorum`, so callers
    degrade to the CSV instead of acting on a bad scrape.
    """
    try:
        scraped_maps = parse_rates_maps_html(html)
    except ParserParsingError as error:
        logger.warning("Ignoring unusable competitive map dropdown: {}", error)
        return None

    if not has_known_maps_quorum(scraped_maps):
        logger.warning(
            "Ignoring competitive map dropdown failing the known-map quorum: "
            "{} entries scraped, only {} known maps",
            len(scraped_maps),
            _count_known_maps(scraped_maps),
        )
        return None

    return scraped_maps


def decode_competitive_keys(stored: object) -> frozenset[str]:
    """Decode the accumulated competitive map keys from a ``static_data`` record.

    Takes the record as returned by ``StoragePort.get_static_data`` (or None on
    a miss) and returns the empty set for anything it can't make sense of, so a
    missing or corrupted row degrades to "nothing remembered" instead of raising
    on the maps read path.
    """
    if stored is None:
        return frozenset()

    data = stored.get("data") if isinstance(stored, dict) else None
    if not isinstance(data, str):
        logger.warning("Unexpected stored competitive map keys: {!r}", stored)
        return frozenset()

    try:
        keys = json.loads(data)
    except ValueError as error:
        logger.warning("Ignoring unreadable competitive map keys: {}", error)
        return frozenset()

    if not isinstance(keys, list):
        logger.warning("Ignoring malformed competitive map keys: {!r}", keys)
        return frozenset()

    return frozenset(key for key in keys if isinstance(key, str))


def encode_competitive_keys(keys: Collection[str]) -> str:
    """Serialise the accumulated competitive map keys for persistent storage."""
    return json.dumps(sorted(keys), separators=(",", ":"))


def parse_maps_html(html: str, known_competitive: Collection[str] = ()) -> list[dict]:
    """
    Parse the full maps list: the CSV baseline, enriched by the scraped dropdown.

    The CSV is the source of truth for which maps exist and for their metadata.
    The scrape may only flag which of them are in the competitive rotation and
    add a map the CSV doesn't know about yet (with null metadata, so a newly
    released map never breaks the API).

    ``known_competitive`` holds the map keys ever observed in the rotation,
    accumulated across scrapes by the caller. It makes the ``competitive`` flag
    monotonic: the scrape can only ever add to it, so a map already known to be
    competitive keeps reporting True through a missing dropdown, a markup
    change, a failed quorum, or its own absence from one dropdown reading.

    ``competitive`` resolves as:

    - True when the key is remembered or in a trusted scrape;
    - False when we have positive information about the rotation — a trusted
      scrape, or a non-empty remembered set — and the key is in neither;
    - None only under total ignorance: nothing remembered and no usable scrape.

    A scrape failure therefore never fails a maps request, and never downgrades
    what is already known.

    Returns:
        List of map dicts with keys: key, name, screenshot, gamemodes,
        location, country_code, competitive
    """
    csv_by_key = {map_dict["key"]: map_dict for map_dict in parse_maps_csv()}
    scraped_maps = parse_trusted_rates_maps_html(html)
    scraped_by_key = {map_dict["key"]: map_dict for map_dict in scraped_maps or []}

    competitive_keys = frozenset(known_competitive) | scraped_by_key.keys()
    has_competitive_info = scraped_maps is not None or bool(known_competitive)

    entries = []
    for key in sorted(csv_by_key.keys() | competitive_keys):
        if key in csv_by_key:
            entries.append(
                _csv_map_entry(
                    csv_by_key[key],
                    competitive=_resolve_competitive(
                        key, competitive_keys, has_info=has_competitive_info
                    ),
                )
            )
        elif key in scraped_by_key:
            entries.append(_scraped_map_entry(scraped_by_key[key]))
        else:
            entries.append(_remembered_map_entry(key))

    return entries


def competitive_keys_of(maps: list[dict]) -> frozenset[str]:
    """Collect the keys flagged competitive in a parsed maps list.

    This is what a caller unions into the persisted set: it already combines
    what was remembered with whatever the scrape just promoted.
    """
    return frozenset(map_dict["key"] for map_dict in maps if map_dict["competitive"])


def _count_known_maps(scraped_maps: list[dict]) -> int:
    """Count scraped entries whose key is a known CSV ``MapKey`` value."""
    return sum(1 for map_dict in scraped_maps if map_dict["key"] in _MAP_KEY_VALUES)


def _resolve_competitive(
    key: str, competitive_keys: frozenset[str], *, has_info: bool
) -> bool | None:
    """Resolve the ``competitive`` flag of a CSV map key."""
    if key in competitive_keys:
        return True
    return False if has_info else None


def _csv_map_entry(csv_map: dict, *, competitive: bool | None) -> dict:
    """Build a maps list entry from the CSV baseline."""
    return {
        "key": csv_map["key"],
        "name": csv_map["name"],
        "screenshot": csv_map["screenshot"],
        "gamemodes": csv_map["gamemodes"],
        "location": csv_map["location"],
        "country_code": csv_map["country_code"],
        "competitive": competitive,
    }


def _scraped_map_entry(scraped_map: dict) -> dict:
    """Build a maps list entry for a map the CSV doesn't know about yet."""
    return {
        "key": scraped_map["key"],
        "name": scraped_map["name"] or scraped_map["key"],
        "screenshot": None,
        "gamemodes": scraped_map["gamemodes"],
        "location": None,
        "country_code": None,
        "competitive": True,
    }


def _remembered_map_entry(key: str) -> dict:
    """Build a maps list entry for a remembered map absent from CSV and scrape.

    Such a map was scraped as competitive at some point but isn't in the CSV, so
    the only thing left of it is its key. Dropping it from the list would demote
    it just as surely as flipping its flag, so it is kept with null metadata.
    """
    return {
        "key": key,
        "name": key,
        "screenshot": None,
        "gamemodes": [],
        "location": None,
        "country_code": None,
        "competitive": True,
    }
