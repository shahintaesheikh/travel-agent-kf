"""Frozen Pydantic result types — exact copies from AGENTS.md §2.

Downstream lanes import these. Never redeclare a local equivalent — two lanes
inventing two flight models is the single most expensive merge failure available.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

__all__ = [
    "BookingRequest",
    "NormalizedActivity",
    "NormalizedFlight",
    "NormalizedLodging",
    "NormalizedPlace",
    "PriceStatus",
    "PriceUnit",
    "PriceUnitMismatch",
    "Priced",
    "price_drift",
]

PriceUnit = Literal["total", "per_night"]
"""What a price is measured in.

Flights are sold as a trip total; lodging is quoted per night. One field name
cannot silently mean both — see `Priced.price_unit`.
"""


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

    price_basis: Literal["actual", "uncounted"] = "uncounted"
    """Per-item, not per-provider — PRD FR-37 and US-016.

    Tripadvisor returns no price at all for experiences or attractions, so its
    results keep the `uncounted` default and must never be given one. Google
    Events sets `actual` only when `extracted_price` is present; an event
    without a price is `uncounted`, not zero. An invented estimate is worse
    than an honest gap (AGENTS.md §6).

    `price_level_estimate` is deliberately absent: it belongs to
    `NormalizedPlace.price_level`, and activities have no path to it.
    """

    price_usd: Decimal | None = None
    """Non-null if and only if `price_basis == "actual"` — enforced below.

    `uncounted` items are excluded from the budget entirely, so a price here
    with an `uncounted` basis would be counted by nobody and trusted by
    somebody.
    """

    @model_validator(mode="after")
    def _price_usd_iff_actual(self) -> NormalizedActivity:
        has_price = self.price_usd is not None
        if has_price and self.price_basis != "actual":
            raise ValueError(
                "price_usd is set but price_basis is "
                f"{self.price_basis!r} — a priced item is 'actual'"
            )
        if not has_price and self.price_basis == "actual":
            raise ValueError(
                "price_basis is 'actual' but price_usd is None — "
                "'actual' means a real observed price, not a missing one"
            )
        return self


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
    number is worse than an honest gap.

    Comparable to the stored price for this item, in the unit named by
    `price_unit` — never a stay total where the stored price is per-night."""

    price_unit: PriceUnit
    """Required, no default. An unlabelled price is the defect.

    Flights store a trip total (`NormalizedFlight.price_usd`); lodging stores
    per-night (`NormalizedLodging.price_per_night_usd`). Putting a stay total
    here for a lodging item reads as an N× price move and marks every lodging
    item `stale` on the first re-price. Worse, the budget layer sums these into
    committed and proposed spend — a per-night figure summed into a trip total
    is a silent money bug landing exactly where nobody checks (FR-35 headroom).

    Required even when `status is unavailable`: the unit is a property of how
    the item is priced, knowable whether or not a price came back, and making
    it conditional reintroduces the unlabelled-price defect on the one path
    where a stale comparison matters most.

    Where a stay total exists, keep it in the adapter's log or normalized
    payload — not here. Compare with `price_drift`, which refuses to compare
    across units.
    """

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


class PriceUnitMismatch(Exception):
    """A re-price was compared against a price in a different unit.

    Raised, not returned. `QuotaExceeded` is a value because exhausting a quota
    is an expected outcome a caller handles; comparing a per-night quote to a
    stay total is a bug in the caller, and the drift it would compute looks
    entirely plausible. Silence is the whole hazard here.
    """


def price_drift(
    priced: Priced,
    stored_price_usd: Decimal,
    stored_price_unit: PriceUnit,
) -> Decimal:
    """Fractional change from a stored price to a re-price, same unit only.

    Signed: positive means the price rose. Callers apply their own threshold
    to `abs(...)` — US-029's is 5%, configurable.

    Asserts the unit matches before comparing. A 4-night stay at $150/night
    re-priced against a $600 stay total is a 300% "move" and would mark a
    perfectly fine listing `stale`; raising is the only outcome that can't be
    mistaken for a real answer.

    Raises:
        PriceUnitMismatch: the two prices are in different units.
        ValueError: `priced` carries no price (status `unavailable` — check
            `status` before comparing), or the stored price is zero.
    """
    if priced.price_unit != stored_price_unit:
        raise PriceUnitMismatch(
            f"cannot compare a {priced.price_unit!r} re-price against a "
            f"{stored_price_unit!r} stored price for ref {priced.ref!r}"
        )
    if priced.price_usd is None:
        raise ValueError(
            f"re-price for ref {priced.ref!r} has no price "
            f"(status {priced.status.value!r}) — check status before comparing"
        )
    if stored_price_usd == 0:
        raise ValueError(f"stored price for ref {priced.ref!r} is zero — no drift to compute")
    return (priced.price_usd - stored_price_usd) / stored_price_usd
