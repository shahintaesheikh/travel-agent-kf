"""Google Places API (New) adapter — restaurants, POIs, and geocoding.

Implements `ProviderAdapter` (restaurant/POI search) and `GeocodePort`
(ingestion resolution). First-party Google API, **not** routed through SerpApi.

**Reference tier.** Places never produces a `BookingRequest`: there is no
availability source and no reservation surface in this project, so `resolve()`
and `reprice()` are unimplementable by design (AGENTS.md §5).

**Places API (New) renames invert the legacy meanings.** Most examples online
describe the legacy API, and code written from them fails silently rather than
loudly:

    legacy `name` (the title)   →  `displayName`
    new    `name`               →  the resource path `places/PLACE_ID`
    legacy `place_id`           →  `id`
    `formatted_phone_number`    →  `nationalPhoneNumber`
    `price_level` (int)         →  `priceLevel` (enum string)

`google_place_id` holds `id`. The resource path is never stored there.

**Field masks are mandatory** — there is no default field list, and omitting the
mask errors rather than defaulting. The mask also determines the billing SKU, so
each call site names its own mask constant to keep the tier legible at review.

**Cache tiers.** Places content (details, ratings, phone) is capped at 30 days by
contract — `PLACES_CONTENT`. IDs, coordinates and geocodes are stable and cache
indefinitely — `GEO_STABLE`. Neither is a tuning knob.
"""

from __future__ import annotations

import re
import time
from difflib import SequenceMatcher
from math import asin, cos, radians, sin, sqrt
from typing import Any, NoReturn

import structlog

from app.models.types import NormalizedPlace
from app.shared.cache import TTLTier, args_hash, cache_key, get_or_fetch
from app.shared.cassettes import make_client
from app.shared.config import settings
from app.shared.quota import QuotaExceeded, quota_counter
from app.travel.base import SearchQuery
from app.travel.ports import GeocodeResult

log = structlog.get_logger()

PROVIDER = "google-places"

_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"
_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
_MAPS_URL_FALLBACK = "https://www.google.com/maps/place/?q=place_id:{place_id}"

# ── Field masks ─────────────────────────────────────────────────────────────
# No spaces anywhere. `places.` prefix for Text Search and Nearby Search; bare
# field names for Place Details. Requesting one expensive field pulls the whole
# call into that billing tier, so these stay minimal and per-call-site.

SEARCH_FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedAddress,"
    "places.location,"
    "places.rating,"
    "places.priceLevel,"
    "places.nationalPhoneNumber,"
    "places.googleMapsUri,"
    "nextPageToken"
)

DETAILS_FIELD_MASK = (
    "id,displayName,formattedAddress,location,rating,priceLevel,nationalPhoneNumber,googleMapsUri"
)

# Geocoding wants identity and position only — no ratings, no phone. Keeping
# content out of this mask is what makes GEO_STABLE caching defensible.
GEOCODE_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.location"

NEARBY_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.location"

# Each page is a separate billed call. The fan-out ceiling lives here, in the
# adapter — never in agent instructions (AGENTS.md §6).
_MAX_PAGES = 3
_MAX_PAGE_SIZE = 20  # Places API (New) hard limit on `pageSize`

# `priceLevel` is the only price signal a restaurant carries. Derived amounts
# render as `price_basis='price_level_estimate'`, never as prices — mapping the
# enum back to the legacy integer keeps that arithmetic possible downstream.
_PRICE_LEVEL_TO_INT: dict[str, int | None] = {
    "PRICE_LEVEL_UNSPECIFIED": None,
    "PRICE_LEVEL_FREE": 0,
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}


# ── Quota signalling ────────────────────────────────────────────────────────


class _QuotaSignal(Exception):  # noqa: N818 — control flow, not an error condition
    """Private control-flow signal. Never escapes this module.

    Quota is checked inside the fetch closure so a cache hit doesn't spend
    budget it didn't need. Raising rather than returning is what stops
    `get_or_fetch` from caching a quota miss as though it were data — the cache
    only stores what `fetch()` returns. Callers of this module always see
    `QuotaExceeded` as a value (AGENTS.md §3).
    """

    def __init__(self, exceeded: QuotaExceeded) -> None:
        super().__init__(str(exceeded))
        self.exceeded = exceeded


# ── Request plumbing ────────────────────────────────────────────────────────


