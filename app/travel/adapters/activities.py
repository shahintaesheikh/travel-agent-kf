"""Activities adapter — Tripadvisor (attractions, experiences, POIs) and Google Events.

Lane A4 · US-016. Reference tier throughout: activities never carry a
`booking_request`, are never resolved, and are never repriced.

Two engines behind one adapter, because `search_activities` takes a single
`kind` and routes on it:

    attraction | experience | poi  →  SerpApi Tripadvisor   (ssrc="A")
    event                          →  SerpApi Google Events

Read `serpapi-tripadvisor` and `serpapi-google-events` before changing this
file. Three behaviours below look like defensive noise and are not:

  * Tripadvisor changes its top-level response key by query (`places` on some,
    `locations` on others), so a missing `places` is not an empty result.
  * Tripadvisor returns thinner records — `location_id` only — at high `limit`,
    with no error and no warning.
  * Google Events omits the year from every date, so a naive parse lands events
    in the current year and silently misfiles anything across a year boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, NoReturn

import structlog

from app.models.types import NormalizedActivity
from app.shared.cache import TTLTier, get_or_fetch
from app.shared.cassettes import make_client
from app.shared.quota import QuotaExceeded, quota_counter
from app.travel.base import SearchQuery

log = structlog.get_logger()

SERPAPI_URL = "https://serpapi.com/search"

# Cassette and quota namespaces, deliberately per-engine rather than "serpapi".
# Three lanes hit SerpApi; a shared namespace would collide their cassettes.
TRIPADVISOR_PROVIDER = "tripadvisor"
EVENTS_PROVIDER = "google_events"

TOOL = "search_activities"

ActivityKind = Literal["attraction", "experience", "event", "poi"]
PriceBasis = Literal["actual", "uncounted"]

# Higher values make Tripadvisor return records carrying only `location_id` and
# `location_type` instead of full details. Not an error — just thinner objects.
_TRIPADVISOR_LIMIT = 30

# Case matters: lowercase "a" is All, uppercase "A" is Things to Do. This is the
# easiest silent mistake in the engine. `v` (vacation rentals) is dead upstream
# and is never used.
_SSRC_THINGS_TO_DO = "A"

# `ATTRACTION` is a place — museum, beach, viewpoint. `ATTRACTION_PRODUCT` is
# Viator inventory: a sellable guided tour or class, linking to a specific
# AttractionProductReview page. Conflating them puts a beach in the itinerary as
# a bookable product, or drops a real tour into a list of landmarks.
_PLACE_TYPE_BY_KIND: dict[str, str] = {
    "attraction": "ATTRACTION",
    "poi": "ATTRACTION",
    "experience": "ATTRACTION_PRODUCT",
}

# Google Events pages in steps of 10 — unlike every other engine in this stack.
# The fan-out ceiling lives here, in the adapter, never in agent instructions.
_EVENT_PAGE_SIZE = 10
_MAX_EVENT_PAGES = 3

_MONTHS = {
    month: index
    for index, month in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}
_MONTH_DAY_RE = re.compile(r"([A-Za-z]{3,9})\s+(\d{1,2})")


# ── Helpers ─────────────────────────────────────────────────────────────────


def _api_key() -> str:
    """SerpApi key from the environment.

    Not read off `settings`: `app/shared/config.py` is trunk-owned (S1) and this
    lane does not edit it. Batched for the integrator — see the lane report. In
    replay mode, the default, the value never reaches a real host.
    """
    return os.environ.get("SERPAPI_API_KEY", "")


def _cache_key(provider: str, params: dict[str, Any]) -> str:
    """`{provider}:{tool}:{sha256(normalized_args)}` — US-017.

    The API key is excluded: it is a credential, not an argument, and including
    it would fragment the cache on every key rotation.
    """
    args = {k: v for k, v in params.items() if k != "api_key"}
    digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
    return f"{provider}:{TOOL}:{digest}"


def _stable_id(prefix: str, *parts: str | None) -> str:
    """Derive a deterministic id for records the provider gives no id for."""
    material = "|".join(part for part in parts if part)
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()[:16]}"


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# ── Tripadvisor ─────────────────────────────────────────────────────────────


def _tripadvisor_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull result records out of either top-level key.

    Some queries return `locations` instead of `places`, and those responses do
    not support `offset` pagination. A missing `places` key is not an empty
    result — it is the other shape.
    """
    for key in ("places", "locations"):
        value = payload.get(key)
        if isinstance(value, list):
            return [record for record in value if isinstance(record, dict)]
    return []


