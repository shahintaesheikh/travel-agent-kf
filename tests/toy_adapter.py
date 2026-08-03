"""Toy adapter proving the ProviderAdapter Protocol end to end.

This adapter lives in tests/ because it's a proof of concept, not a real
provider. Real adapters go in app/travel/adapters/.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.types import (
    BookingRequest,
    NormalizedFlight,
    NormalizedLodging,
    Priced,
    PriceStatus,
)
from app.shared.quota import QuotaExceeded
from app.travel.base import SearchQuery


class ToyFlightAdapter:
    """Toy adapter that returns canned flight data without hitting a provider.

    Satisfies ProviderAdapter structurally. Used to verify the Protocol
    end to end before any real adapter is written.
    """

    async def search(self, q: SearchQuery) -> list[NormalizedFlight] | QuotaExceeded:
        now = datetime.now(UTC)
        return [
            NormalizedFlight(
                carrier="ToyAir",
                flight_numbers=["TA123"],
                depart=now,
                arrive=now,
                stops=0,
                duration_minutes=120,
                price_usd=Decimal("299.00"),
                selected_flights_json={
                    "outbound": [
                        {
                            "flight_number": "TA123",
                            "departure_id": q.origin or "JFK",
                            "arrival_id": q.destination,
                            "date": str(q.departure_date or "2027-04-01"),
                        }
                    ]
                },
                booking_request=BookingRequest(
                    url="https://example.com/book/TA123",
                    post_data={"flight": "TA123", "passengers": 1},
                    vendor="toyair",
                ),
                observed_at=now,
            )
        ]

    async def resolve(self, ref: str) -> BookingRequest:
        return BookingRequest(
            url="https://example.com/book/TA123",
            post_data={"flight": "TA123", "passengers": 1},
            vendor="toyair",
        )

    async def reprice(self, ref: str) -> Priced:
        # Flights are sold as a trip total, and the booking-options response
        # carries fresh post_data alongside the price — so a real flight
        # adapter returns both here rather than reusing the payload captured
        # at item_pending.
        return Priced(
            ref=ref,
            status=PriceStatus.available,
            price_usd=Decimal("299.00"),
            price_unit="total",
            observed_at=datetime.now(UTC),
            booking_request=BookingRequest(
                url="https://example.com/book/TA123",
                post_data={"flight": "TA123", "passengers": 1},
                vendor="toyair",
            ),
        )


class ToyLodgingAdapter:
    """Toy adapter that returns canned lodging data."""

    async def search(self, q: SearchQuery) -> list[NormalizedLodging] | QuotaExceeded:
        now = datetime.now(UTC)
        return [
            NormalizedLodging(
                property_id="toy-hotel-1",
                name="Toy Hotel",
                rating=4.5,
                price_per_night_usd=Decimal("150.00"),
                booking_request=BookingRequest(
                    url="https://example.com/book/toy-hotel-1",
                    post_data={"property": "toy-hotel-1", "check_in": str(q.check_in)},
                    vendor="toyhotels",
                ),
                cancellation_terms="Free cancellation 48h before check-in",
                observed_at=now,
            )
        ]

    async def resolve(self, ref: str) -> BookingRequest:
        return BookingRequest(
            url="https://example.com/book/toy-hotel-1",
            post_data={"property": "toy-hotel-1"},
            vendor="toyhotels",
        )

    async def reprice(self, ref: str) -> Priced:
        # per_night, matching NormalizedLodging.price_per_night_usd — the
        # stored price this gets compared against. A stay total here would
        # read as a 4x move and mark the item stale on the first check.
        return Priced(
            ref=ref,
            status=PriceStatus.available,
            price_usd=Decimal("150.00"),
            price_unit="per_night",
            observed_at=datetime.now(UTC),
        )