def _api_key() -> str:
    """The Places key, read at the call site and never earlier.

    `SecretStr` keeps it out of reprs, tracebacks and log lines. Replay needs
    no credential, which is why its absence is only fatal outside replay mode.
    """
    secret = settings.google_places_api_key
    if secret is None:
        if settings.provider_mode != "replay":
            raise RuntimeError(
                f"GOOGLE_PLACES_API_KEY is required when PROVIDER_MODE={settings.provider_mode}."
            )
        return ""
    return secret.get_secret_value()


def _headers(field_mask: str) -> dict[str, str]:
    return {
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": field_mask,
        "Content-Type": "application/json",
    }


async def _call(
    method: str,
    url: str,
    *,
    tool: str,
    field_mask: str,
    body: dict[str, Any] | None,
    trip_id: str | None,
) -> dict[str, Any]:
    """One billed Places call, quota-checked at the network boundary.

    Always invoked as the `fetch` closure of `get_or_fetch`, so it runs only on
    a cache miss — which is what keeps a cache hit from spending quota.

    Raises `_QuotaSignal` when the ceiling is hit — see that class for why the
    signal is an exception here and a value at the adapter surface.
    """
    blocked = await quota_counter.check(PROVIDER, trip_id)
    if blocked is not None:
        raise _QuotaSignal(blocked)

    started = time.perf_counter()
    async with make_client(PROVIDER) as client:
        response = await client.request(method, url, json=body, headers=_headers(field_mask))
        response.raise_for_status()
        payload = response.json()

    await quota_counter.increment(PROVIDER, trip_id)
    log.info(
        "provider_call",
        provider=PROVIDER,
        tool=tool,
        args_hash=args_hash({"url": url, "body": body or {}}),
        cache_hit=False,  # this function only runs on a cache miss
        latency_ms=round((time.perf_counter() - started) * 1000),
    )
    return payload


# ── Normalization ───────────────────────────────────────────────────────────


def _display_name(raw: dict[str, Any]) -> str | None:
    """`displayName` is `{"text": ..., "languageCode": ...}`, never a bare string.

    `raw["name"]` is the resource path `places/PLACE_ID` and is never a title.
    """
    display = raw.get("displayName")
    if isinstance(display, dict):
        text = display.get("text")
        return text if isinstance(text, str) and text else None
    if isinstance(display, str) and display:
        return display
    return None


def _price_level(raw: dict[str, Any]) -> int | None:
    value = raw.get("priceLevel")
    if isinstance(value, int):  # legacy shape, tolerated rather than trusted
        return value
    if isinstance(value, str):
        return _PRICE_LEVEL_TO_INT.get(value)
    return None


def normalize_place(raw: dict[str, Any]) -> NormalizedPlace | None:
    """Map one Places API (New) record to `NormalizedPlace`.

    Returns None for records missing an id, a display name, or coordinates.
    A place with null geometry never returns from a spatial query and rots in
    the backlog (AGENTS.md §6), so dropping it is the honest outcome. Every
    other field is optional and tolerated as missing — partial responses are
    normal.
    """
    place_id = raw.get("id")
    name = _display_name(raw)
    location = raw.get("location") or {}
    lat = location.get("latitude")
    lon = location.get("longitude")

    if not place_id or not name or lat is None or lon is None:
        return None

    return NormalizedPlace(
        google_place_id=place_id,
        name=name,
        address=raw.get("formattedAddress") or "",
        lat=float(lat),
        lon=float(lon),
        rating=raw.get("rating"),
        price_level=_price_level(raw),
        phone=raw.get("nationalPhoneNumber"),
        maps_url=raw.get("googleMapsUri") or _MAPS_URL_FALLBACK.format(place_id=place_id),
    )


def _normalize_many(payload: dict[str, Any]) -> list[NormalizedPlace]:
    places = payload.get("places") or []
    normalized = [normalize_place(raw) for raw in places]
    return [p for p in normalized if p is not None]


# ── Geocode confidence ──────────────────────────────────────────────────────


def _tokens(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _tokens(a), _tokens(b)).ratio()


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(h))


# Two candidates whose names score this close to each other are not
# distinguishable by name alone — the classic chain-restaurant case.
_AMBIGUITY_BAND = 0.08
_AMBIGUITY_PENALTY = 0.7


