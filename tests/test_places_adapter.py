"""Tripwires for the Places adapter (lane A3, US-015).

Adapter normalizers only — run against cassettes, so they're free and
deterministic. No agent-behavior or ranking-quality assertions.

Every fixture record carries both `name` (the resource path `places/PLACE_ID`)
and `displayName` (the title), so a normalizer that reads the legacy field
fails here rather than in production with a resource path as a restaurant name.
"""

from __future__ import annotations

import pytest

import app.travel.adapters.places as places
from app.models.types import NormalizedPlace
from app.shared import cassettes
from app.shared.cache import TTLTier
from app.shared.config import settings
from app.shared.quota import QuotaExceeded, quota_counter
from app.travel.base import ProviderAdapter, SearchQuery
from app.travel.ports import GeocodePort

# Queries with a recorded cassette. Changing one needs a new cassette.
LISBON = SearchQuery(destination="Lisbon", query="restaurants", limit=10)

RAMIRO_ID = "ChIJc7cVJl40GQ0RA0RiA1YkFRk"
GHOST_ID = "ChIJnoGeometry00000000000000"
TABERNA_ID = "ChIJmY0jQ0k0GQ0R9EiVsFqTe1o"
KIOSK_ID = "ChIJ4wFreePlace000000000000000"


@pytest.fixture(autouse=True)
def _needs_redis(redis_available: bool) -> None:
    """Every test here goes through `get_or_fetch`, so every one needs Redis.

    Requesting the fixture is also what makes conftest purge `cache:` and
    `quota:` around each test — without it, one test's cached payload answers
    the next test's quota assertion.
    """


