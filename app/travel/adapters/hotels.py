"""Google Hotels lodging adapter — SerpApi `google_hotels` engine (US-013).

⚠️  DEEP-LINK PRECISION: this provider does not clear the bar.

Per-vendor entries in `properties[].prices[]` carry a source, a logo, a guest
count and a rate — and no URL. The only link on a property is
`properties[].link`, which points at the hotel's own marketing site, not at a
dated checkout for the requested stay. There is no equivalent of the flights
booking-options step.

`DEEP_LINK_PRECISION = False` below records that fact in code. Whether hotels
stay in the actionable tier or drop to reference is a product decision, not an
adapter decision — this module surfaces the gap and does not engineer around
it. `BookingRequest.url` is the property page and the user re-enters dates.

Three response traps this module handles (each produces wrong data silently
rather than erroring — see the `serpapi-google-hotels` skill):

1. `ads[]` is a separate array from `properties[]`, with a different shape and
   a different meaning. Never merged.
2. An exact-name `q` returns property details at the **top level** instead of a
   `properties` array. `search_information.hotels_results_state` is the signal.
3. `rate_per_night` and `total_rate` are both present and both nested. Only the
   `extracted_*` numeric fields are read; the string forms carry currency
   symbols.

**Price convention, chosen once and applied everywhere in this module:**
`lowest` (taxes and fees included), never `before_taxes_fees`. They differ by
roughly 20%, and mixing them across items makes a trip total meaningless.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog

from app.models.types import BookingRequest, NormalizedLodging, Priced, PriceStatus
from app.shared.cache import TTLTier, get_or_fetch
from app.shared.cassettes import make_client
from app.shared.config import settings
from app.shared.quota import QuotaExceeded, quota_counter
from app.travel.base import SearchQuery

log = structlog.get_logger()

PROVIDER = "serpapi_hotels"  # cassette namespace and quota bucket
ENDPOINT = "https://serpapi.com/search"

#: Google Hotels does not satisfy the project's deep-link precision requirement.
#: See the module docstring. Consumers that need a dated checkout must use the
#: Airbnb adapter; this constant exists so the gap is checkable, not folklore.
DEEP_LINK_PRECISION = False

#: Which nested price field is authoritative. Chosen once — see module docstring.
_PRICE_FIELD = "extracted_lowest"

#: Fan-out ceiling. Limits live in the adapter, never in agent instructions.
#: Roughly 18–20 properties per page, so two pages covers any sane top-N.
_MAX_PAGES = 2

#: In replay mode the key is a fixed sentinel so cassette hashes are identical
#: on every machine — the transport hashes the full URL, api_key included.
_REPLAY_KEY = "REPLAY"


# ── Structured errors ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class LodgingProviderError:
    """A lodging provider failure, returned as a value and never raised.

    Same contract as `QuotaExceeded`: the caller reasons about it instead of
    catching it. An omkar failure degrades the app to hotels-only; a SerpApi
    failure degrades it to Airbnb-only. No code path may assume either source
    produced results.
    """

    provider: str
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        return f"{self.provider} error [{self.code}]: {self.message}"


class _Uncacheable(Exception):  # noqa: N818 — control flow, not an error surface
    """Internal control flow that keeps failures out of the 30-minute cache.

    `get_or_fetch` caches whatever the fetch callable returns. A cached
    `QuotaExceeded` would be served for the whole TTL and the next turn would
    never retry; a cached transport error would outlive the outage that caused
    it. Raised inside the callable and caught immediately outside it — this
    never escapes the adapter, so failures still reach the caller as values.
    """

    def __init__(self, value: LodgingProviderError | QuotaExceeded) -> None:
        super().__init__(str(value))
        self.value = value


# ── Pure helpers (shared with the Airbnb adapter and the tests) ─────────────


def api_key() -> str:
    """SerpApi key, or a fixed sentinel under replay so cassettes are portable."""
    if settings.provider_mode == "replay":
        return _REPLAY_KEY
    return os.environ.get("SERPAPI_API_KEY", "")


def cache_key(provider: str, tool: str, args: dict[str, Any]) -> str:
    """`{provider}:{tool}:{sha256(normalized_args)}` — US-017 key format."""
    normalized = json.dumps(args, sort_keys=True, default=str)
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    return f"{provider}:{tool}:{digest}"


def nights_between(check_in: date, check_out: date) -> int:
    return (check_out - check_in).days


def validate_stay_dates(
    check_in: date | None,
    check_out: date | None,
    *,
    provider: str,
    reject_past: bool,
    today: date | None = None,
) -> LodgingProviderError | None:
    """Return a structured error if the stay dates can't produce a real search.

    `reject_past` exists because omkar answers a past `arrival_date` with
    `total_results: 0` and no error at all — a wasted call that looks like an
    empty city. Google Hotels has no such observed behaviour, so hotels pass
    past dates through to the provider rather than inventing a rule for it.
    """
    if check_in is None or check_out is None:
        return LodgingProviderError(
            provider,
            "missing_dates",
            "check_in and check_out are both required for a lodging search.",
            {"check_in": check_in, "check_out": check_out},
        )
    if nights_between(check_in, check_out) < 1:
        return LodgingProviderError(
            provider,
            "invalid_date_range",
            "check_out must be at least one night after check_in.",
            {"check_in": check_in, "check_out": check_out},
        )
    if reject_past and check_in < (today or datetime.now(UTC).date()):
        return LodgingProviderError(
            provider,
            "past_check_in",
            "check_in is in the past; the provider returns zero results with no error.",
            {"check_in": check_in},
        )
    return None


def to_number(value: Any) -> float | None:
    """Coerce a provider number, tolerating strings and rejecting junk.

    Never used on a currency string like `"$275"` — those are the display forms
    and the `extracted_*` sibling is the one to read.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _extracted(node: Any, key: str = _PRICE_FIELD) -> float | None:
    """Read a nested `extracted_*` price. `rate_per_night`/`total_rate` shape."""
    if not isinstance(node, dict):
        return None
    return to_number(node.get(key))