def _score_candidates(
    query: str, locality_hint: str | None, places: list[dict[str, Any]]
) -> list[GeocodeResult]:
    """Score geocode candidates, ordered by confidence descending.

    Confidence is a name-similarity score, lifted when the locality hint shows
    up in the formatted address and cut when two candidates are equally good.
    That last term is the point of the function: a venue name with no locality
    resolves *confidently and wrongly* for any chain, and a confident wrong
    answer never trips the confidence gate — which makes it worse than a clean
    miss. The gate's threshold is C1's to calibrate; this only has to make
    ambiguity visible.
    """
    scored: list[tuple[float, dict[str, Any], float]] = []
    for raw in places:
        name = _display_name(raw) or ""
        similarity = _similarity(query, name)
        address = raw.get("formattedAddress") or ""
        hint_match = bool(
            locality_hint and _tokens(locality_hint) and _tokens(locality_hint) in _tokens(address)
        )
        confidence = 0.15 + 0.65 * similarity + (0.20 if hint_match else 0.0)
        scored.append((min(confidence, 1.0), raw, similarity))

    if len(scored) > 1:
        best = max(s for _, _, s in scored)
        contenders = sum(1 for _, _, s in scored if s >= best - _AMBIGUITY_BAND)
        if contenders > 1:
            scored = [(c * _AMBIGUITY_PENALTY, raw, s) for c, raw, s in scored]

    results = [
        GeocodeResult(
            google_place_id=raw.get("id"),
            lat=(raw.get("location") or {}).get("latitude"),
            lon=(raw.get("location") or {}).get("longitude"),
            formatted_address=raw.get("formattedAddress"),
            name=_display_name(raw),
            confidence=round(confidence, 3),
        )
        for confidence, raw, _ in scored
    ]
    results.sort(key=lambda r: r.confidence, reverse=True)
    return results


# ── Adapter ─────────────────────────────────────────────────────────────────


