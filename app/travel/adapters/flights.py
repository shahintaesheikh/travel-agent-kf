"""SerpApi Google Flights adapter — lane A1, US-012.

Reference: the `serpapi-google-flights` skill and PRD Appendix D (S0.1 spike).

Four things about this adapter are non-obvious and deliberate:

1. **A round-trip `search()` returns candidates with both legs already
   pinned**, at the cost of one `departure_token` call per candidate. Google
   Flights does not return both directions in one response, so the return half
   is billed however you slice it — fetching it here means the ref handed back
   is directly resolvable. An outbound-only pin is priced as a one-way at
   roughly half the fare, and every field in that response looks right.

2. **`selected_flights_json`, never `booking_token`.** The token is opaque and
   expires; re-price weeks later fails silently and you cannot inspect it.
   Segments (flight number + airports + date) are durable and replay forever.

3. **Flight numbers arrive with a space** (`"LX 243"`). SerpApi accepts them
   only stripped (`"LX243"`) when echoed back in `selected_flights_json`.
   Appendix D confirmed this on both spike routes.

4. **`post_data` is an opaque URL-encoded string**, but the frozen
   `BookingRequest.post_data` is `dict | None`. It is carried verbatim as
   `{"_raw": "<string>"}` — see `POST_DATA_ENVELOPE_KEY`. The handoff endpoint
   POSTs `post_data["_raw"]` as the raw body, unchanged.

Booking options are resolved only at `item_pending` and are never cached.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from app.models.types import BookingRequest, NormalizedFlight, Priced, PriceStatus
from app.shared.cache import TTLTier, args_hash, cache_key, get_or_fetch
from app.shared.cassettes import make_client
from app.shared.config import settings
from app.shared.quota import QuotaExceeded, quota_counter
from app.travel.base import SearchQuery

# ── Constants ───────────────────────────────────────────────────────────────

SERPAPI_ENDPOINT = "https://serpapi.com/search"
ENGINE = "google_flights"

#: Cassette namespace. Google Flights only — A2 (hotels) and A4 (activities)
#: also go through SerpApi but own their own directories, so lanes never
#: collide inside one namespace at merge time.
PROVIDER = "serpapi_google_flights"

#: `post_data` from SerpApi is an opaque URL-encoded form body, but the frozen
#: `BookingRequest.post_data` is typed `dict | None`. The string is carried
#: under this key, byte-for-byte. Consumers POST it as the raw request body —
#: never re-encode, reorder or prune it.
POST_DATA_ENVELOPE_KEY = "_raw"

#: SerpApi requires the key as a query parameter. Read from `settings` as a
#: `SecretStr` and unwrapped at the call site, so it never renders in a repr,
#: traceback or log line. Never needed in replay mode.
_API_KEY_SETTING = "serpapi_api_key"

#: Trip type codes accepted by the engine.
TYPE_ROUND_TRIP = 1
TYPE_ONE_WAY = 2

#: How many outbound candidates get their return options fetched.
#:
#: Google Flights splits a round trip across two responses: the first lists
#: outbound legs, and `departure_token` asks for the returns that pair with one
#: of them. There is no shape in which the return half arrives free — it costs
#: one call per outbound you want to complete, so a round-trip search costs
#: `1 + MAX_RETURN_FETCHES` against the 6-per-turn ceiling.
#:
#: The limit lives here rather than in agent instructions (AGENTS.md §6): an
#: agent that can choose its own fan-out eventually chooses an expensive one.
MAX_RETURN_FETCHES = 3


class _QuotaRefusal(Exception):
    """Private control flow: unwind out of `get_or_fetch` without caching.

    Never escapes the adapter. `QuotaExceeded` is always returned as a value
    (AGENTS.md §3); this exists only because the cache API has no
    "fetched, but don't store" path.
    """

    def __init__(self, value: QuotaExceeded) -> None:
        super().__init__(str(value))
        self.value = value


class IncompleteItineraryError(ValueError):
    """Raised when `resolve()` is handed a ref that is not a full itinerary.

    A round trip needs both legs pinned. Given an outbound-only ref, Google
    Flights does not complain — it returns booking options for the outbound
    alone, at roughly half the round-trip fare, and every field is well-formed.
    Refusing before the call is made beats handing the money path something
    that looks right.
    """


class NoBookingOptionsError(RuntimeError):
    """The itinerary is complete but nothing is sellable through it.

    Distinct from `IncompleteItineraryError`: that one is a caller bug, this
    one is the world. Carries the escape hatch so the caller can offer the
    live Google Flights page instead of a dead end.
    """

    def __init__(self, escape_hatch: str) -> None:
        super().__init__(
            f"No booking options returned for this itinerary. Escape hatch: {escape_hatch}"
        )
        self.escape_hatch_url = escape_hatch


class PartialItineraryCoverageError(RuntimeError):
    """Booking options came back, but they don't sell the whole pinned trip.

    The failure this exists to prevent: a round-trip pin whose options price
    the outbound only. Observed on JFK→LAX — `marketed_as` listed the two
    outbound segments and the price was ~$170 against a $344 round-trip best.
    Nothing about that response is malformed, so nothing else catches it, and
    a handoff would land on a one-way checkout at half the expected fare.

    Re-price does not save you: it re-resolves the same pin and cheerfully
    agrees with itself. The coverage check is the only thing standing here.
    """

    def __init__(self, pinned: set[str], covered: set[str], escape_hatch: str) -> None:
        missing = sorted(pinned - covered)
        super().__init__(
            f"Booking options cover only {sorted(covered)} of the pinned itinerary "
            f"{sorted(pinned)} — missing {missing}. Refusing to hand off a partial "
            f"purchase. Escape hatch: {escape_hatch}"
        )
        self.pinned = pinned
        self.covered = covered
        self.missing = set(missing)
        self.escape_hatch_url = escape_hatch


# ── Search context — everything the agent must never see ────────────────────


@dataclass(frozen=True)
class FlightSearchContext:
    """Per-search side channel.

    `NormalizedFlight` is a frozen type and cannot carry these, and the raw
    payload must not reach the model at all (AGENTS.md §6). The persistence
    layer reads this to fill `itinerary_items.raw_payload`, `options_partial`
    and the price-insight fields.
    """

    raw_payload: dict[str, Any]
    price_insights: dict[str, Any] | None
    typical_price_range: tuple[int, int] | None
    escape_hatch_url: str
    #: Always true. SerpApi has a known open bug returning fewer booking
    #: options than the live page shows; never present a list as exhaustive.
    options_partial: bool = True
    #: A return-leg fetch was refused by quota, so the candidate list is
    #: shorter than the provider would have supplied. Distinct from simply
    #: reaching `MAX_RETURN_FETCHES`, which is a deliberate ceiling — this one
    #: means the search was cut short by something the user didn't choose, and
    #: `search()` has no way to say so in its return value.
    quota_truncated: bool = False


@dataclass(frozen=True)
class FlightBookingOptions:
    """Full booking-option set for one pinned itinerary.

    `resolve()` returns only the best option to satisfy the ProviderAdapter
    Protocol; the `resolve_booking_options` node wants the whole list.
    """

    options: list[BookingOption]
    options_partial: bool
    escape_hatch_url: str
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class BookingOption:
    vendor: str
    price_usd: Decimal | None
    booking_request: BookingRequest
    #: `together` | `departing` | `returning` — split options price one leg only.
    scope: str


# ── Field-level normalizers ─────────────────────────────────────────────────


def strip_flight_number(raw: str) -> str:
    """`"LX 243"` → `"LX243"`.

    SerpApi renders flight numbers with a space; the engine rejects them that
    way when echoed back in `selected_flights_json` (Appendix D).
    """
    return "".join(raw.split())


def _segment_date(time_str: str | None) -> str:
    """`"2027-03-10 01:50"` → `"2027-03-10"`."""
    if not time_str:
        return ""
    return time_str.split(" ", 1)[0]


def _parse_time(time_str: str | None) -> datetime | None:
    """Parse a SerpApi airport time.

    Returned **naive**: SerpApi gives local airport time with no offset, and
    inventing one would be a fabrication. `observed_at` is UTC-aware.
    """
    if not time_str:
        return None
    try:
        return datetime.strptime(time_str, "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _segments(flights: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Map raw segments to `selected_flights_json` entries."""
    out: list[dict[str, str]] = []
    for f in flights:
        dep = f.get("departure_airport") or {}
        arr = f.get("arrival_airport") or {}
        out.append(
            {
                "flight_number": strip_flight_number(f.get("flight_number") or ""),
                "departure_id": dep.get("id") or "",
                "arrival_id": arr.get("id") or "",
                "date": _segment_date(dep.get("time")),
            }
        )
    return out