def money_usd(value: float) -> Decimal:
    return Decimal(str(round(value, 2)))


# ── Request building ────────────────────────────────────────────────────────


def build_search_params(q: SearchQuery, *, page_token: str | None = None) -> dict[str, Any]:
    """Params for one `google_hotels` search page.

    `check_in_date` and `check_out_date` are required by the engine — unlike
    most SerpApi engines, a call without them errors.
    """
    params: dict[str, Any] = {
        "engine": "google_hotels",
        "q": q.query or q.destination,
        "check_in_date": str(q.check_in),
        "check_out_date": str(q.check_out),
        "adults": q.guests,
        "currency": "USD",
        "api_key": api_key(),
    }
    if page_token:
        params["next_page_token"] = page_token
    return params


def build_property_params(
    property_token: str, check_in: date, check_out: date, adults: int
) -> dict[str, Any]:
    """Params for a property-details lookup — same engine, plus `property_token`."""
    return {
        "engine": "google_hotels",
        "property_token": property_token,
        "check_in_date": str(check_in),
        "check_out_date": str(check_out),
        "adults": adults,
        "currency": "USD",
        "api_key": api_key(),
    }


def make_ref(property_token: str, check_in: date, check_out: date, adults: int = 2) -> str:
    """Opaque handle for `resolve()` / `reprice()`.

    Produced and consumed only by this adapter. A property token alone is not
    enough — every downstream call needs the stay dates back.
    """
    return f"{property_token}|{check_in}|{check_out}|{adults}"


def parse_ref(ref: str) -> tuple[str, date, date, int]:
    token, check_in, check_out, adults = ref.split("|")
    return token, date.fromisoformat(check_in), date.fromisoformat(check_out), int(adults)


# ── Normalization ───────────────────────────────────────────────────────────


def _is_single_property(payload: dict[str, Any]) -> bool:
    """Trap 2: an exact-name query returns details at the top level.

    Parsers that assume `properties[]` get nothing and fail silently, so the
    state string is checked before iterating anything.
    """
    state = (payload.get("search_information") or {}).get("hotels_results_state") or ""
    if "property details" in state.lower():
        return True
    # Tolerant fallback: no array, but the top level looks like a property.
    return not payload.get("properties") and bool(payload.get("name"))


