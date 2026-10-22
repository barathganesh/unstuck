from datetime import date
from importlib.resources import files
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from unstuck.domain import Contract, CountryCode, Identifier, JoinMode

Key = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^(country:)?[A-Z]{2}$")
]


class Order(Contract):
    order_id: Identifier
    country_id: Key
    order_date_utc: date
    units: Annotated[int, Field(ge=0, le=1_000_000)]


class Country(Contract):
    country_id: Key
    country_code: CountryCode
    country_name: Annotated[str, Field(min_length=1, max_length=128)]


class Fixture(Contract):
    classification: Literal["synthetic"]
    dataset_id: Identifier
    dataset_version: Identifier
    order_date_utc: date
    orders: Annotated[tuple[Order, ...], Field(max_length=1000)]
    countries: Annotated[tuple[Country, ...], Field(max_length=250)]
    job_state: Literal["pending", "complete", "failed"]
    scope_complete: bool
    metadata_complete: bool
    scope_covered: bool
    orders_key_encoding: Literal["plain", "country_prefix", "unknown"]
    countries_key_encoding: Literal["plain", "country_prefix", "unknown"]
    approved_join_modes: Annotated[tuple[JoinMode, ...], Field(max_length=2)]
    arrival_after_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = (
        None
    )

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        if len({order.order_id for order in self.orders}) != len(self.orders):
            raise ValueError("duplicate order IDs")
        if len({country.country_id for country in self.countries}) != len(
            self.countries
        ):
            raise ValueError("duplicate dimension IDs")
        return self


def load_fixture() -> Fixture:
    return Fixture.model_validate_json(
        files("unstuck").joinpath("data/ds-17.json").read_text()
    )