@dataclass(frozen=True)
class _Leg:
    """One direction of an itinerary, normalized out of a raw entry.

    A one-way candidate is one of these; a round-trip candidate is two.
    """

    carrier: str
    segments: list[dict[str, str]]
    depart: datetime
    arrive: datetime
    price: Decimal
    duration_minutes: int
    stops: int


def _entries(raw: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """`best_flights` then `other_flights`, in the order the provider ranked them."""
    for group in ("best_flights", "other_flights"):
        for entry in raw.get(group) or []:
            if isinstance(entry, dict):
                yield entry


def _leg(entry: dict[str, Any]) -> _Leg | None:
    """Normalize one entry, or `None` if it is too incomplete to trust.

    No segments, an unparseable departure or arrival, or no price all drop the
    entry. Partial responses are normal and none of these may be filled in with
    a guess — an honest gap beats an invented candidate.
    """
    segments_raw = entry.get("flights") or []
    if not segments_raw:
        return None

    price = _price(entry.get("price"))
    if price is None:
        return None

    depart = _parse_time((segments_raw[0].get("departure_airport") or {}).get("time"))
    arrive = _parse_time((segments_raw[-1].get("arrival_airport") or {}).get("time"))
    if depart is None or arrive is None:
        return None

    duration = entry.get("total_duration")
    if not isinstance(duration, int):
        duration = int((arrive - depart).total_seconds() // 60)

    return _Leg(
        carrier=segments_raw[0].get("airline") or "",
        segments=_segments(segments_raw),
        depart=depart,
        arrive=arrive,
        price=price,
        duration_minutes=duration,
        stops=max(len(segments_raw) - 1, 0),
    )


def _cheapest_leg(raw: dict[str, Any]) -> _Leg | None:
    """The cheapest usable leg in a response, or `None` if none are usable.

    Used on the `departure_token` half: for a fixed outbound, the cheapest
    return is the cheapest round trip containing it.
    """
    legs = [leg for leg in map(_leg, _entries(raw)) if leg is not None]
    return min(legs, key=lambda leg: leg.price) if legs else None


def _typical_price_range(insights: dict[str, Any] | None) -> tuple[int, int] | None:
    """Read `price_insights.typical_price_range`, tolerating its absence.

    Present on the DXB→LHR spike route ($540–760), absent on JFK→LAX.
    """
    if not insights:
        return None
    rng = insights.get("typical_price_range")
    if isinstance(rng, list) and len(rng) == 2:
        try:
            return (int(rng[0]), int(rng[1]))
        except (TypeError, ValueError):
            return None
    return None


def _price(raw: Any) -> Decimal | None:
    """Never fabricate a price — a missing one drops the candidate."""
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (TypeError, ValueError, ArithmeticError):
        return None


# ── Refs — durable, inspectable, replayable ─────────────────────────────────


def encode_ref(query: dict[str, Any], selected_flights_json: dict[str, Any]) -> str:
    """Pack the query params plus the pinned itinerary into one opaque-looking ref.

    Deliberately *not* opaque: base64 of sorted JSON, so a ref stored months
    ago can be decoded in a debugger and replayed. This is the whole reason
    `booking_token` is not persisted.
    """
    payload = json.dumps(
        {"q": query, "sfj": selected_flights_json},
        sort_keys=True,
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_ref(ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Inverse of `encode_ref`. Returns `(query_params, selected_flights_json)`."""
    padded = ref + "=" * (-len(ref) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    return payload["q"], payload["sfj"]


def itinerary_flight_numbers(
    sfj: dict[str, Any], *, legs: tuple[str, ...] | None = None
) -> set[str]:
    """Every flight number pinned in `selected_flights_json`.

    `legs` narrows it to one leg — a `departing` booking option is only ever
    expected to cover the outbound.
    """
    wanted = legs if legs is not None else tuple(sfj.keys())
    return {
        segment["flight_number"]
        for leg in wanted
        for segment in sfj.get(leg) or []
        if segment.get("flight_number")
    }


def option_flight_numbers(block: dict[str, Any]) -> set[str] | None:
    """Flight numbers a booking option actually sells, from `marketed_as`.

    `None` means the option didn't say. On the money path that is not the same
    as "covers everything" — see `_covers`.
    """
    marketed = block.get("marketed_as")
    if not isinstance(marketed, list):
        return None
    return {strip_flight_number(str(m)) for m in marketed if m}


#: Which legs each option scope is expected to sell.
_SCOPE_LEGS: dict[str, tuple[str, ...]] = {
    "departing": ("outbound",),
    "returning": ("return",),
}


def sort_params(params: dict[str, Any]) -> dict[str, Any]:
    """Order query parameters deterministically before they reach the wire."""
    return dict(sorted(params.items()))


def sfj_param(selected_flights_json: dict[str, Any]) -> str:
    """Serialize `selected_flights_json` for the query string.

    `sort_keys` is load-bearing, not cosmetic: a ref survives an encode/decode
    round trip that reorders dict keys, and the same itinerary must always
    produce the same cache key and the same cassette hash.
    """
    return json.dumps(selected_flights_json, sort_keys=True, separators=(",", ":"))


def is_complete_itinerary(query: dict[str, Any], sfj: dict[str, Any]) -> bool:
    """A round trip needs both legs pinned; a one-way needs only the outbound."""
    if not sfj.get("outbound"):
        return False
    if int(query.get("type", TYPE_ROUND_TRIP)) == TYPE_ROUND_TRIP:
        return bool(sfj.get("return"))
    return True


def escape_hatch_url(query: dict[str, Any]) -> str:
    """Link to the live Google Flights page for the same search.

    Required because SerpApi's booking-option list is known to be incomplete.
    Never present the returned set as exhaustive.
    """
    dep = query.get("departure_id", "")
    arr = query.get("arrival_id", "")
    out = query.get("outbound_date", "")
    ret = query.get("return_date")
    phrase = f"Flights from {dep} to {arr} on {out}"
    if ret:
        phrase += f" through {ret}"
    return "https://www.google.com/travel/flights?" + urlencode({"q": phrase})


# ── Adapter ─────────────────────────────────────────────────────────────────


class FlightsAdapter:
    """SerpApi Google Flights. Satisfies `ProviderAdapter`.

        search()  →  user picks  →  resolve()  →  booking options

    `booking_token` is skipped entirely in favour of `selected_flights_json`.
    `departure_token` is used, but only inside `search()`: it fetches the
    return half of a round trip, which is not an optional extra step — Google
    Flights simply does not put both directions in one response.

    **A round-trip ref must pin both legs in one payload**, which is why
    `search()` completes them rather than handing back outbound-only
    candidates. Google Flights prices exactly the segments you send and does so
    without complaint: an outbound-only pin returns well-formed options at
    roughly half the fare, and `reprice` re-resolves the same pin and confirms
    them. `resolve_booking_options` checks `marketed_as` against the pin for
    this reason and refuses anything that sells less than the whole trip — the
    guard stays regardless of how the ref was built.
    """

    def __init__(self, trip_id: str | None = None) -> None:
        self.trip_id = trip_id
        self.last_search_context: FlightSearchContext | None = None
        # Per-ref context, so the persistence layer can look up the raw payload
        # for whichever candidate the agent proposed. Process-scoped and read
        # within the same turn — it is not a cache and not a store of record.
        self._context_by_ref: dict[str, FlightSearchContext] = {}

    # ── Public side channels ────────────────────────────────────────────

    def context(self, ref: str) -> FlightSearchContext | None:
        """Search context for a candidate ref, if this process produced it."""
        return self._context_by_ref.get(ref)

    def raw_payload(self, ref: str) -> dict[str, Any] | None:
        """Raw provider response for a candidate. Never goes to the agent."""
        ctx = self._context_by_ref.get(ref)
        return ctx.raw_payload if ctx else None

    # ── ProviderAdapter ─────────────────────────────────────────────────

    async def search(self, q: SearchQuery) -> list[NormalizedFlight] | QuotaExceeded:
        """Bookable candidates for a query, normalized top-N.

        A one-way search is one call. A **round-trip search returns candidates
        that already pin both legs** — Google Flights splits the itinerary
        across two responses and `departure_token` fetches the second half, so
        each candidate costs one extra call. Doing it here rather than leaving
        it to the caller is what makes the returned `selected_flights_json`
        directly resolvable: an outbound-only pin is priced as a one-way at
        roughly half the fare, and nothing downstream would notice.

        Cached 15 minutes on normalized arguments (`FLIGHT_SEARCH`), both
        halves.
        """
        params = self._search_params(q)
        raw = await self._get_cached(params, TTLTier.FLIGHT_SEARCH)
        if isinstance(raw, QuotaExceeded):
            return raw
        if not q.return_date:
            return self._normalize_flights(raw, params, leg="outbound", limit=q.limit)
        return await self._search_round_trip(raw, params, limit=q.limit)

    async def resolve(self, ref: str) -> BookingRequest | QuotaExceeded:
        """Booking options for a pinned itinerary — the best one.

        Called only at `item_pending`, never speculatively, and never cached.
        """
        result = await self.resolve_booking_options(ref)
        if isinstance(result, QuotaExceeded):
            return result
        if not result.options:
            raise NoBookingOptionsError(result.escape_hatch_url)
        return result.options[0].booking_request

    async def reprice(self, ref: str) -> Priced | QuotaExceeded:
        """Current price for a previously selected itinerary. Never cached.

        Runs unconditionally before every handoff (US-029). Reports
        `unavailable` with no price rather than guessing when the itinerary no
        longer sells — an invented number is worse than an honest gap.

        The returned `booking_request` is the one that came back *with this
        price*, in the same response. The handoff must POST that, not the copy
        captured at `item_pending` — `post_data` expires, and the veto window
        can be twelve hours wide.
        """
        result = await self.resolve_booking_options(ref)
        if isinstance(result, QuotaExceeded):
            return result

        observed_at = datetime.now(UTC)

        # `together` only. A split departing/returning pair is two vendors and
        # two handoffs, so no single link can charge the sum — which is exactly
        # what `split_options_only` exists to say instead of pretending
        # otherwise.
        together = [o for o in result.options if o.scope == "together" and o.price_usd is not None]
        if together:
            best = min(together, key=lambda o: o.price_usd or Decimal("Infinity"))
            return Priced(
                ref=ref,
                status=PriceStatus.available,
                price_usd=best.price_usd,
                observed_at=observed_at,
                booking_request=best.booking_request,
            )

        cheapest_leg = {
            scope: min(
                (
                    o.price_usd
                    for o in result.options
                    if o.scope == scope and o.price_usd is not None
                ),
                default=None,
            )
            for scope in ("departing", "returning")
        }
        if all(price is not None for price in cheapest_leg.values()):
            # Priced, but only as two separate purchases. The sum is what the
            # trip costs; `booking_request` stays None because there is no one
            # request that buys it, and the caller must not treat this as a
            # handoff. Reported only when *both* legs sell — one sellable leg
            # is not a priced itinerary, it's a hole.
            return Priced(
                ref=ref,
                status=PriceStatus.split_options_only,
                price_usd=sum(cheapest_leg.values(), start=Decimal(0)),
                observed_at=observed_at,
                unavailable_reason=(
                    "Sellable only as separate departing and returning bookings; "
                    f"no single link charges this itinerary. Escape hatch: "
                    f"{result.escape_hatch_url}"
                ),
            )

        return Priced(
            ref=ref,
            status=PriceStatus.unavailable,
            price_usd=None,
            observed_at=observed_at,
            unavailable_reason=(
                f"No booking option sells this itinerary. Escape hatch: {result.escape_hatch_url}"
            ),
        )

    # ── Beyond the Protocol ─────────────────────────────────────────────

    async def resolve_booking_options(self, ref: str) -> FlightBookingOptions | QuotaExceeded:
        """Every booking option for a pinned itinerary.

        Never cached — resolved at approval and consumed immediately.

        Every option is checked against the pin before it is returned: Google
        Flights sells exactly what you pinned, so an under-specified round-trip
        ref buys a one-way. See `PartialItineraryCoverageError`.
        """
        query, sfj = decode_ref(ref)
        if not is_complete_itinerary(query, sfj):
            raise IncompleteItineraryError(
                "A round-trip ref must pin both the outbound and the return before "
                "booking options are requested. Google Flights prices only the "
                "segments present in selected_flights_json, so an outbound-only pin "
                "returns a half-price one-way that looks like a valid round trip."
            )

        params = {**query, "selected_flights_json": sfj_param(sfj)}
        raw = await self._fetch(params)
        if isinstance(raw, QuotaExceeded):
            return raw

        hatch = escape_hatch_url(query)
        options: list[BookingOption] = []
        widest_coverage: set[str] = set()
        rejected = 0

        for option, covered in self._normalize_booking_options(raw):
            expected = itinerary_flight_numbers(sfj, legs=_SCOPE_LEGS.get(option.scope))
            if not self._covers(expected, covered):
                widest_coverage |= covered or set()
                rejected += 1
                continue
            options.append(option)

        if not options and rejected:
            # Options existed and every one of them sold less than the pin.
            raise PartialItineraryCoverageError(
                itinerary_flight_numbers(sfj), widest_coverage, hatch
            )

        options.sort(key=lambda o: (o.scope != "together", o.price_usd or Decimal("Infinity")))
        return FlightBookingOptions(
            options=options,
            options_partial=True,
            escape_hatch_url=hatch,
            raw_payload=raw,
        )

    @staticmethod
    def _covers(expected: set[str], covered: set[str] | None) -> bool:
        """Does this option sell every segment it is supposed to?

        An option with no `marketed_as` cannot be verified, and unverifiable is
        rejected rather than assumed good — the whole point of the check is
        that the under-covering response looks entirely well-formed.
        """
        if not expected:
            return True
        if covered is None:
            return False
        return expected <= covered

    # ── Round trip: the second half of the search ───────────────────────

    async def _search_round_trip(
        self, raw: dict[str, Any], query: dict[str, Any], *, limit: int
    ) -> list[NormalizedFlight] | QuotaExceeded:
        """Pair each outbound with its cheapest return, one extra call apiece.

        An outbound whose returns can't be fetched is **dropped, not
        downgraded**. Returning it with only the outbound pinned would hand the
        caller a ref that prices as a one-way — the exact failure the coverage
        guard exists to catch, arriving from the other direction.

        Each outbound's cheapest return is also the cheapest total for that
        outbound, so taking one per outbound gives the price frontier rather
        than a slice of one airline's timetable.
        """
        observed_at = datetime.now(UTC)
        pairs: list[tuple[str, NormalizedFlight]] = []
        fetches = 0
        quota_truncated = False

        for entry in _entries(raw):
            if len(pairs) >= limit or fetches >= MAX_RETURN_FETCHES:
                break

            outbound = _leg(entry)
            token = entry.get("departure_token")
            if outbound is None or not token:
                continue

            fetches += 1
            returns = await self._get_cached(
                {**query, "departure_token": token}, TTLTier.FLIGHT_SEARCH
            )
            if isinstance(returns, QuotaExceeded):
                # Stop rather than keep asking. Calls already spent are kept;
                # the refusal is recorded on the context because `search()`
                # returns a list here and cannot carry it.
                quota_truncated = True
                break

            inbound = _cheapest_leg(returns)
            if inbound is None:
                continue
            pairs.append(self._combine(outbound, inbound, query, observed_at))

        if not pairs and quota_truncated:
            # Nothing to show and a refusal to explain it — the refusal is the
            # honest answer, not an empty list that reads as "no flights".
            return QuotaExceeded(PROVIDER, f"Max {settings.quota_calls_per_turn} calls per turn")

        insights = (
            raw.get("price_insights") if isinstance(raw.get("price_insights"), dict) else None
        )
        ctx = FlightSearchContext(
            raw_payload=raw,
            price_insights=insights,
            typical_price_range=_typical_price_range(insights),
            escape_hatch_url=escape_hatch_url(query),
            quota_truncated=quota_truncated,
        )
        self.last_search_context = ctx
        for ref, _ in pairs:
            self._context_by_ref[ref] = ctx

        return [flight for _, flight in pairs]

    def _combine(
        self,
        outbound: _Leg,
        inbound: _Leg,
        query: dict[str, Any],
        observed_at: datetime,
    ) -> tuple[str, NormalizedFlight]:
        """One round-trip candidate from two legs.

        The scalar fields describe **the trip, not a leg**: `depart` is the
        outbound departure, `arrive` is the return arrival, and `stops` and
        `duration_minutes` sum both legs. Per-leg detail lives in
        `selected_flights_json`, which is the field that gets persisted and
        replayed anyway.

        `price_usd` comes from the return half. Google Flights quotes the
        round-trip total on the second screen, so the outbound entry's price
        is a partial figure and using it would understate every candidate.
        """
        sfj = {"outbound": outbound.segments, "return": inbound.segments}
        return encode_ref(query, sfj), NormalizedFlight(
            carrier=outbound.carrier,
            flight_numbers=[s["flight_number"] for s in outbound.segments + inbound.segments],
            depart=outbound.depart,
            arrive=inbound.arrive,
            stops=outbound.stops + inbound.stops,
            duration_minutes=outbound.duration_minutes + inbound.duration_minutes,
            price_usd=inbound.price,
            selected_flights_json=sfj,
            # Booking options resolve only at item_pending, never at search.
            booking_request=None,
            observed_at=observed_at,
        )

    # ── Normalization ───────────────────────────────────────────────────

    def _normalize_flights(
        self,
        raw: dict[str, Any],
        query: dict[str, Any],
        *,
        leg: str,
        limit: int,
    ) -> list[NormalizedFlight]:
        """Map a one-way search response to typed candidates.

        `best_flights` first, then `other_flights`, capped at `limit`. Missing
        keys are tolerated: partial responses are normal (travel-providers).

        One-way only. A round trip goes through `_search_round_trip`, because a
        single-leg candidate is not resolvable — see `search()`.
        """
        insights = (
            raw.get("price_insights") if isinstance(raw.get("price_insights"), dict) else None
        )
        ctx = FlightSearchContext(
            raw_payload=raw,
            price_insights=insights,
            typical_price_range=_typical_price_range(insights),
            escape_hatch_url=escape_hatch_url(query),
        )
        self.last_search_context = ctx

        observed_at = datetime.now(UTC)
        candidates: list[NormalizedFlight] = []

        for entry in _entries(raw):
            if len(candidates) >= limit:
                break
            one_way = _leg(entry)
            if one_way is None:
                continue

            sfj: dict[str, Any] = {leg: one_way.segments}
            self._context_by_ref[encode_ref(query, sfj)] = ctx
            candidates.append(
                NormalizedFlight(
                    carrier=one_way.carrier,
                    flight_numbers=[s["flight_number"] for s in one_way.segments],
                    depart=one_way.depart,
                    arrive=one_way.arrive,
                    stops=one_way.stops,
                    duration_minutes=one_way.duration_minutes,
                    price_usd=one_way.price,
                    selected_flights_json=sfj,
                    # Booking options resolve only at item_pending, never here.
                    booking_request=None,
                    observed_at=observed_at,
                )
            )

        return candidates

    def _normalize_booking_options(
        self, raw: dict[str, Any]
    ) -> Iterator[tuple[BookingOption, set[str] | None]]:
        """Yield `(option, flight numbers it sells)` per priced block.

        An option is either `together` (whole trip, one vendor) or split into
        `departing` / `returning`. `post_data` passes through untouched. The
        coverage set rides alongside rather than on `BookingOption` so the
        caller has to look at it.
        """
        for option in raw.get("booking_options") or []:
            if not isinstance(option, dict):
                continue
            for scope in ("together", "departing", "returning"):
                block = option.get(scope)
                if not isinstance(block, dict):
                    continue
                br_raw = block.get("booking_request")
                if not isinstance(br_raw, dict) or not br_raw.get("url"):
                    continue
                yield (
                    BookingOption(
                        vendor=block.get("book_with") or "",
                        price_usd=_price(block.get("price")),
                        booking_request=BookingRequest(
                            url=br_raw["url"],
                            post_data=self._envelope_post_data(br_raw.get("post_data")),
                            vendor=block.get("book_with") or "",
                        ),
                        scope=scope,
                    ),
                    option_flight_numbers(block),
                )

    @staticmethod
    def _envelope_post_data(post_data: Any) -> dict[str, Any] | None:
        """Carry the opaque form body verbatim under `_raw`.

        Never parsed into fields: re-encoding reorders keys and re-escapes
        values, and vendors reject the result. Treat it as bytes.
        """
        if post_data is None:
            return None
        if isinstance(post_data, dict):
            # Already enveloped, or the engine changed shape — pass through.
            return post_data
        return {POST_DATA_ENVELOPE_KEY: post_data}

    # ── Transport ───────────────────────────────────────────────────────

    def _search_params(self, q: SearchQuery) -> dict[str, Any]:
        """Normalized arguments. Also the cache key and the cassette hash input."""
        params: dict[str, Any] = {
            "engine": ENGINE,
            "departure_id": q.origin or "",
            "arrival_id": q.destination,
            "outbound_date": str(q.departure_date) if q.departure_date else "",
            "adults": q.guests,
            "currency": "USD",
            "hl": "en",
            "type": TYPE_ROUND_TRIP if q.return_date else TYPE_ONE_WAY,
        }
        if q.return_date:
            params["return_date"] = str(q.return_date)
        return params

    async def _get_cached(
        self, params: dict[str, Any], tier: TTLTier
    ) -> dict[str, Any] | QuotaExceeded:
        """Cached fetch. Quota is checked on the miss path only — a cache hit
        costs no provider call and must not burn budget."""
        key = cache_key(PROVIDER, "flights", args_hash(params))

        async def fetch() -> dict[str, Any]:
            result = await self._fetch(params)
            if isinstance(result, QuotaExceeded):
                # Unwind past get_or_fetch so the refusal is never stored — a
                # cached refusal would outlive the window that produced it and
                # blank out results for the next 15 minutes.
                raise _QuotaRefusal(result)
            return result

        try:
            return await get_or_fetch(key, tier, fetch)
        except _QuotaRefusal as refusal:
            # Still a value at the adapter boundary. The exception is private
            # control flow, never visible to a caller.
            return refusal.value

    async def _fetch(self, params: dict[str, Any]) -> dict[str, Any] | QuotaExceeded:
        """One billed provider call, quota-checked.

        Quota is enforced here, at the adapter boundary — never in agent
        instructions. Exceeding returns a value; it never raises.
        """
        refusal = await quota_counter.check(PROVIDER, self.trip_id)
        if refusal is not None:
            return refusal

        # Sorted for a stable, diffable URL. Not load-bearing for the cassette
        # hash any more — S1.1 canonicalizes the parameter dict before hashing,
        # so a ref that came back from encode/decode in a different key order
        # now lands on the same cassette either way.
        request_params = sort_params(params)
        secret = getattr(settings, _API_KEY_SETTING)
        if settings.provider_mode != "replay":
            if secret is None:
                raise RuntimeError(
                    f"{_API_KEY_SETTING} is unset and PROVIDER_MODE={settings.provider_mode}. "
                    "Set SERPAPI_API_KEY, or use PROVIDER_MODE=replay."
                )
            # Unwrapped here and nowhere earlier. S1.1 strips credential-named
            # parameters before hashing and before writing, so this neither
            # forks the cassette hash nor lands in a committed fixture.
            request_params["api_key"] = secret.get_secret_value()

        async with make_client(PROVIDER) as client:
            response = await client.get(SERPAPI_ENDPOINT, params=request_params)
            response.raise_for_status()
            payload = response.json()

        await quota_counter.increment(PROVIDER, self.trip_id)
        return payload if isinstance(payload, dict) else {}
