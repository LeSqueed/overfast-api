import pytest

from app.domain.enums import MapGamemode, MapKey
from app.domain.exceptions import ParserParsingError
from app.domain.parsers.maps import (
    MIN_KNOWN_SCRAPED_MAPS,
    has_known_maps_quorum,
    parse_maps_csv,
    parse_maps_html,
    parse_rates_maps_html,
    parse_trusted_rates_maps_html,
    slugify_gamemode,
)

MAP_ENTRY_KEYS = {
    "key",
    "name",
    "screenshot",
    "gamemodes",
    "location",
    "country_code",
    "competitive",
}


def _build_dropdown(options_by_gamemode: dict[str, list[str]]) -> str:
    """Build a minimal rates page with the given ``label -> map keys`` groups."""
    optgroups = "".join(
        '<optgroup label="{}">{}</optgroup>'.format(
            label,
            "".join(
                f'<option data-title="{key}" value="{key}">{key}</option>'
                for key in keys
            ),
        )
        for label, keys in options_by_gamemode.items()
    )
    return (
        "<html><body><main class='main-content'>"
        f'<select id="filter-map-select">{optgroups}</select>'
        "</main></body></html>"
    )


def test_parse_maps_csv_returns_all_maps():
    result = parse_maps_csv()

    assert isinstance(result, list)
    assert len(result) > 0
    assert {m["key"] for m in result} == {str(m) for m in MapKey}


def test_parse_maps_csv_entry_format():
    result = parse_maps_csv()
    first = result[0]

    assert set(first.keys()) == {
        "key",
        "name",
        "screenshot",
        "gamemodes",
        "location",
        "country_code",
    }
    assert first["key"] == "aatlis"
    assert isinstance(first["gamemodes"], list)


def test_parse_rates_maps_html_lists_competitive_maps(rates_maps_html_data: str):
    known_gamemodes = {str(gamemode) for gamemode in MapGamemode}

    result = parse_rates_maps_html(rates_maps_html_data)

    assert isinstance(result, list)
    assert len(result) >= MIN_KNOWN_SCRAPED_MAPS
    assert "busan" in {m["key"] for m in result}
    assert all(set(m.keys()) == {"key", "name", "gamemodes"} for m in result)
    assert all(
        gamemode in known_gamemodes for m in result for gamemode in m["gamemodes"]
    )


def test_parse_rates_maps_html_keys_are_a_subset_of_the_csv(rates_maps_html_data: str):
    result = parse_rates_maps_html(rates_maps_html_data)

    assert {m["key"] for m in result} <= {str(m) for m in MapKey}


def test_parse_rates_maps_html_excludes_all_maps(rates_maps_html_data: str):
    result = parse_rates_maps_html(rates_maps_html_data)

    assert "all-maps" not in {m["key"] for m in result}


def test_parse_rates_maps_html_missing_dropdown_raises():
    html = "<html><body><main class='main-content'></main></body></html>"

    with pytest.raises(ParserParsingError):
        parse_rates_maps_html(html)


def test_parse_rates_maps_html_empty_dropdown_raises():
    html = (
        "<html><body><main class='main-content'>"
        "<select id='filter-map-select'></select>"
        "</main></body></html>"
    )

    with pytest.raises(ParserParsingError):
        parse_rates_maps_html(html)


def test_parse_rates_maps_html_empty_name_falls_back_to_key(rates_maps_html_data: str):
    html = rates_maps_html_data.replace('data-title="Busan"', 'data-title=""')

    result = parse_rates_maps_html(html)

    assert {m["key"]: m["name"] for m in result}["busan"] == "busan"


def test_parse_rates_maps_html_dedupes_keys_across_optgroups():
    html = _build_dropdown({"Control": ["busan"], "Push": ["busan"]})

    result = parse_rates_maps_html(html)

    assert len(result) == 1
    assert result[0]["key"] == "busan"


def test_parse_rates_maps_html_accumulates_gamemodes_of_duplicate_keys():
    html = _build_dropdown({"Control": ["busan"], "Payload Race": ["busan"]})

    result = parse_rates_maps_html(html)

    assert result[0]["gamemodes"] == ["control", "payload-race"]


def test_parse_rates_maps_html_drops_unknown_gamemode_label():
    html = _build_dropdown({"Brand New Mode": ["busan"]})

    result = parse_rates_maps_html(html)

    assert result[0]["key"] == "busan"
    assert result[0]["gamemodes"] == []


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Control", "control"),
        ("Payload Race", "payload-race"),
        ("  Capture the Flag  ", "capture-the-flag"),
        ("TEAM  DEATHMATCH", "team-deathmatch"),
        ("Brand New Mode", None),
        ("", None),
    ],
)
def test_slugify_gamemode(label: str, expected: str | None):
    result = slugify_gamemode(label)

    assert result == expected


def test_has_known_maps_quorum_accepts_the_real_dropdown(rates_maps_html_data: str):
    scraped_maps = parse_rates_maps_html(rates_maps_html_data)

    result = has_known_maps_quorum(scraped_maps)

    assert result is True


def test_has_known_maps_quorum_rejects_too_few_known_maps():
    scraped_maps = [{"key": key} for key in ("busan", "ilios", "nepal")]

    result = has_known_maps_quorum(scraped_maps)

    assert result is False