class PlacesAdapter:
    """Places API (New) adapter. Satisfies `ProviderAdapter` and `GeocodePort`.

    `trip_id` scopes the per-trip-hour quota counter; it is optional so
    ingestion, which has no trip, can still geocode.
    """

    def __init__(self, *, trip_id: str | None = None) -> None:
        self.trip_id = trip_id

    # ── ProviderAdapter ─────────────────────────────────────────────────

    async def search(self, q: SearchQuery) -> list[NormalizedPlace] | QuotaExceeded:
        """Text Search for restaurants and POIs.

        Returns `QuotaExceeded` as a value when the ceiling is hit before any
        result was retrieved; if earlier pages already returned, those are
        returned instead — partial data beats no data, and the caller can't
        act on the difference.
        """
        subject = q.query or q.kind or "restaurants"
        text_query = f"{subject} in {q.destination}" if q.destination else subject
        limit = max(1, q.limit)

        collected: list[NormalizedPlace] = []
        page_token: str | None = None

        try:
            for _page in range(_MAX_PAGES):
                body: dict[str, Any] = {
                    "textQuery": text_query,
                    "pageSize": min(limit - len(collected), _MAX_PAGE_SIZE),
                }
                if page_token:
                    body["pageToken"] = page_token

                payload = await get_or_fetch(
                    cache_key(PROVIDER, "search_places", args_hash(body)),
                    TTLTier.PLACES_CONTENT,  # 30 days — contractual ceiling
                    lambda body=body: _call(
                        "POST",
                        _TEXT_SEARCH_URL,
                        tool="search_places",
                        field_mask=SEARCH_FIELD_MASK,
                        body=body,
                        trip_id=self.trip_id,
                    ),
                )

                collected.extend(_normalize_many(payload))
                page_token = payload.get("nextPageToken")
                if not page_token or len(collected) >= limit:
                    break
        except _QuotaSignal as signal:
            if not collected:
                return signal.exceeded

        return collected[:limit]

    async def resolve(self, ref: str) -> NoReturn:
        """Unimplementable by design — Places is reference tier.

        Restaurants and POIs have no availability source and no reservation
        surface in this project, so there is no `BookingRequest` to produce
        (AGENTS.md §5). Inventing one would be worse than this exception.
        """
        raise NotImplementedError(
            "Places is reference tier: no booking surface, no BookingRequest."
        )

    async def reprice(self, ref: str) -> NoReturn:
        """Unimplementable by design — see `resolve`. Nothing here is transacted.

        There is no honest `Priced` to return: a restaurant has no fare, and
        `priceLevel` is an estimate rather than a price. `PriceStatus` has no
        member for "was never transactable", and inventing `unavailable` would
        put a reference item on the money path.
        """
        raise NotImplementedError(
            "Places is reference tier: nothing to re-price, no transaction exists."
        )

    # ── Place details ───────────────────────────────────────────────────

    async def details(self, place_id: str) -> NormalizedPlace | None | QuotaExceeded:
        """Fetch one place's details. `PLACES_CONTENT` — 30-day ceiling."""
        url = _DETAILS_URL.format(place_id=place_id)
        try:
            payload = await get_or_fetch(
                cache_key(PROVIDER, "place_details", args_hash({"place_id": place_id})),
                TTLTier.PLACES_CONTENT,
                lambda: _call(
                    "GET",
                    url,
                    tool="place_details",
                    field_mask=DETAILS_FIELD_MASK,  # bare names — Details, not Search
                    body=None,
                    trip_id=self.trip_id,
                ),
            )
        except _QuotaSignal as signal:
            return signal.exceeded
        return normalize_place(payload)

    # ── GeocodePort ─────────────────────────────────────────────────────

    async def geocode(self, query: str, locality_hint: str | None = None) -> list[GeocodeResult]:
        """Resolve free text to geocoded candidates, best first.

        `locality_hint` carries ingestion's locality priors — active trip
        destinations, backlog clustering. Passing one is what keeps a bare
        venue name from resolving to the wrong branch of a chain.

        Returns `[]` when quota is exhausted. Resolution never blocks: an
        unresolved item stays visible in the backlog (US-009), which is a
        recoverable state, and the port's signature has no room for a
        `QuotaExceeded` value.
        """
        text_query = f"{query}, {locality_hint}" if locality_hint else query
        body: dict[str, Any] = {"textQuery": text_query, "pageSize": 5}

        try:
            payload = await get_or_fetch(
                cache_key(PROVIDER, "geocode", args_hash(body)),
                TTLTier.GEO_STABLE,  # ids, coordinates, geocodes — indefinite
                lambda: _call(
                    "POST",
                    _TEXT_SEARCH_URL,
                    tool="geocode",
                    field_mask=GEOCODE_FIELD_MASK,
                    body=body,
                    trip_id=self.trip_id,
                ),
            )
        except _QuotaSignal as signal:
            log.info("geocode_skipped", provider=PROVIDER, reason=str(signal.exceeded))
            return []

        return _score_candidates(query, locality_hint, payload.get("places") or [])

    async def reverse_geocode(self, lat: float, lon: float) -> GeocodeResult | None:
        """Nearest place to a coordinate.

        Places API (New) has no reverse-geocode endpoint — Nearby Search ranked
        by distance is the Places-native approximation, so confidence decays
        with the offset between the requested point and the returned one.
        """
        body: dict[str, Any] = {
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lon},
                    "radius": 50.0,
                }
            },
            "rankPreference": "DISTANCE",
            "maxResultCount": 1,
        }

        try:
            payload = await get_or_fetch(
                cache_key(PROVIDER, "reverse_geocode", args_hash(body)),
                TTLTier.GEO_STABLE,
                lambda: _call(
                    "POST",
                    _NEARBY_SEARCH_URL,
                    tool="reverse_geocode",
                    field_mask=NEARBY_FIELD_MASK,
                    body=body,
                    trip_id=self.trip_id,
                ),
            )
        except _QuotaSignal:
            return None

        places = payload.get("places") or []
        if not places:
            return None

        raw = places[0]
        location = raw.get("location") or {}
        got_lat = location.get("latitude")
        got_lon = location.get("longitude")
        if got_lat is None or got_lon is None:
            return None

        # Full confidence within 25m, decaying to 0.3 at the 200m edge.
        distance = _haversine_m(lat, lon, float(got_lat), float(got_lon))
        confidence = 1.0 if distance <= 25 else max(0.3, 1.0 - (distance - 25) / 250)

        return GeocodeResult(
            google_place_id=raw.get("id"),
            lat=float(got_lat),
            lon=float(got_lon),
            formatted_address=raw.get("formattedAddress"),
            name=_display_name(raw),
            confidence=round(confidence, 3),
        )
