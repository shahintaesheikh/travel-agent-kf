"""Travel adapter base — ProviderAdapter Protocol, SearchQuery, QuotaExceeded.

Every provider implements the ProviderAdapter Protocol. Adapters own
normalization, caching, and quota. Nothing above the adapter layer sees a
raw provider response.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from app.models.types import (
    BookingRequest,
    NormalizedActivity,
    NormalizedFlight,
    NormalizedLodging,
    NormalizedPlace,
    Priced,
)
from app.shared.quota import QuotaExceeded


class SearchQuery:
    """Normalized search query for any provider."""

    def __init__(
        self,
        *,
        origin: str | None = None,
        destination: str,
        departure_date: date | None = None,
        return_date: date | None = None,
        check_in: date | None = None,
        check_out: date | None = None,
        guests: int = 2,
        query: str | None = None,
        kind: str | None = None,
        limit: int = 10,
    ):
        self.origin = origin
        self.destination = destination
        self.departure_date = departure_date
        self.return_date = return_date
        self.check_in = check_in
        self.check_out = check_out
        self.guests = guests
        self.query = query
        self.kind = kind
        self.limit = limit


@runtime_checkable
class ProviderAdapter(Protocol):
    """Interface every provider adapter implements.

    Methods:
        search: Return normalized results for a query.
        resolve: Return a BookingRequest for a specific selection.
        reprice: Return current price for a previously selected option.
    """

    async def search(
        self, q: SearchQuery
    ) -> (
        list[NormalizedFlight]
        | list[NormalizedLodging]
        | list[NormalizedPlace]
        | list[NormalizedActivity]
        | QuotaExceeded
    ): ...

    async def resolve(self, ref: str) -> BookingRequest:
        """Return a bookable request for one specific selection.

        Called by the `resolve_booking_options` node on the transition into
        `item_pending` — never during exploration. Booking options are never
        cached.
        """
        ...

    async def reprice(self, ref: str) -> Priced:
        """Return the current price for a previously selected option.

        Called by the `reprice` node unconditionally before every handoff,
        regardless of cache or recency (US-029). Adapters must not consult the
        cache here — a cached fare is exactly the fiction this call exists to
        rule out.
        """
        ...
