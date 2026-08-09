import pytest

from app.domain.enums import MapKey
from app.domain.exceptions import ParserParsingError
from app.domain.parsers.maps import (
    parse_maps_csv,
    parse_maps_html,
    parse_rates_maps_html,
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
    result = parse_rates_maps_html(rates_maps_html_data)

    assert isinstance(result, list)
    assert len(result) == 30  # noqa: PLR2004
    assert "busan" in {m["key"] for m in result}
    assert all(
        m["gamemode"] in {"control", "escort", "flashpoint", "hybrid", "push"}
        for m in result
    )


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


def test_parse_maps_html_merges_csv_and_scraped(rates_maps_html_data: str):
    result = parse_maps_html(rates_maps_html_data)
    by_key = {m["key"]: m for m in result}

    # All CSV maps are present, with a competitive flag.
    assert {str(k) for k in MapKey} <= set(by_key)
    assert all("competitive" in m for m in result)

    # Scraped competitive maps are flagged competitive and keep CSV metadata.
    busan = by_key["busan"]
    assert busan["competitive"] is True
    assert busan["location"] is not None
    assert busan["screenshot"] is not None

    # Non-competitive CSV maps are flagged non-competitive.
    anubis = by_key["anubis"]
    assert anubis["competitive"] is False


def test_parse_maps_html_new_map_falls_back_to_null(rates_maps_html_data: str):
    # Simulate a freshly released map present in the dropdown but not the CSV.
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


def test_parse_maps_html_empty_scraped_name_falls_back_to_key(rates_maps_html_data: str):
    html = rates_maps_html_data.replace(
        'data-title="Suravasa" value="suravasa"',
        'data-title="" value="brand-new-map"',
    )

    result = parse_maps_html(html)
    by_key = {m["key"]: m for m in result}

    assert by_key["brand-new-map"]["name"] == "brand-new-map"
