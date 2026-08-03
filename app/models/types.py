"""Frozen Pydantic result types — exact copies from AGENTS.md §2.

Downstream lanes import these. Never redeclare a local equivalent — two lanes
inventing two flight models is the single most expensive merge failure available.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

__all__ = [
    "BookingRequest",
    "NormalizedActivity",
    "NormalizedFlight",
    "NormalizedLodging",
    "NormalizedPlace",
    "PriceStatus",
    "Priced",
]


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


class PriceStatus(enum.StrEnum):
    """Outcome of a re-price.

    Three cases, not two booleans. `available` and `split_options_only` both
    mean "the itinerary still exists and has a price", but only the first can
    become an actionable handoff — and `if priced.still_available: hand_off()`
    reads as correct while being wrong. An enum makes the reader handle the
    third case instead of remembering an `and not`.
    """

    available = "available"
    """One booking, one link. Eligible for handoff."""

    split_options_only = "split_options_only"
    """Sellable only as separate departing and returning options — no single
    link can charge it, so it cannot be an actionable handoff. Distinct from
    `unavailable`: the itinerary exists and is priced, it just needs two
    bookings."""

    unavailable = "unavailable"
    """Itinerary or listing is gone. `price_usd` is None."""


class Priced(BaseModel):
    """Result of the unconditional re-price before every handoff (US-029).

    Frozen: the money path compares this against a stored price to decide
    whether an item goes `stale`, and a mutated copy would silently change
    that decision.
    """

    model_config = ConfigDict(frozen=True)

    ref: str
    """Echoes the ref passed to `reprice()` — `selected_flights_json` for
    flights, `property_id` for lodging."""

    status: PriceStatus

    price_usd: Decimal | None = None
    """None when `unavailable`. Never a fabricated estimate — an invented
    number is worse than an honest gap."""

    observed_at: datetime
    """UTC-aware. System timestamp, not provider-local — see AGENTS.md §2."""

    booking_request: BookingRequest | None = None
    """A currently-valid way to charge this one selection, when the provider
    returned one alongside the price.

    Flights re-price by POSTing `selected_flights_json` to the booking-options
    endpoint, so fresh `post_data` arrives in the same response as the price.
    Without this field the adapter would fetch a valid payload, discard it, and
    the handoff would POST the copy captured at `item_pending` — up to a
    12-hour veto window earlier. `post_data` is opaque and expires, which is
    the same reason AGENTS.md §6 forbids persisting `booking_token`.

    None where the provider has nothing to give: Airbnb's `booking_url` is a
    dated GET link, and Google Hotels per-vendor prices carry no booking URL.
    Callers fall back to the stored `booking_request` when this is None.
    """

    unavailable_reason: str | None = None
    """Human-readable, surfaced to the agent when the item returns as `stale`."""