def normalize_tripadvisor(payload: dict[str, Any], kind: str) -> list[NormalizedActivity]:
    """Map a Tripadvisor search response to typed activities.

    Every result is `price_basis='uncounted'`: the engine returns no price, no
    price level and no availability. Nothing here infers one from a description
    — an invented number that looks authoritative is worse than an honest gap.
    """
    wanted = _PLACE_TYPE_BY_KIND[kind]
    activities: list[NormalizedActivity] = []
    thin = 0

    for record in _tripadvisor_records(payload):
        place_type = record.get("place_type") or record.get("location_type")
        if place_type != wanted:
            continue

        name = _text(record.get("title"))
        link = _text(record.get("link"))
        if not name or not link:
            # A record with an id but no title is the high-`limit` truncation
            # trap. Counted and dropped rather than emitted half-empty.
            thin += 1
            continue

        external_id = _text(record.get("location_id")) or _stable_id("ta", name, link)
        activities.append(
            NormalizedActivity(
                external_id=external_id,
                name=name,
                rating=_as_float(record.get("rating")),
                kind=kind,  # type: ignore[arg-type]
                link=link,
                price_basis="uncounted",
            )
        )

    if thin:
        log.warning(
            "tripadvisor_thin_records",
            provider=TRIPADVISOR_PROVIDER,
            tool=TOOL,
            dropped=thin,
            limit=_TRIPADVISOR_LIMIT,
        )
    return activities


# ── Google Events ───────────────────────────────────────────────────────────


def _parse_month_day(text: str) -> tuple[int, int] | None:
    """Read `"Aug 30"` into `(8, 30)`. Returns None when the shape is unfamiliar."""
    match = _MONTH_DAY_RE.search(text)
    if not match:
        return None
    month = _MONTHS.get(match.group(1)[:3].lower())
    if month is None:
        return None
    return month, int(match.group(2))


