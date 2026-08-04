"""Regenerate the synthetic google-places cassettes.

    GOOGLE_PLACES_API_KEY=unused uv run python \
        fixtures/cassettes/google-places/_generate_synthetic.py

Runs the real adapter in `record` mode with the network transport swapped for
canned responses, so filenames come out at the hash the transport actually
computes rather than from a duplicated hashing routine. Nothing here touches
the network.

This exists because live recording is billed and needs approval (AGENTS.md §0).
It is a stopgap: prefer `PROVIDER_MODE=record` against the live API once a
recording session is approved. See README.md in this directory.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.shared import cassettes
from app.shared.config import settings
from app.travel.base import SearchQuery

settings.provider_mode = "record"

import app.travel.adapters.places as places  # noqa: E402


def _place(pid: str, name: str, address: str, lat: float, lon: float, **extra: Any) -> dict:
    """One Places API (New) record.

    `name` is the resource path and `displayName` the title — both present in
    every fixture, so a normalizer that reads the legacy field fails in tests.
    """
    record: dict[str, Any] = {
        "name": f"places/{pid}",
        "id": pid,
        "displayName": {"text": name, "languageCode": "en"},
        "formattedAddress": address,
        "location": {"latitude": lat, "longitude": lon},
    }
    record.update(extra)
    return record


SEARCH_LISBON = {
    "places": [
        _place(
            "ChIJc7cVJl40GQ0RA0RiA1YkFRk",
            "Cervejaria Ramiro",
            "Av. Almirante Reis 1, 1150-007 Lisboa, Portugal",
            38.7223,
            -9.1355,
            rating=4.5,
            priceLevel="PRICE_LEVEL_MODERATE",
            nationalPhoneNumber="218 851 024",
            googleMapsUri="https://maps.google.com/?cid=1234567890",
        ),
        _place(
            "ChIJVeIRDXk0GQ0RfHiI9PxlkVU",
            "Time Out Market Lisboa",
            "Av. 24 de Julho 49, 1200-479 Lisboa, Portugal",
            38.7071,
            -9.1459,
            rating=4.4,
            priceLevel="PRICE_LEVEL_INEXPENSIVE",
            googleMapsUri="https://maps.google.com/?cid=2234567890",
        ),
        # Thin record: no rating, no priceLevel, no phone, no maps URI.
        # Partial responses are normal; the normalizer tolerates them.
        _place(
            "ChIJmY0jQ0k0GQ0R9EiVsFqTe1o",
            "Taberna Sem Nome",
            "R. dos Bacalhoeiros 12, 1100-068 Lisboa, Portugal",
            38.7095,
            -9.1338,
        ),
        # Null geometry: dropped, never saved. An item with no coordinates
        # never returns from a spatial query and rots in the backlog.
        {
            "name": "places/ChIJnoGeometry00000000000000",
            "id": "ChIJnoGeometry00000000000000",
            "displayName": {"text": "Ghost Kitchen", "languageCode": "en"},
            "formattedAddress": "Lisboa, Portugal",
            "rating": 3.9,
        },
        _place(
            "ChIJ4wFreePlace000000000000000",
            "Miradouro da Graca Kiosk",
            "Calcada da Graca, 1170-165 Lisboa, Portugal",
            38.7157,
            -9.1300,
            rating=4.6,
            priceLevel="PRICE_LEVEL_FREE",
            nationalPhoneNumber="218 873 000",
        ),
    ]
}

GEOCODE_WITH_LOCALITY = {
    "places": [
        _place(
            "ChIJVeIRDXk0GQ0RfHiI9PxlkVU",
            "Time Out Market Lisboa",
            "Av. 24 de Julho 49, 1200-479 Lisboa, Portugal",
            38.7071,
            -9.1459,
        ),
        _place(
            "ChIJOtherMarket0000000000000",
            "Mercado da Ribeira",
            "Av. 24 de Julho, 1200-479 Lisboa, Portugal",
            38.7069,
            -9.1462,
        ),
    ]
}

# The chain case: several equally good name matches and no locality to separate
# them. This is what the ambiguity penalty exists for.
GEOCODE_AMBIGUOUS = {
    "places": [
        _place(
            "ChIJChainA000000000000000000",
            "Starbucks",
            "R. Augusta 100, 1100-053 Lisboa, Portugal",
            38.7110,
            -9.1385,
        ),
        _place(
            "ChIJChainB000000000000000000",
            "Starbucks",
            "Av. da Liberdade 200, 1250-147 Lisboa, Portugal",
            38.7220,
            -9.1450,
        ),
        _place(
            "ChIJChainC000000000000000000",
            "Starbucks",
            "Aeroporto de Lisboa, 1700-008 Lisboa, Portugal",
            38.7742,
            -9.1342,
        ),
    ]
}

DETAILS_RAMIRO = _place(
    "ChIJc7cVJl40GQ0RA0RiA1YkFRk",
    "Cervejaria Ramiro",
    "Av. Almirante Reis 1, 1150-007 Lisboa, Portugal",
    38.7223,
    -9.1355,
    rating=4.5,
    priceLevel="PRICE_LEVEL_MODERATE",
    nationalPhoneNumber="218 851 024",
    googleMapsUri="https://maps.google.com/?cid=1234567890",
)

NEARBY = {
    "places": [
        _place(
            "ChIJVeIRDXk0GQ0RfHiI9PxlkVU",
            "Time Out Market Lisboa",
            "Av. 24 de Julho 49, 1200-479 Lisboa, Portugal",
            38.7071,
            -9.1459,
        )
    ]
}


def _canned(request: httpx.Request) -> dict:
    url = str(request.url)
    body = json.loads(request.content) if request.content else {}
    text_query = body.get("textQuery", "")

    if url.startswith(places._NEARBY_SEARCH_URL):
        return NEARBY
    if "places:searchText" in url:
        if text_query == "restaurants in Lisbon":
            return SEARCH_LISBON
        if text_query == "Time Out Market, Lisbon":
            return GEOCODE_WITH_LOCALITY
        if text_query == "Starbucks":
            return GEOCODE_AMBIGUOUS
        raise SystemExit(f"no canned response for textQuery={text_query!r}")
    if "/v1/places/" in url:
        return DETAILS_RAMIRO
    raise SystemExit(f"no canned response for {url}")


class _StubTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_canned(request), request=request)


# `CassetteTransport` builds the real transport itself, so the swap happens in
# the module that constructs it. Filenames then come out at the fingerprint the
# trunk transport actually computes.
cassettes.httpx.AsyncHTTPTransport = _StubTransport  # type: ignore[assignment]


async def main() -> None:
    adapter = places.PlacesAdapter()
    await adapter.search(SearchQuery(destination="Lisbon", query="restaurants", limit=10))
    await adapter.geocode("Time Out Market", "Lisbon")
    await adapter.geocode("Starbucks")
    await adapter.details("ChIJc7cVJl40GQ0RA0RiA1YkFRk")
    await adapter.reverse_geocode(38.7071, -9.1466)


if __name__ == "__main__":
    asyncio.run(main())