def _property_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every property in a response, with `ads[]` deliberately left out.

    Trap 1: sponsored results live in `ads[]` behind a Google `aclk` redirect
    and carry a different shape (`hotel_class` is an Integer there and a String
    in `properties`). Merging them is how type drift reaches the canvas.
    """
    if _is_single_property(payload):
        return [payload]
    props = payload.get("properties")
    return [p for p in props if isinstance(p, dict)] if isinstance(props, list) else []


def _per_night_usd(prop: dict[str, Any], nights: int) -> float | None:
    """Nightly rate, in preference order, always from an `extracted_*` field."""
    rate = _extracted(prop.get("rate_per_night"))
    if rate is not None:
        return rate
    total = _extracted(prop.get("total_rate"))
    if total is not None and nights > 0:
        return total / nights
    # Last resort: the cheapest per-vendor rate. Same nested shape, no URL.
    vendor_rates = [
        r
        for p in (prop.get("prices") or [])
        if isinstance(p, dict)
        for r in [_extracted(p.get("rate_per_night"))]
        if r is not None
    ]
    return min(vendor_rates) if vendor_rates else None


def _property_id(prop: dict[str, Any]) -> str | None:
    token = prop.get("property_token")
    if isinstance(token, str) and token:
        return token
    name = prop.get("name")
    if isinstance(name, str) and name:
        # Stable fallback so a tokenless property is still addressable.
        return "name:" + hashlib.sha256(name.encode()).hexdigest()[:16]
    return None


def _cancellation_terms(prop: dict[str, Any]) -> str | None:
    """Only what the provider actually states. Never inferred prose."""
    free = prop.get("free_cancellation")
    if free is True:
        until = prop.get("free_cancellation_until_date")
        return f"Free cancellation until {until}" if until else "Free cancellation"
    return None


def normalize_search(
    payload: dict[str, Any], *, nights: int, observed_at: datetime | None = None
) -> list[NormalizedLodging]:
    """Map a `google_hotels` response to typed output, dropping nothing silently.

    A property missing a price or a link is skipped and counted rather than
    given a fabricated one — an invented estimate is worse than an honest gap.
    """
    observed_at = observed_at or datetime.now(UTC)
    results: list[NormalizedLodging] = []
    skipped_no_price = 0
    skipped_no_link = 0

    for prop in _property_records(payload):
        property_id = _property_id(prop)
        name = prop.get("name")
        if not property_id or not isinstance(name, str) or not name:
            continue

        per_night = _per_night_usd(prop, nights)
        if per_night is None:
            skipped_no_price += 1
            continue

        link = prop.get("link")
        if not isinstance(link, str) or not link:
            skipped_no_link += 1
            continue

        rating = to_number(prop.get("overall_rating"))

        results.append(
            NormalizedLodging(
                property_id=property_id,
                name=name,
                # `overall_rating` arrives as a long float (4.4355693).
                rating=round(rating, 2) if rating is not None else None,
                price_per_night_usd=money_usd(per_night),
                booking_request=BookingRequest(
                    url=link,  # property page, not a dated checkout — see module docstring
                    post_data=None,
                    vendor="google_hotels",
                ),
                cancellation_terms=_cancellation_terms(prop),
                observed_at=observed_at,
            )
        )

    if skipped_no_price or skipped_no_link:
        log.warning(
            "hotels_properties_skipped",
            provider=PROVIDER,
            no_price=skipped_no_price,
            no_link=skipped_no_link,
            kept=len(results),
        )
    return results


# ── Adapter ─────────────────────────────────────────────────────────────────


class HotelsAdapter:
    """SerpApi Google Hotels. Satisfies `ProviderAdapter` structurally.

    Owns normalization, caching (`LODGING_SEARCH`, 30 min) and quota. Nothing
    above this layer sees a raw SerpApi response: raw payloads belong in
    Postgres, written by whichever lane persists the itinerary item.
    """

    provider = PROVIDER
    deep_link_precision = DEEP_LINK_PRECISION

    def __init__(self, trip_id: str | None = None) -> None:
        self.trip_id = trip_id

    # ── network ──────────────────────────────────────────────────────────
    async def _get(self, params: dict[str, Any]) -> dict[str, Any] | LodgingProviderError:
        """One SerpApi call. Every failure comes back as a value."""
        try:
            async with make_client(PROVIDER) as client:
                response = await client.get(ENDPOINT, params=params)
        except FileNotFoundError as exc:  # replay with no cassette recorded
            return LodgingProviderError(
                PROVIDER, "no_cassette", str(exc), {"mode": settings.provider_mode}
            )
        except httpx.HTTPError as exc:
            return LodgingProviderError(
                PROVIDER, "transport_error", str(exc), {"type": type(exc).__name__}
            )

        if response.status_code >= 400:
            return LodgingProviderError(
                PROVIDER,
                "http_error",
                f"SerpApi returned HTTP {response.status_code}.",
                {"status": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            return LodgingProviderError(PROVIDER, "bad_json", str(exc))

        if isinstance(payload, dict) and payload.get("error"):
            return LodgingProviderError(PROVIDER, "provider_error", str(payload["error"]))
        if not isinstance(payload, dict):
            return LodgingProviderError(PROVIDER, "bad_json", "Response was not an object.")
        return payload

    async def _spend_quota(self) -> QuotaExceeded | None:
        """Quota lives here, at the network boundary — never in a prompt.

        Async because the counter is Redis-backed and shared across processes:
        an un-awaited coroutine here would count nothing and return a truthy
        object, so every ceiling would read as exceeded while none was enforced.
        """
        exceeded = await quota_counter.check(PROVIDER, self.trip_id)
        if exceeded is not None:
            return exceeded
        await quota_counter.increment(PROVIDER, self.trip_id)
        return None

    # ── ProviderAdapter ──────────────────────────────────────────────────
    async def search(
        self, q: SearchQuery
    ) -> list[NormalizedLodging] | LodgingProviderError | QuotaExceeded:
        invalid = validate_stay_dates(q.check_in, q.check_out, provider=PROVIDER, reject_past=False)
        if invalid is not None:
            return invalid

        assert q.check_in is not None and q.check_out is not None  # narrowed above
        nights = nights_between(q.check_in, q.check_out)
        key = cache_key(
            PROVIDER,
            "search",
            {
                "q": q.query or q.destination,
                "check_in": q.check_in,
                "check_out": q.check_out,
                "adults": q.guests,
                "limit": q.limit,
            },
        )

        async def fetch() -> list[NormalizedLodging]:
            results: list[NormalizedLodging] = []
            page_token: str | None = None

            for page in range(_MAX_PAGES):
                exceeded = await self._spend_quota()
                if exceeded is not None:
                    # Partial results beat none; an empty page-one is the error.
                    if not results:
                        raise _Uncacheable(exceeded)
                    break

                payload = await self._get(build_search_params(q, page_token=page_token))
                if isinstance(payload, LodgingProviderError):
                    if not results:
                        raise _Uncacheable(payload)
                    break

                results.extend(normalize_search(payload, nights=nights))
                log.info(
                    "hotels_search_page",
                    provider=PROVIDER,
                    page=page + 1,
                    normalized=len(results),
                    deep_link_precision=DEEP_LINK_PRECISION,
                )
                if len(results) >= q.limit:
                    break
                page_token = (payload.get("serpapi_pagination") or {}).get("next_page_token")
                if not page_token:
                    break

            return results[: q.limit]

        # 30-minute LODGING_SEARCH tier. The tier is positional and required.
        try:
            return await get_or_fetch(key, TTLTier.LODGING_SEARCH, fetch)
        except _Uncacheable as escape:
            return escape.value  # a failure must not occupy the cache for 30 min

    async def resolve(self, ref: str) -> BookingRequest | LodgingProviderError | QuotaExceeded:
        """Best available booking target for one property.

        Still the property page — see the module docstring. This call confirms
        the link for the requested dates; it does not produce a dated checkout,
        because the engine has no such field. Booking targets are never cached.
        """
        token, check_in, check_out, adults = parse_ref(ref)
        exceeded = await self._spend_quota()
        if exceeded is not None:
            return exceeded

        payload = await self._get(build_property_params(token, check_in, check_out, adults))
        if isinstance(payload, LodgingProviderError):
            return payload

        link = payload.get("link") or (payload.get("properties") or [{}])[0].get("link")
        if not isinstance(link, str) or not link:
            return LodgingProviderError(
                PROVIDER, "no_booking_url", "Property carries no link.", {"ref": ref}
            )
        return BookingRequest(url=link, post_data=None, vendor="google_hotels")

    async def reprice(self, ref: str) -> Priced | LodgingProviderError | QuotaExceeded:
        """Current price for a previously selected property. Never cached.

        Runs unconditionally before every handoff — that is what stands between
        a stale number and a real charge.

        `Priced.price_usd` is **per night**, matching
        `NormalizedLodging.price_per_night_usd`. The money path compares this
        against the stored price to decide whether an item goes `stale`, and a
        stay total compared against a per-night figure would read as a ~N×
        price move on every single item. The stay total is logged, not
        returned — the frozen type carries one number.

        `booking_request` is always None here: the engine exposes no per-vendor
        booking URL, so there is nothing fresher than the property link already
        stored on the item. Callers fall back to that.
        """
        token, check_in, check_out, adults = parse_ref(ref)
        exceeded = await self._spend_quota()
        if exceeded is not None:
            return exceeded

        payload = await self._get(build_property_params(token, check_in, check_out, adults))
        if isinstance(payload, LodgingProviderError):
            # A provider outage is not "the listing is gone". Returning
            # `unavailable` here would let a transport blip cancel a stay.
            return payload

        nights = nights_between(check_in, check_out)
        record = (
            payload if _is_single_property(payload) else (_property_records(payload) or [{}])[0]
        )
        per_night = _per_night_usd(record, nights)
        if per_night is None:
            # Priced with no price: the property answered and quoted nothing
            # for these dates. `price_usd` stays None rather than reusing the
            # figure the user approved.
            return Priced(
                ref=ref,
                status=PriceStatus.unavailable,
                price_usd=None,
                observed_at=datetime.now(UTC),
                booking_request=None,
                unavailable_reason="Property quoted no price for these dates.",
            )

        total = _extracted(record.get("total_rate"))
        log.info(
            "hotels_repriced",
            provider=PROVIDER,
            nights=nights,
            per_night_usd=str(money_usd(per_night)),
            stay_total_usd=str(money_usd(total)) if total is not None else None,
        )

        return Priced(
            ref=ref,
            # The engine reports no availability signal at all, so a quote for
            # the requested dates is the only evidence available.
            status=PriceStatus.available,
            price_usd=money_usd(per_night),
            observed_at=datetime.now(UTC),
            booking_request=None,  # no per-vendor URL — see the docstring
        )