@pytest.fixture
def captured_requests(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record every outbound request without changing replay behaviour."""
    seen: list = []
    original = cassettes.CassetteTransport.handle_async_request

    async def spy(self, request):  # noqa: ANN001
        seen.append(request)
        return await original(self, request)

    monkeypatch.setattr(cassettes.CassetteTransport, "handle_async_request", spy)
    return seen


def _by_id(results: list[NormalizedPlace], place_id: str) -> NormalizedPlace | None:
    return next((p for p in results if p.google_place_id == place_id), None)


# ── Protocol conformance ────────────────────────────────────────────────────


def test_adapter_satisfies_both_protocols() -> None:
    adapter = places.PlacesAdapter()
    assert isinstance(adapter, ProviderAdapter)
    assert isinstance(adapter, GeocodePort)


# ── Normalization ───────────────────────────────────────────────────────────


async def test_search_maps_a_recorded_response_with_no_field_loss() -> None:
    results = await places.PlacesAdapter().search(LISBON)

    ramiro = _by_id(results, RAMIRO_ID)
    assert ramiro is not None
    assert ramiro.name == "Cervejaria Ramiro"  # displayName.text, not `name`
    assert ramiro.address.startswith("Av. Almirante Reis 1")
    assert (ramiro.lat, ramiro.lon) == (38.7223, -9.1355)
    assert ramiro.rating == 4.5
    assert ramiro.price_level == 2  # PRICE_LEVEL_MODERATE
    assert ramiro.phone == "218 851 024"  # nationalPhoneNumber
    assert ramiro.maps_url == "https://maps.google.com/?cid=1234567890"


async def test_resource_path_is_never_stored_as_id_or_name() -> None:
    """Places API (New) `name` holds `places/PLACE_ID`. Reading it as a title
    produces confidently wrong data rather than an error."""
    results = await places.PlacesAdapter().search(LISBON)
    assert results
    for place in results:
        assert not place.google_place_id.startswith("places/")
        assert not place.name.startswith("places/")


async def test_partial_record_normalizes_without_crashing() -> None:
    """Thin responses are normal — every field except id, name and geometry is
    optional."""
    taberna = _by_id(await places.PlacesAdapter().search(LISBON), TABERNA_ID)
    assert taberna is not None
    assert taberna.rating is None
    assert taberna.price_level is None
    assert taberna.phone is None
    assert taberna.maps_url.endswith(f"place_id:{TABERNA_ID}")  # fallback link


async def test_price_level_free_maps_to_zero_not_none() -> None:
    """PRICE_LEVEL_FREE is a real signal; conflating it with 'unknown' would
    turn a free viewpoint into an unpriced one."""
    kiosk = _by_id(await places.PlacesAdapter().search(LISBON), KIOSK_ID)
    assert kiosk is not None
    assert kiosk.price_level == 0


async def test_record_with_null_geometry_is_dropped() -> None:
    """An item with no coordinates never returns from a spatial query and rots
    in the backlog (AGENTS.md §6)."""
    results = await places.PlacesAdapter().search(LISBON)
    assert _by_id(results, GHOST_ID) is None
    assert len(results) == 4  # five in the cassette, one has no location


async def test_no_booking_request_is_produced() -> None:
    """Restaurants are reference tier — there is no availability source and no
    reservation surface (AGENTS.md §5)."""
    results = await places.PlacesAdapter().search(LISBON)
    assert results
    for place in results:
        assert not hasattr(place, "booking_request")


async def test_resolve_and_reprice_refuse() -> None:
    adapter = places.PlacesAdapter()
    with pytest.raises(NotImplementedError, match="reference tier"):
        await adapter.resolve(RAMIRO_ID)
    with pytest.raises(NotImplementedError, match="reference tier"):
        await adapter.reprice(RAMIRO_ID)


async def test_details_returns_a_normalized_place() -> None:
    place = await places.PlacesAdapter().details(RAMIRO_ID)
    assert isinstance(place, NormalizedPlace)
    assert place.google_place_id == RAMIRO_ID
    assert place.phone == "218 851 024"


# ── Field masks ─────────────────────────────────────────────────────────────


async def test_every_request_carries_a_spaceless_field_mask(
    captured_requests: list,
) -> None:
    """Omitting the mask errors rather than defaulting, and a space in the list
    breaks it. Search uses the `places.` prefix; Details uses bare names."""
    adapter = places.PlacesAdapter()
    await adapter.search(LISBON)
    await adapter.details(RAMIRO_ID)
    await adapter.geocode("Time Out Market", "Lisbon")

    assert len(captured_requests) == 3
    for request in captured_requests:
        mask = request.headers.get("X-Goog-FieldMask")
        assert mask
        assert " " not in mask

    search_mask, details_mask, _ = (r.headers["X-Goog-FieldMask"] for r in captured_requests)
    assert search_mask.startswith("places.")
    assert details_mask.startswith("id,")


# ── Credentials ─────────────────────────────────────────────────────────────


@pytest.mark.xfail(
    reason="trunk `_CREDENTIAL_NAMES` doesn't cover `X-Goog-Api-Key`; raised with S1.1. "
    "A record session would commit the Places key. Non-strict so it doesn't break "
    "other lanes when the trunk fix lands — delete the marker then.",
)
def test_places_api_key_header_is_redacted_before_writing() -> None:
    """Cassettes are committed, so a credential in one lands in git history.

    Redaction is by key name, and Google carries its key in a header name that
    no other provider in this stack uses.
    """
    assert cassettes._is_credential("X-Goog-Api-Key")


# ── Cache tiers ─────────────────────────────────────────────────────────────


async def test_search_uses_places_content_and_geocode_uses_geo_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Places content is capped at 30 days by contract; ids and coordinates are
    stable indefinitely. A tier is never defaulted."""
    tiers: list[TTLTier] = []
    original = places.get_or_fetch

    async def spy(key, tier, fetch):  # noqa: ANN001
        tiers.append(tier)
        return await original(key, tier, fetch)

    monkeypatch.setattr(places, "get_or_fetch", spy)

    adapter = places.PlacesAdapter()
    await adapter.search(LISBON)
    await adapter.details(RAMIRO_ID)
    await adapter.geocode("Time Out Market", "Lisbon")
    await adapter.reverse_geocode(38.7071, -9.1466)

    assert tiers == [
        TTLTier.PLACES_CONTENT,
        TTLTier.PLACES_CONTENT,
        TTLTier.GEO_STABLE,
        TTLTier.GEO_STABLE,
    ]


# ── Quota ───────────────────────────────────────────────────────────────────


async def _exhaust_turn_quota() -> None:
    for _ in range(settings.quota_calls_per_turn):
        await quota_counter.increment(places.PROVIDER)


async def test_quota_exceeded_is_returned_as_a_value_never_raised() -> None:
    await _exhaust_turn_quota()

    result = await places.PlacesAdapter().search(LISBON)

    assert isinstance(result, QuotaExceeded)
    assert result.provider == places.PROVIDER


async def test_cache_hit_does_not_spend_quota() -> None:
    """Quota is checked inside the fetch closure — a cached answer costs
    nothing, because it made no call. Counting reads instead of outbound calls
    would make the ceiling limit the wrong thing."""
    adapter = places.PlacesAdapter()
    await adapter.search(LISBON)
    turn_calls, _ = await quota_counter.peek(places.PROVIDER)
    assert turn_calls == 1

    await adapter.search(LISBON)

    assert await quota_counter.peek(places.PROVIDER) == (1, 1)


async def test_geocode_returns_empty_rather_than_blocking_on_quota() -> None:
    """Resolution never blocks; an unresolved item stays visible in the backlog
    (US-009)."""
    await _exhaust_turn_quota()
    assert await places.PlacesAdapter().geocode("Time Out Market", "Lisbon") == []


# ── Geocoding ───────────────────────────────────────────────────────────────


async def test_geocode_ranks_the_named_venue_first() -> None:
    results = await places.PlacesAdapter().geocode("Time Out Market", "Lisbon")

    assert [r.name for r in results][0] == "Time Out Market Lisboa"
    assert results[0].google_place_id == "ChIJVeIRDXk0GQ0RfHiI9PxlkVU"
    assert results[0].confidence > results[1].confidence
    assert results[0].lat == 38.7071


async def test_ambiguous_chain_match_is_not_confident() -> None:
    """A bare chain name resolves confidently and wrongly if nothing penalizes
    ambiguity — and a confident wrong answer never trips the confidence gate,
    which makes it worse than a clean miss."""
    unambiguous = await places.PlacesAdapter().geocode("Time Out Market", "Lisbon")
    ambiguous = await places.PlacesAdapter().geocode("Starbucks")

    assert len(ambiguous) == 3
    assert ambiguous[0].confidence < unambiguous[0].confidence
    assert ambiguous[0].confidence < 0.75


async def test_reverse_geocode_returns_the_nearest_place() -> None:
    result = await places.PlacesAdapter().reverse_geocode(38.7071, -9.1466)

    assert result is not None
    assert result.google_place_id == "ChIJVeIRDXk0GQ0RfHiI9PxlkVU"
    assert 0.0 < result.confidence <= 1.0
