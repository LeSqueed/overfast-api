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
    competitive: bool = Field(
        ...,
        description=(
            "Whether the map is in the competitive rotation (has hero stats data)."
        ),
        examples=[True],
    )