def _in_window(start_date_text: str, window: tuple[date, date] | None) -> bool:
    """Resolve a year-less event date against the trip range.

    `"Aug 30"` carries month and day only. Resolving against *today* is wrong for
    any trip planned across a year boundary — if a trip runs Dec 28 – Jan 4,
    "Jan 2" belongs to the following year. So rather than picking a year, this
    asks whether *any* year the trip spans places the event inside the range.

    An absent or unparseable date is kept: upstream shapes are not guaranteed,
    and a parse failure is not evidence that an event falls outside the trip.
    """
    if window is None or not start_date_text:
        return True

    month_day = _parse_month_day(start_date_text)
    if month_day is None:
        return True

    month, day = month_day
    start, end = window
    for year in range(start.year, end.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:  # Feb 29 in a non-leap year
            continue
        if start <= candidate <= end:
            return True
    return False


def _looks_usd(price_text: Any) -> bool:
    """Whether the display price is denominated in USD.

    `extracted_price` is a bare number carrying no currency. `"CA$120"` and
    `"€40"` both yield an `extracted_price`, and writing either into a USD budget
    line fabricates a price. Only an unambiguous USD marker counts.
    """
    text = _text(price_text)
    return text.startswith("$") or "USD" in text.upper()


def _event_price(record: dict[str, Any]) -> tuple[PriceBasis, Decimal | None]:
    """`price_basis` is a per-item determination for events, not a constant.

    Events are the one activity source that sometimes carries a real number.
    Present and unambiguously USD → `actual`, counted in the budget (FR-37).
    Anything else → `uncounted`.
    """
    extracted = record.get("extracted_price")
    if extracted is None or not _looks_usd(record.get("price")):
        return "uncounted", None
    try:
        return "actual", Decimal(str(extracted))
    except (InvalidOperation, ValueError):
        return "uncounted", None


def _event_link(record: dict[str, Any]) -> str:
    """Primary vendor link, falling back to the first `ticket_info` entry.

    `ticket_info` has disappeared in past upstream regressions, so it is optional
    on both sides of this fallback.
    """
    link = _text(record.get("link"))
    if link:
        return link
    for entry in record.get("ticket_info") or []:
        if isinstance(entry, dict):
            candidate = _text(entry.get("link"))
            if candidate:
                return candidate
    return ""


def normalize_events(
    payload: dict[str, Any], window: tuple[date, date] | None
) -> list[NormalizedActivity]:
    """Map a Google Events response to typed activities, scoped to the trip range.

    `description`, `venue` and `ticket_info` are all treated as optional — each
    has gone missing in an upstream regression, and a normalizer that requires
    `venue` crashes on data that is otherwise fine. `date.when` (`"Sat 6:00 PM"`)
    is display text and is never parsed.
    """
    activities: list[NormalizedActivity] = []

    for record in payload.get("events_results") or []:
        if not isinstance(record, dict):
            continue

        name = _text(record.get("title"))
        link = _event_link(record)
        if not name or not link:
            continue

        date_block = record.get("date") if isinstance(record.get("date"), dict) else {}
        start_date_text = _text(date_block.get("start_date"))
        if not _in_window(start_date_text, window):
            # An off-dates festival is noise on the canvas.
            continue

        basis, price_usd = _event_price(record)
        activities.append(
            NormalizedActivity(
                # Google Events carries no stable id, so one is derived from the
                # fields that identify a showing.
                external_id=_stable_id("evt", name, start_date_text, _text(record.get("venue"))),
                name=name,
                rating=None,  # events carry no rating
                kind="event",
                link=link,
                price_basis=basis,
                price_usd=price_usd,
            )
        )

    return activities


def _trip_window(q: SearchQuery) -> tuple[date, date] | None:
    """The trip's date range, used only to resolve the year events omit.

    `SearchQuery` is trunk-owned and carries no trip-range fields, so the flight
    and lodging date pairs stand in. With no range available, no date filtering
    happens at all — returning unfiltered events is better than inventing a year.
    """
    start = q.departure_date or q.check_in
    end = q.return_date or q.check_out or start
    if start is None or end is None or end < start:
        return None
    return start, end


# ── Adapter ─────────────────────────────────────────────────────────────────


class ActivitiesAdapter:
    """ProviderAdapter for attractions, experiences, POIs and events.

    Reference tier: `resolve()` and `reprice()` exist to satisfy the Protocol and
    raise. There is no transaction to resolve and no fare to reprice.
    """

    def __init__(self, trip_id: str | None = None) -> None:
        # SearchQuery is trunk-owned and carries no trip_id, so the per-trip-hour
        # ceiling is wired through the constructor instead of the query.
        self.trip_id = trip_id

    async def search(self, q: SearchQuery) -> list[NormalizedActivity] | QuotaExceeded:
        kind = (q.kind or "attraction").lower()
        if kind == "event":
            return await self._search_events(q)
        if kind not in _PLACE_TYPE_BY_KIND:
            raise ValueError(
                f"unknown activity kind {kind!r}; "
                f"expected one of attraction, experience, event, poi"
            )
        return await self._search_tripadvisor(q, kind)

    async def resolve(self, ref: str) -> NoReturn:
        raise NotImplementedError(
            "Activities are reference tier — there is no transaction to resolve (AGENTS.md §5)."
        )

    async def reprice(self, ref: str) -> NoReturn:
        # The contract returns `Priced`, whose `booking_request` carries a
        # currently-valid way to charge one selection — the point being that a
        # re-price refreshes the payload rather than reusing the copy captured
        # at `item_pending`. Activities never had a `booking_request` to go
        # stale, and carry no price, so there is nothing here to refresh.
        raise NotImplementedError(
            "Activities are reference tier — there is no price to re-check (AGENTS.md §5)."
        )

    # ── Tripadvisor path ────────────────────────────────────────────────────

    def tripadvisor_params(self, q: SearchQuery) -> dict[str, Any]:
        """Outbound Tripadvisor parameters. Public so tests can derive the
        cassette path without duplicating the request shape."""
        return {
            "engine": "tripadvisor_search",
            "api_key": _api_key(),
            "q": q.query or q.destination,
            "ssrc": _SSRC_THINGS_TO_DO,
            "limit": _TRIPADVISOR_LIMIT,
        }

    async def _search_tripadvisor(
        self, q: SearchQuery, kind: str
    ) -> list[NormalizedActivity] | QuotaExceeded:
        blocked = await quota_counter.check(TRIPADVISOR_PROVIDER, self.trip_id)
        if blocked is not None:
            return blocked

        params = self.tripadvisor_params(q)
        payload = await get_or_fetch(
            _cache_key(TRIPADVISOR_PROVIDER, params),
            TTLTier.ACTIVITY_SEARCH,
            lambda: self._get(TRIPADVISOR_PROVIDER, params),
        )
        # No `offset` paging: it is unsupported on `locations` responses, and one
        # call at limit=30 covers the fan-out this tier deserves.
        return normalize_tripadvisor(payload, kind)[: q.limit]

    # ── Events path ─────────────────────────────────────────────────────────

    def events_params(self, q: SearchQuery, start: int = 0) -> dict[str, Any]:
        """Outbound Google Events parameters. Public for the same reason as
        `tripadvisor_params`."""
        params: dict[str, Any] = {
            "engine": "google_events",
            "api_key": _api_key(),
            "q": q.query or f"events in {q.destination}",
        }
        if start:
            params["start"] = start
        return params

    async def _search_events(self, q: SearchQuery) -> list[NormalizedActivity] | QuotaExceeded:
        window = _trip_window(q)
        collected: list[NormalizedActivity] = []
        seen: set[str] = set()
        pages = min(_MAX_EVENT_PAGES, max(1, -(-q.limit // _EVENT_PAGE_SIZE)))

        for page in range(pages):
            blocked = await quota_counter.check(EVENTS_PROVIDER, self.trip_id)
            if blocked is not None:
                # Partial results beat discarding what the earlier pages returned.
                return collected[: q.limit] if collected else blocked

            params = self.events_params(q, start=page * _EVENT_PAGE_SIZE)
            payload = await get_or_fetch(
                _cache_key(EVENTS_PROVIDER, params),
                TTLTier.ACTIVITY_SEARCH,
                lambda p=params: self._get(EVENTS_PROVIDER, p),
            )

            for activity in normalize_events(payload, window):
                if activity.external_id not in seen:
                    seen.add(activity.external_id)
                    collected.append(activity)

            returned = len(payload.get("events_results") or [])
            if returned < _EVENT_PAGE_SIZE or len(collected) >= q.limit:
                break

        return collected[: q.limit]

    # ── Network boundary ────────────────────────────────────────────────────

    async def _get(self, provider: str, params: dict[str, Any]) -> dict[str, Any]:
        """The one outbound call. Quota is counted here, where the network is.

        Reached only on a cache miss, so cached answers cost no quota. The
        converse — a cached answer served while over quota — is not reachable:
        `get_or_fetch` exposes no peek, so the ceiling is checked before the
        lookup and errs toward refusing rather than toward spending.
        """
        await quota_counter.increment(provider, self.trip_id)
        started = time.perf_counter()

        async with make_client(provider) as client:
            response = await client.get(SERPAPI_URL, params=params)
            response.raise_for_status()
            payload = response.json()

        log.info(
            "provider_call",
            provider=provider,
            tool=TOOL,
            args_hash=_cache_key(provider, params).rsplit(":", 1)[-1][:16],
            cache_hit=False,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
        return payload
