"""Frozen Pydantic result types — exact copies from AGENTS.md §2.

Downstream lanes import these. Never redeclare a local equivalent — two lanes
inventing two flight models is the single most expensive merge failure available.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class BookingRequest(BaseModel):
    url: str
    post_data: dict | None = None  # when present, MUST be POSTed unchanged
    vendor: str


class NormalizedFlight(BaseModel):
    carrier: str
    flight_numbers: list[str]
    depart: datetime
    arrive: datetime
    stops: int
    duration_minutes: int
    price_usd: Decimal
    selected_flights_json: dict  # NOT booking_token — see AGENTS.md §6
    booking_request: BookingRequest | None
    observed_at: datetime


class NormalizedLodging(BaseModel):
    property_id: str
    name: str
    rating: float | None
    price_per_night_usd: Decimal
    booking_request: BookingRequest
    cancellation_terms: str | None
    observed_at: datetime


class NormalizedPlace(BaseModel):
    google_place_id: str  # Places API (New) `id` — NOT `name`, see AGENTS.md §6
    name: str  # from `displayName`
    address: str
    lat: float
    lon: float
    rating: float | None
    price_level: int | None
    phone: str | None  # from `nationalPhoneNumber`
    maps_url: str
    # no booking_request — reference tier, see AGENTS.md §5


class NormalizedActivity(BaseModel):
    external_id: str
    name: str
    rating: float | None
    kind: Literal["attraction", "experience", "event", "poi"]
    link: str
    price_basis: Literal["uncounted"] = "uncounted"
