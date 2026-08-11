"""Set of pydantic models used for Maps API routes"""

from pydantic import BaseModel, Field, HttpUrl

from app.domain.enums import MapGamemode


class Map(BaseModel):
    key: str = Field(
        ...,
        description="Key name of the map",
        examples=["aatlis"],
    )
    name: str = Field(..., description="Name of the map", examples=["Aatlis"])
    screenshot: HttpUrl | None = Field(
        None,
        description=(
            "Screenshot of the map. Null for newly released maps that aren't "
            "in the CSV yet."
        ),
        examples=["https://overfast-api.tekrop.fr/static/maps/aatlis.jpg"],
    )
    gamemodes: list[MapGamemode] = Field(
        ...,
        description="Main gamemodes on which the map is playable",
    )
    location: str | None = Field(
        None,
        description=(
            "Location of the map. Null for newly released maps that aren't "
            "in the CSV yet."
        ),
        examples=["Morocco"],
    )
    country_code: str | None = Field(
        ...,
        min_length=2,
        max_length=2,
        description=(
            "Country Code of the location of the map. If not defined, it's null."
        ),
        examples=["MA"],
    )
    competitive: bool | None = Field(
        ...,
        description=(
            "Whether the map has been listed in Blizzard's competitive map filter. "
            "True is sticky: once a map has been seen in that listing it keeps "
            "reporting true, because reading the listing may only promote a map, "
            "never demote one — a failed or unusable read, or the map dropping "
            "out of the listing, leaves the flag untouched. False means the "
            "listing is known and this map isn't in it. Null means no competitive "
            "information is available at all, so an unavailable listing isn't "
            "reported as 'not competitive'. Note that true is not a guarantee "
            "that hero statistics exist for the map: it reports what the filter "
            "listed, not what the stats endpoints will return."
        ),
        examples=[True],
    )