def test_has_known_maps_quorum_rejects_mostly_unknown_entries():
    known = [{"key": str(map_key)} for map_key in list(MapKey)[:MIN_KNOWN_SCRAPED_MAPS]]
    junk = [
        {"key": f"junk-entry-{index}"} for index in range(MIN_KNOWN_SCRAPED_MAPS + 1)
    ]

    result = has_known_maps_quorum([*known, *junk])

    assert result is False


def test_parse_trusted_rates_maps_html_returns_none_on_missing_dropdown():
    html = "<html><body><main class='main-content'></main></body></html>"

    result = parse_trusted_rates_maps_html(html)

    assert result is None


def test_parse_trusted_rates_maps_html_returns_none_on_junk_dropdown():
    html = _build_dropdown({"Control": ["junk-one", "junk-two", "junk-three"]})

    result = parse_trusted_rates_maps_html(html)

    assert result is None


def test_parse_maps_html_returns_the_csv_baseline(rates_maps_html_data: str):
    result = parse_maps_html(rates_maps_html_data)

    assert {str(k) for k in MapKey} <= {m["key"] for m in result}
    assert all(set(m.keys()) == MAP_ENTRY_KEYS for m in result)
    assert [m["key"] for m in result] == sorted(m["key"] for m in result)


def test_parse_maps_html_enriches_csv_maps_with_competitive_flag(
    rates_maps_html_data: str,
):
    result = parse_maps_html(rates_maps_html_data)
    by_key = {m["key"]: m for m in result}

    busan = by_key["busan"]
    assert busan["competitive"] is True
    assert busan["location"] is not None
    assert busan["screenshot"] is not None
    assert by_key["anubis"]["competitive"] is False


def test_parse_maps_html_new_map_falls_back_to_null(rates_maps_html_data: str):
    html = rates_maps_html_data.replace(
        'data-title="Suravasa" value="suravasa"',
        'data-title="Brand New Map" value="brand-new-map"',
    )

    result = parse_maps_html(html)
    by_key = {m["key"]: m for m in result}

    new_map = by_key["brand-new-map"]
    assert new_map["competitive"] is True
    assert new_map["name"] == "Brand New Map"
    assert new_map["location"] is None
    assert new_map["country_code"] is None
    assert new_map["screenshot"] is None


def test_parse_maps_html_keeps_non_ascii_scraped_only_map_name(
    rates_maps_html_data: str,
):
    html = rates_maps_html_data.replace(
        'data-title="Paraíso" value="paraiso"',
        'data-title="Château Vermeil" value="chateau-vermeil"',
    )

    result = parse_maps_html(html)
    by_key = {m["key"]: m for m in result}

    assert by_key["chateau-vermeil"]["name"] == "Château Vermeil"
    assert by_key["chateau-vermeil"]["competitive"] is True


def test_parse_maps_html_keeps_non_ascii_csv_map_name(rates_maps_html_data: str):
    result = parse_maps_html(rates_maps_html_data)
    by_key = {m["key"]: m for m in result}

    assert by_key["paraiso"]["name"] == "Paraíso"
    assert by_key["esperanca"]["name"] == "Esperança"


def test_parse_maps_html_empty_scraped_name_falls_back_to_key(
    rates_maps_html_data: str,
):
    html = rates_maps_html_data.replace(
        'data-title="Suravasa" value="suravasa"',
        'data-title="" value="brand-new-map"',
    )

    result = parse_maps_html(html)
    by_key = {m["key"]: m for m in result}

    assert by_key["brand-new-map"]["name"] == "brand-new-map"


def test_parse_maps_html_degrades_to_csv_on_missing_dropdown():
    html = "<html><body><main class='main-content'></main></body></html>"

    result = parse_maps_html(html)

    assert {m["key"] for m in result} == {str(m) for m in MapKey}
    assert all(m["competitive"] is None for m in result)


def test_parse_maps_html_degrades_to_csv_on_empty_dropdown():
    html = (
        "<html><body><main class='main-content'>"
        "<select id='filter-map-select'></select>"
        "</main></body></html>"
    )

    result = parse_maps_html(html)

    assert {m["key"] for m in result} == {str(m) for m in MapKey}
    assert all(m["competitive"] is None for m in result)


def test_parse_maps_html_degrades_to_csv_on_junk_dropdown():
    html = _build_dropdown({"Control": ["junk-one", "junk-two", "junk-three"]})

    result = parse_maps_html(html)

    assert {m["key"] for m in result} == {str(m) for m in MapKey}
    assert all(m["competitive"] is None for m in result)


def test_parse_maps_html_degrades_to_csv_on_broken_html():
    result = parse_maps_html("<not-even-html>")

    assert {m["key"] for m in result} == {str(m) for m in MapKey}
    assert all(m["competitive"] is None for m in result)


def test_parse_maps_html_keeps_csv_metadata_when_degraded():
    result = parse_maps_html("<not-even-html>")
    by_key = {m["key"]: m for m in result}

    assert by_key["busan"]["location"] is not None
    assert by_key["busan"]["screenshot"] is not None
    assert by_key["busan"]["gamemodes"] == ["control"]
