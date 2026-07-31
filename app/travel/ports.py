"""GeocodePort — port for geocoding operations.

Used by the ingestion ladder and saved_item resolution. Implemented by the
Places adapter (A3 lane).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class GeocodeResult:
    """Result of a geocoding lookup."""

    def __init__(
        self,
        google_place_id: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        formatted_address: str | None = None,
        name: str | None = None,
        confidence: float = 0.0,
    ):
        self.google_place_id = google_place_id
        self.lat = lat
        self.lon = lon
        self.formatted_address = formatted_address
        self.name = name
        self.confidence = confidence


@runtime_checkable
class GeocodePort(Protocol):
    """Interface for geocoding. Implemented by the Places adapter (A3)."""

    async def geocode(
        self, query: str, locality_hint: str | None = None
    ) -> list[GeocodeResult]:
        """Resolve a text query to geocoded results.

        Args:
            query: Free-text place name or address.
            locality_hint: Optional city/region to bias results.

        Returns:
            List of geocoded results, ordered by confidence descending.
        """
        ...

    async def reverse_geocode(
        self, lat: float, lon: float
    ) -> GeocodeResult | None:
        """Resolve coordinates to a place.

        Args:
            lat: Latitude.
            lon: Longitude.

        Returns:
            GeocodeResult if a place is found, None otherwise.
        """
        ...
