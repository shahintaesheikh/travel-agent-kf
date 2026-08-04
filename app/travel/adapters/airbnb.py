"""Airbnb lodging adapter — omkar.cloud Airbnb Scraper API (US-014).

Airbnb is the only lodging source that clears deep-link precision: search
returns a `booking_url` with dates and guests already applied. That URL is the
entire reason this provider is in the stack. It exists **only on search** — a
details call cannot recover it — so it is captured during search and persisted.

The provider is a small vendor on a 100-queries/month free tier and every code
path here treats it as optional. Failures come back as `LodgingProviderError`
values, never exceptions, and the app degrades to hotels-only. Nothing may
assume Airbnb results exist.

Three documented traps, all handled below:

1. **`nightly_rate` is not a nightly rate.** It holds the discounted total for
   the stay. Mapping it to `price_per_night_usd` produces a figure N× too high
   for an N-night stay and looks entirely plausible on the canvas. Per-night is
   always derived: stay total ÷ nights.
2. **`booking_url` exists only on search** (details returns `listing_url`
   only). Captured at search; never reconstructed by hand.
3. **The two endpoints disagree field by field** — different names, different
   casing (`priceItems` is the API's only camelCase key), different types
   (`host_id` is an integer on search and a string on details). Two separate
   parsers, because one normalizer with optional fields silently produces
   partial rows for whichever endpoint it wasn't designed around.

Two observed behaviours from the S0.1 spike (PRD Appendix D) shape the rest:

- **Past `arrival_date` returns `total_results: 0` with no error.** Rejected
  before the call rather than spent against a 100/month quota.
- **The dated details endpoint returns HTTP 500** after ~61s. That is an
  expected failure, not an exception: catch it, retry undated, and mark
  availability unknown. Undated details works and undated pricing is base
  pricing — which is why `reprice()` reports `dates_applied`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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
from app.travel.adapters.hotels import (
    LodgingProviderError,
    _Uncacheable,
    cache_key,
    money_usd,
    nights_between,
    to_number,
    validate_stay_dates,
)
from app.travel.base import SearchQuery

log = structlog.get_logger()

PROVIDER = "omkar_airbnb"  # cassette namespace and quota bucket
BASE_URL = "https://airbnb-scraper-api.omkar.cloud"
SEARCH_PATH = "/airbnb/listings/search"
DETAILS_PATH = "/airbnb/listings/details"

#: Airbnb returns a dated `booking_url` — this source does clear the bar.
DEEP_LINK_PRECISION = True

_REPLAY_KEY = "REPLAY"


def api_key() -> str:
    """omkar key. Sent as the `API-Key` header — not Bearer, not X-API-Key."""
    if settings.provider_mode == "replay":
        return _REPLAY_KEY
    return os.environ.get("OMKAR_API_KEY", "")


# ── Details result (search has no availability signal at all) ───────────────


@dataclass(frozen=True)
class AirbnbAvailability:
    """What a details call adds on top of a search row.

    `is_available` is `None` when the dated call 500'd and the undated fallback
    answered instead: the listing exists, but nothing here speaks to the
    requested dates. That is genuinely unknown, and saying so beats guessing.
    """

    listing_id: str
    is_available: bool | None
    unavailability_reason: str | None
    cancellation_terms: str | None
    host_id: str | None
    stay_total_usd: Decimal | None
    per_night_usd: Decimal | None
    dates_applied: bool
    observed_at: datetime


# ── Pricing ─────────────────────────────────────────────────────────────────


def _stay_total_from_search(pricing: dict[str, Any], listing_id: str) -> float | None:
    """Stay total from a *search* row, in descending order of trustworthiness.

    `total_cost` is exact when present (null in the vendor's own sample and in
    the observed 2027 response). `nightly_rate` is the mislabeled stay total and
    already has discounts applied. Only then the breakdown.

    The breakdown is a list of signed-by-convention-not-by-type amounts: the
    vendor sample pairs `{"4 nights x $90.36": 361.43}` with
    `{"Early bird discount": 30.46}`, where the real total is the difference.
    Nothing but the human-readable `label` distinguishes a charge from a
    discount, and labels are display text whose format is not contractual — so
    only the first entry (the accommodation charge in every observed response)
    is read, and a multi-entry breakdown is logged rather than guessed at.
    """
    total = to_number(pricing.get("total_cost"))
    if total is not None:
        return total

    total = to_number(pricing.get("nightly_rate"))  # mislabeled: the stay total
    if total is not None:
        return total

    breakdown = pricing.get("cost_breakdown")
    if isinstance(breakdown, list) and breakdown:
        entries = [b for b in breakdown if isinstance(b, dict)]
        if not entries:
            return None
        if len(entries) > 1:
            log.warning(
                "airbnb_multi_entry_cost_breakdown",
                provider=PROVIDER,
                listing_id=listing_id,
                entries=len(entries),
                labels=[e.get("label") for e in entries],
                note="using first entry only; adjustments are not identifiable without parsing labels",
            )
        return to_number(entries[0].get("amount"))
    return None


def _per_night(stay_total: float | None, nights: int) -> Decimal | None:
    """Per-night is always derived. The provider never reports one numerically."""
    if stay_total is None or nights < 1:
        return None
    return money_usd(stay_total / nights)


# ── Search parser ───────────────────────────────────────────────────────────


def _listing_id(listing: dict[str, Any]) -> str | None:
    for key in ("listing_id", "id", "room_id"):
        value = listing.get(key)
        if value not in (None, ""):
            return str(value)  # integer on search, string on details
    return None


def _rating(listing: dict[str, Any]) -> float | None:
    value = listing.get("rating")
    if isinstance(value, dict):  # some responses nest it
        value = value.get("value") or value.get("average")
    rating = to_number(value)
    return round(rating, 2) if rating is not None else None


def normalize_search(
    payload: dict[str, Any], *, nights: int, observed_at: datetime | None = None
) -> list[NormalizedLodging]:
    """Map an omkar *search* response to typed output.

    Search-side parser only — details uses different names for the same
    concepts and must never be routed through here.
    """
    observed_at = observed_at or datetime.now(UTC)
    listings = payload.get("listings")
    if not isinstance(listings, list):
        return []

    results: list[NormalizedLodging] = []
    skipped_no_price = 0
    undated_urls = 0

    for listing in listings:
        if not isinstance(listing, dict):
            continue
        listing_id = _listing_id(listing)
        name = listing.get("name") or listing.get("title")
        if not listing_id or not isinstance(name, str) or not name:
            continue

        pricing = listing.get("pricing") if isinstance(listing.get("pricing"), dict) else {}
        per_night = _per_night(_stay_total_from_search(pricing, listing_id), nights)
        if per_night is None:
            skipped_no_price += 1
            continue

        # Trap 2: only search carries this. Persist it — details cannot recover it.
        booking_url = listing.get("booking_url") or listing.get("listing_url")
        if not isinstance(booking_url, str) or not booking_url:
            continue
        if "check_in=&" in booking_url or booking_url.endswith("check_in="):
            # Dates were sent but did not land. Surfaced, not silently dropped.
            undated_urls += 1

        policy = listing.get("cancellation_policy")

        results.append(
            NormalizedLodging(
                property_id=listing_id,
                name=name,
                rating=_rating(listing),
                price_per_night_usd=per_night,
                booking_request=BookingRequest(url=booking_url, post_data=None, vendor="airbnb"),
                # Search gives an enum string; details gives prose. One field,
                # two sources, neither embellished.
                cancellation_terms=policy if isinstance(policy, str) and policy else None,
                observed_at=observed_at,
            )
        )

    if skipped_no_price or undated_urls:
        log.warning(
            "airbnb_listings_flagged",
            provider=PROVIDER,
            skipped_no_price=skipped_no_price,
            booking_urls_without_dates=undated_urls,
            kept=len(results),
        )
    return results


# ── Details parser (separate on purpose — see module docstring) ─────────────


def parse_details(
    payload: dict[str, Any],
    *,
    listing_id: str,
    nights: int,
    dates_applied: bool,
    observed_at: datetime | None = None,
) -> AirbnbAvailability:
    """Map an omkar *details* response. Never used on a search response."""
    observed_at = observed_at or datetime.now(UTC)
    listing = payload.get("listing") if isinstance(payload.get("listing"), dict) else payload
    pricing = listing.get("pricing") if isinstance(listing.get("pricing"), dict) else {}

    # `total` is exact; `rate` is the same figure rounded. `qualifier`
    # ("For 4 nights") is what says which period either one covers.
    stay_total = to_number(pricing.get("total"))
    if stay_total is None:
        stay_total = to_number(pricing.get("rate"))

    terms = listing.get("cancellation_terms")
    if isinstance(terms, list):
        prose = " · ".join(str(t) for t in terms if t)
    elif isinstance(terms, str):
        prose = terms
    else:
        policy = listing.get("cancellation_policy")
        prose = policy if isinstance(policy, str) else ""

    is_available = listing.get("is_available")
    host_id = listing.get("host_id")

    return AirbnbAvailability(
        listing_id=listing_id,
        # Unknown when the undated fallback answered — it says nothing about
        # the requested dates.
        is_available=bool(is_available) if dates_applied and is_available is not None else None,
        unavailability_reason=listing.get("unavailability_reason") or None,
        cancellation_terms=prose or None,
        host_id=str(host_id) if host_id not in (None, "") else None,  # int here, str there
        stay_total_usd=money_usd(stay_total) if stay_total is not None else None,
        per_night_usd=_per_night(stay_total, nights) if dates_applied else None,
        dates_applied=dates_applied,
        observed_at=observed_at,
    )


# ── Request building ────────────────────────────────────────────────────────


def build_search_params(q: SearchQuery, *, page_number: int = 1) -> dict[str, Any]:
    """Search params. Dates are optional to the API and mandatory to us —
    without them `booking_url` comes back with empty date parameters."""
    return {
        "destination_query": q.query or q.destination,
        "arrival_date": str(q.check_in),
        "departure_date": str(q.check_out),
        "adult_guests": q.guests,
        "page_number": page_number,
    }


def build_details_params(
    stay_id: str,
    check_in: date | None,
    check_out: date | None,
    adults: int,
) -> dict[str, Any]:
    """Details params. Dates omitted entirely on the undated fallback path."""
    params: dict[str, Any] = {"stay_id": stay_id, "adult_guests": adults, "currency_code": "USD"}
    if check_in and check_out:
        params["arrival_date"] = str(check_in)
        params["departure_date"] = str(check_out)
    return params


def make_ref(listing_id: str, check_in: date, check_out: date, adults: int = 2) -> str:
    """Opaque handle for `availability()` / `reprice()`, produced by this adapter."""
    return f"{listing_id}|{check_in}|{check_out}|{adults}"


def parse_ref(ref: str) -> tuple[str, date, date, int]:
    listing_id, check_in, check_out, adults = ref.split("|")
    return listing_id, date.fromisoformat(check_in), date.fromisoformat(check_out), int(adults)


# ── Adapter ─────────────────────────────────────────────────────────────────


class AirbnbAdapter:
    """omkar Airbnb Scraper API. Satisfies `ProviderAdapter` structurally.

    Search during exploration; details **only** for a candidate entering
    `item_pending`. Two calls per candidate against a 100/month tier is how the
    quota disappears in an afternoon, so nothing here fetches details for a
    page of results to build a comparison table.
    """

    provider = PROVIDER
    deep_link_precision = DEEP_LINK_PRECISION

    def __init__(self, trip_id: str | None = None) -> None:
        self.trip_id = trip_id

    # ── network ──────────────────────────────────────────────────────────
    async def _get(
        self, path: str, params: dict[str, Any]
    ) -> dict[str, Any] | LodgingProviderError:
        """One omkar call. Every failure — including the known 500 — is a value."""
        try:
            async with make_client(PROVIDER) as client:
                response = await client.get(
                    BASE_URL + path, params=params, headers={"API-Key": api_key()}
                )
        except FileNotFoundError as exc:  # replay with no cassette recorded
            return LodgingProviderError(
                PROVIDER, "no_cassette", str(exc), {"mode": settings.provider_mode}
            )
        except httpx.HTTPError as exc:
            # Includes the ~61s timeout the dated details path exhibits.
            return LodgingProviderError(
                PROVIDER, "transport_error", str(exc), {"type": type(exc).__name__, "path": path}
            )

        if response.status_code >= 400:
            body: Any = None
            try:
                body = response.json()
            except ValueError:
                body = response.text[:200]
            return LodgingProviderError(
                PROVIDER,
                "http_error",
                f"omkar returned HTTP {response.status_code}.",
                {"status": response.status_code, "path": path, "body": body},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            return LodgingProviderError(PROVIDER, "bad_json", str(exc), {"path": path})
        if not isinstance(payload, dict):
            return LodgingProviderError(
                PROVIDER, "bad_json", "Response was not an object.", {"path": path}
            )
        return payload

    async def _spend_quota(self) -> QuotaExceeded | None:
        """Async because the counter is Redis-backed and shared across
        processes. An un-awaited coroutine would count nothing and still be
        truthy, so every ceiling would read as exceeded while none was
        enforced — on a 100-queries/month tier that failure is expensive in
        both directions."""
        exceeded = await quota_counter.check(PROVIDER, self.trip_id)
        if exceeded is not None:
            return exceeded
        await quota_counter.increment(PROVIDER, self.trip_id)
        return None

    # ── ProviderAdapter ──────────────────────────────────────────────────
    async def search(
        self, q: SearchQuery
    ) -> list[NormalizedLodging] | LodgingProviderError | QuotaExceeded:
        invalid = validate_stay_dates(q.check_in, q.check_out, provider=PROVIDER, reject_past=True)
        if invalid is not None:
            # Notably `past_check_in`: the provider answers those with
            # `total_results: 0` and no error, so the call is never made.
            log.info("airbnb_search_rejected", provider=PROVIDER, code=invalid.code)
            return invalid

        assert q.check_in is not None and q.check_out is not None  # narrowed above
        nights = nights_between(q.check_in, q.check_out)
        key = cache_key(
            PROVIDER,
            "search",
            {
                "destination": q.query or q.destination,
                "check_in": q.check_in,
                "check_out": q.check_out,
                "adults": q.guests,
                "limit": q.limit,
            },
        )

        async def fetch() -> list[NormalizedLodging]:
            exceeded = await self._spend_quota()
            if exceeded is not None:
                raise _Uncacheable(exceeded)

            payload = await self._get(SEARCH_PATH, build_search_params(q))
            if isinstance(payload, LodgingProviderError):
                # Structured, so the caller degrades to hotels-only. Not cached:
                # a 30-minute-old outage would otherwise outlive itself.
                log.warning("airbnb_search_failed", provider=PROVIDER, code=payload.code)
                raise _Uncacheable(payload)

            results = normalize_search(payload, nights=nights)
            log.info(
                "airbnb_search",
                provider=PROVIDER,
                total_results=payload.get("total_results"),
                normalized=len(results),
            )
            return results[: q.limit]

        # 30-minute LODGING_SEARCH tier. The tier is positional and required.
        try:
            return await get_or_fetch(key, TTLTier.LODGING_SEARCH, fetch)
        except _Uncacheable as escape:
            return escape.value  # a failure must not occupy the cache for 30 min

    async def availability(
        self, ref: str
    ) -> AirbnbAvailability | LodgingProviderError | QuotaExceeded:
        """Details lookup for one candidate. Fires only at `item_pending`.

        Tries the dated call, which is known to 500. On failure it falls back
        to the undated call and the result carries `dates_applied=False` and
        `is_available=None` — availability for the requested dates is unknown,
        and callers must not read the fallback as a yes.

        Not cached: this is a resolution step, consumed immediately.
        """
        listing_id, check_in, check_out, adults = parse_ref(ref)
        nights = nights_between(check_in, check_out)

        exceeded = await self._spend_quota()
        if exceeded is not None:
            return exceeded

        payload = await self._get(
            DETAILS_PATH, build_details_params(listing_id, check_in, check_out, adults)
        )
        if not isinstance(payload, LodgingProviderError):
            return parse_details(payload, listing_id=listing_id, nights=nights, dates_applied=True)

        # Expected failure, not an exception. Appendix D: HTTP 500 after ~61s.
        log.info(
            "airbnb_dated_details_failed",
            provider=PROVIDER,
            listing_id=listing_id,
            code=payload.code,
            expected=True,
        )

        exceeded = await self._spend_quota()
        if exceeded is not None:
            return exceeded

        undated = await self._get(
            DETAILS_PATH, build_details_params(listing_id, None, None, adults)
        )
        if isinstance(undated, LodgingProviderError):
            return undated
        return parse_details(undated, listing_id=listing_id, nights=nights, dates_applied=False)

    async def resolve(self, ref: str) -> BookingRequest | LodgingProviderError:
        """Airbnb booking targets are resolved at search time, not here.

        `booking_url` is returned only by search and no endpoint gives it back,
        so the dated URL captured during search and persisted on the itinerary
        item *is* the resolution. Reconstructing the query string by hand would
        couple this adapter to Airbnb's URL format. This method exists to
        satisfy the Protocol and says so structurally rather than inventing a
        link that might land on the wrong page.
        """
        listing_id, _, _, _ = parse_ref(ref)
        return LodgingProviderError(
            PROVIDER,
            "use_persisted_booking_request",
            "Airbnb booking_url is captured at search and persisted; details cannot return it.",
            {"listing_id": listing_id},
        )

    async def reprice(self, ref: str) -> Priced | LodgingProviderError | QuotaExceeded:
        """Current price for a previously selected listing. Never cached.

        `Priced.price_usd` is **per night**, matching
        `NormalizedLodging.price_per_night_usd` — the same convention the
        hotels adapter uses, so the money path compares like with like.

        ⚠️ When the dated details call 500s, the undated fallback returns *base*
        pricing, which is not the price of this stay. There is no honest
        `Priced` for that: `available` would publish a number for the wrong
        dates and `unavailable` would cancel a stay that is probably fine. It
        comes back as a `LodgingProviderError` — a re-price that did not
        happen, which is what the caller needs to know.

        `booking_request` is always None: `booking_url` is a dated GET link
        returned only by search, so the copy persisted at search time is
        already the valid one. Callers fall back to it.
        """
        result = await self.availability(ref)
        if isinstance(result, (LodgingProviderError, QuotaExceeded)):
            return result

        if not result.dates_applied:
            log.warning(
                "airbnb_reprice_undated",
                provider=PROVIDER,
                listing_id=result.listing_id,
                note="undated fallback pricing — not valid for the requested stay",
            )
            return LodgingProviderError(
                PROVIDER,
                "reprice_dates_not_applied",
                "Dated details call failed; undated base pricing is not this stay's price.",
                {"ref": ref, "listing_id": result.listing_id},
            )

        if result.is_available is False:
            return Priced(
                ref=ref,
                status=PriceStatus.unavailable,
                price_usd=None,  # gone — never the price the user approved
                observed_at=result.observed_at,
                booking_request=None,
                unavailable_reason=result.unavailability_reason
                or "Listing is not available for these dates.",
            )

        if result.per_night_usd is None:
            return Priced(
                ref=ref,
                status=PriceStatus.unavailable,
                price_usd=None,
                observed_at=result.observed_at,
                booking_request=None,
                unavailable_reason="Listing quoted no price for these dates.",
            )

        # `is_available` is True, or absent from a response that priced these
        # dates anyway. Either way the listing quoted this stay, and the link
        # is a GET — the user confirms on Airbnb before anything is charged.
        return Priced(
            ref=ref,
            status=PriceStatus.available,
            price_usd=result.per_night_usd,
            observed_at=result.observed_at,
            booking_request=None,  # search-time dated URL is the valid one
        )
