"""Adapter-normalizer tripwires for the flights lane (A1, US-012).

Deterministic and free: every assertion runs against a recorded cassette in
`PROVIDER_MODE=replay`. No live call is possible from this file.

The bodies under `fixtures/cassettes/serpapi_google_flights/` were authored
from PRD Appendix D (the S0.1 spike, DXB→LHR and JFK→LAX) plus the documented
SerpApi `google_flights` response shape — not recorded, since recording is
billed and requires approval.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from redis.exceptions import RedisError

import app.shared.cassettes as cassettes_mod
from app.models.types import NormalizedFlight, Priced, PriceStatus
from app.shared import redis_client
from app.shared.cache import clear_cache
from app.shared.config import settings
from app.shared.quota import QuotaExceeded, quota_counter
from app.travel.adapters.flights import (
    MAX_RETURN_FETCHES,
    POST_DATA_ENVELOPE_KEY,
    PROVIDER,
    FlightsAdapter,
    IncompleteItineraryError,
    NoBookingOptionsError,
    PartialItineraryCoverageError,
    _entries,
    decode_ref,
    encode_ref,
    is_complete_itinerary,
    itinerary_flight_numbers,
    option_flight_numbers,
    strip_flight_number,
)
from app.travel.base import ProviderAdapter, SearchQuery

pytestmark = pytest.mark.normalizer

REPO_ROOT = Path(__file__).resolve().parent.parent

DXB_LHR = SearchQuery(
    origin="DXB",
    destination="LHR",
    departure_date=date(2027, 3, 10),
    return_date=date(2027, 3, 20),
    guests=1,
    limit=10,
)
JFK_LAX = SearchQuery(
    origin="JFK",
    destination="LAX",
    departure_date=date(2027, 3, 10),
    guests=1,
    limit=10,
)
JFK_LAX_ROUND_TRIP = SearchQuery(
    origin="JFK",
    destination="LAX",
    departure_date=date(2027, 3, 10),
    return_date=date(2027, 3, 15),
    guests=1,
    limit=10,
)

#: One search + one `departure_token` call per outbound candidate.
ROUND_TRIP_CALLS = 1 + MAX_RETURN_FETCHES


async def _purge() -> None:
    """Drop this project's cache and quota keys. Best-effort by design.

    Redis-backed since S1.1, and both modules fail open when it's unreachable.
    That's right for production and wrong for a test: a test that needs a real
    counter asks for `redis_available` and skips loudly, and one that doesn't
    still gets isolation from whatever a neighbour left behind.
    """
    try:
        r = redis_client.get_redis()
        for prefix in ("cache:*", "quota:*"):
            async for key in r.scan_iter(match=prefix, count=500):
                await r.delete(key)
    except RedisError:
        pass


@pytest.fixture(autouse=True)
async def _isolate():
    """Absolute cassette path, empty cache, fresh quota — per test."""
    original = cassettes_mod.CASSETTE_DIR
    cassettes_mod.CASSETTE_DIR = REPO_ROOT / "fixtures" / "cassettes"
    # pytest-asyncio hands each test a new event loop; a pool cached from the
    # previous one raises `Event loop is closed` on first use, which is not a
    # RedisError and would not fail open.
    redis_client.reset_redis()
    await _purge()
    yield
    cassettes_mod.CASSETTE_DIR = original
    await clear_cache()
    await _purge()


async def _burn_turn_quota(calls: int) -> None:
    """Leave exactly `quota_calls_per_turn - calls` calls in the budget."""
    for _ in range(calls):
        await quota_counter.increment(PROVIDER)


async def _exhaust_turn_quota() -> None:
    """Burn the per-turn ceiling so the next call is refused."""
    await _burn_turn_quota(settings.quota_calls_per_turn)


async def _turn_calls() -> int:
    turn_calls, _ = await quota_counter.peek(PROVIDER)
    return turn_calls


async def _swiss(adapter: FlightsAdapter) -> NormalizedFlight:
    """The SWISS LX243+LX316 / LX317+LX242 round trip, straight from search."""
    flights = await adapter.search(DXB_LHR)
    return next(f for f in flights if f.flight_numbers[0] == "LX243")


async def _full_ref(adapter: FlightsAdapter) -> str:
    """A complete, directly resolvable round-trip ref — what search now returns."""
    swiss = await _swiss(adapter)
    return encode_ref(adapter._search_params(DXB_LHR), swiss.selected_flights_json)


async def _outbound_ref(adapter: FlightsAdapter) -> str:
    """The same trip with the return stripped back off.

    Search no longer produces one of these; it has to be built by hand, which
    is the point — the shape that silently buys a one-way is now something a
    caller has to go out of its way to construct.
    """
    query, sfj = decode_ref(await _full_ref(adapter))
    return encode_ref(query, {"outbound": sfj["outbound"]})


# ── Protocol conformance ────────────────────────────────────────────────────


def test_satisfies_provider_adapter_protocol() -> None:
    assert isinstance(FlightsAdapter(), ProviderAdapter)


# ── Flight-number whitespace (Appendix D) ───────────────────────────────────


def test_strip_flight_number() -> None:
    assert strip_flight_number("LX 243") == "LX243"
    assert strip_flight_number("EK 001") == "EK001"
    assert strip_flight_number("AS12") == "AS12"
    assert strip_flight_number("") == ""


async def test_whitespace_stripped_before_building_selected_flights_json() -> None:
    """SerpApi renders `"LX 243"`; the engine only accepts `"LX243"`."""
    adapter = FlightsAdapter()
    flights = await adapter.search(DXB_LHR)

    for flight in flights:
        for number in flight.flight_numbers:
            assert " " not in number
        for leg in flight.selected_flights_json.values():
            for segment in leg:
                assert " " not in segment["flight_number"]

    assert flights[0].selected_flights_json["outbound"][0]["flight_number"] == "LX243"


# ── Recorded response → typed output, no field loss ─────────────────────────


async def test_round_trip_search_returns_both_legs_pinned() -> None:
    """A round-trip candidate carries the whole trip, not half of it.

    The scalar fields describe the trip: `depart` starts it, `arrive` ends it,
    and `stops`/`duration_minutes` sum both legs. Per-leg detail is in
    `selected_flights_json`, which is what gets persisted and replayed.
    """
    adapter = FlightsAdapter()
    flights = await adapter.search(DXB_LHR)

    swiss = flights[0]
    assert isinstance(swiss, NormalizedFlight)
    assert swiss.carrier == "SWISS"
    assert swiss.flight_numbers == ["LX243", "LX316", "LX317", "LX242"]
    assert swiss.depart.isoformat() == "2027-03-10T01:50:00"  # outbound departure
    assert swiss.arrive.isoformat() == "2027-03-20T23:10:00"  # return arrival
    assert swiss.stops == 2  # one each way
    assert swiss.duration_minutes == 1195  # 600 out + 595 back, flying time
    assert swiss.observed_at.tzinfo is not None

    # The round-trip total from the return half — not the outbound's own price,
    # which is a partial figure and would understate every candidate.
    assert swiss.price_usd == Decimal("640")

    # Segment dates come from `departure_airport.time`, not the search params.
    assert swiss.selected_flights_json == {
        "outbound": [
            {
                "flight_number": "LX243",
                "departure_id": "DXB",
                "arrival_id": "ZRH",
                "date": "2027-03-10",
            },
            {
                "flight_number": "LX316",
                "departure_id": "ZRH",
                "arrival_id": "LHR",
                "date": "2027-03-10",
            },
        ],
        "return": [
            {
                "flight_number": "LX317",
                "departure_id": "LHR",
                "arrival_id": "ZRH",
                "date": "2027-03-20",
            },
            {
                "flight_number": "LX242",
                "departure_id": "ZRH",
                "arrival_id": "DXB",
                "date": "2027-03-20",
            },
        ],
    }

    nonstop = flights[1]
    assert nonstop.carrier == "Emirates"
    assert nonstop.flight_numbers == ["EK001", "EK002"]
    assert nonstop.stops == 0
    assert nonstop.price_usd == Decimal("712")


async def test_every_round_trip_candidate_is_directly_resolvable() -> None:
    """Search must not emit a ref that `resolve()` would refuse.

    An outbound-only candidate prices as a half-fare one-way, so a search that
    can produce one has moved the failure earlier rather than removing it.
    """
    adapter = FlightsAdapter()
    query = adapter._search_params(DXB_LHR)

    for flight in await adapter.search(DXB_LHR):
        sfj = flight.selected_flights_json
        assert set(sfj) == {"outbound", "return"}
        assert is_complete_itinerary(query, sfj)


async def test_round_trip_fan_out_is_bounded_by_the_adapter(redis_available: bool) -> None:
    """Fan-out limits live here, never in agent instructions (AGENTS.md §6).

    One search plus one call per outbound completed — and no more, whatever
    the response offers.
    """
    adapter = FlightsAdapter()
    flights = await adapter.search(DXB_LHR)

    assert len(flights) == MAX_RETURN_FETCHES
    assert await _turn_calls() == ROUND_TRIP_CALLS
    # The cassette lists more outbound candidates than the ceiling allows.
    assert len(list(_entries(adapter.last_search_context.raw_payload))) > MAX_RETURN_FETCHES


async def test_search_returns_no_booking_request() -> None:
    """Options resolve at `item_pending` only — never speculatively at search."""
    adapter = FlightsAdapter()
    flights = await adapter.search(DXB_LHR)
    assert all(f.booking_request is None for f in flights)


# ── Partial responses are normal; nothing is fabricated ─────────────────────


async def test_partial_entries_are_dropped_not_invented() -> None:
    """An entry with no price, no departure time, or no segments is skipped.

    An invented estimate is worse than an honest gap. Checked on the one-way
    path, where nothing else truncates the list — the round-trip fan-out
    ceiling would hide the drop behind its own limit.
    """
    adapter = FlightsAdapter()
    flights = await adapter.search(JFK_LAX)

    raw = adapter.last_search_context.raw_payload
    assert len(raw["best_flights"]) + len(raw["other_flights"]) == 5
    # Three degenerate entries in `other_flights` are dropped.
    assert [f.flight_numbers for f in flights] == [["AS12"], ["B6423"]]
    assert all(f.price_usd is not None for f in flights)


# ── price_insights ──────────────────────────────────────────────────────────


async def test_typical_price_range_captured_when_present() -> None:
    adapter = FlightsAdapter()
    await adapter.search(DXB_LHR)
    ctx = adapter.last_search_context
    assert ctx.typical_price_range == (540, 760)
    assert ctx.price_insights["lowest_price"] == 640


async def test_typical_price_range_absent_is_none_not_zero() -> None:
    """JFK→LAX returned no typical range in the spike. Absence stays absence."""
    adapter = FlightsAdapter()
    await adapter.search(JFK_LAX)
    ctx = adapter.last_search_context
    assert ctx.typical_price_range is None
    assert ctx.price_insights["lowest_price"] == 344


# ── Raw payload stays out of the conversation (FR-14) ───────────────────────


async def test_raw_payload_reachable_for_persistence_but_not_on_the_result() -> None:
    adapter = FlightsAdapter()
    flights = await adapter.search(DXB_LHR)
    ref = encode_ref(adapter._search_params(DXB_LHR), flights[0].selected_flights_json)

    raw = adapter.raw_payload(ref)
    assert raw is not None and "search_metadata" in raw

    # Nothing provider-shaped rides along on what the agent sees.
    serialized = flights[0].model_dump_json()
    for leaked in ("departure_token", "booking_token", "search_metadata", "airline_logo"):
        assert leaked not in serialized


async def test_booking_token_is_never_persisted_anywhere() -> None:
    """The cassette carries `departure_token`; no normalized field may hold it."""
    adapter = FlightsAdapter()
    flights = await adapter.search(DXB_LHR)
    for flight in flights:
        blob = json.dumps(flight.selected_flights_json)
        assert "token" not in blob.lower()


def test_ref_round_trips_and_stays_inspectable() -> None:
    """A ref stored months ago must decode without the provider."""
    query = {"departure_id": "DXB", "arrival_id": "LHR", "type": 1}
    sfj = {
        "outbound": [
            {
                "flight_number": "LX243",
                "departure_id": "DXB",
                "arrival_id": "ZRH",
                "date": "2027-03-10",
            }
        ]
    }
    assert decode_ref(encode_ref(query, sfj)) == (query, sfj)


# ── Caching: 15 minutes on normalized arguments ─────────────────────────────


async def test_search_cached_on_normalized_arguments(redis_available: bool) -> None:
    """Both halves are cached — the `departure_token` calls too.

    Caching only the outbound would leave a repeated round-trip search costing
    `MAX_RETURN_FETCHES` billed calls while looking free.
    """
    adapter = FlightsAdapter()
    first = await adapter.search(DXB_LHR)
    calls_after_first = await _turn_calls()

    second = await adapter.search(DXB_LHR)
    assert await _turn_calls() == calls_after_first == ROUND_TRIP_CALLS
    assert [f.selected_flights_json for f in second] == [f.selected_flights_json for f in first]


async def test_booking_options_are_never_cached(redis_available: bool) -> None:
    """Resolved at approval and consumed immediately — a cached fare is fiction."""
    adapter = FlightsAdapter()
    ref = await _full_ref(adapter)
    before = await _turn_calls()

    await adapter.resolve_booking_options(ref)
    await adapter.resolve_booking_options(ref)
    assert await _turn_calls() == before + 2


# ── Quota returns as a value, never raises ──────────────────────────────────


async def test_quota_exceeded_returns_as_a_value(redis_available: bool) -> None:
    adapter = FlightsAdapter()
    await _exhaust_turn_quota()

    result = await adapter.search(DXB_LHR)
    assert isinstance(result, QuotaExceeded)
    assert not result


async def test_fan_out_cut_short_by_quota_keeps_what_it_paid_for(
    redis_available: bool,
) -> None:
    """A refusal mid-fan-out stops the search; it doesn't discard the results.

    Those calls were billed. Throwing them away to return a tidy refusal would
    charge for nothing — but the shortfall has to be visible somewhere, and
    `search()` returns a list here and cannot say it.
    """
    adapter = FlightsAdapter()
    # Budget for the search plus exactly one return-leg fetch.
    await _burn_turn_quota(settings.quota_calls_per_turn - 2)

    flights = await adapter.search(DXB_LHR)
    assert isinstance(flights, list)
    assert len(flights) == 1
    assert set(flights[0].selected_flights_json) == {"outbound", "return"}
    assert adapter.last_search_context.quota_truncated is True


async def test_fan_out_refused_outright_returns_the_refusal(redis_available: bool) -> None:
    """No complete candidate and a refusal to explain it — say so.

    An empty list would read as "no flights on this route", which is a
    different fact and would send the agent looking for other dates.
    """
    adapter = FlightsAdapter()
    # Budget for the search and nothing else.
    await _burn_turn_quota(settings.quota_calls_per_turn - 1)

    result = await adapter.search(DXB_LHR)
    assert isinstance(result, QuotaExceeded)
    assert not result


async def test_quota_refusal_does_not_poison_the_cache(redis_available: bool) -> None:
    """A refusal must not be stored — it would blank results for 15 minutes."""
    adapter = FlightsAdapter()
    await _exhaust_turn_quota()
    assert isinstance(await adapter.search(DXB_LHR), QuotaExceeded)

    await _purge()
    flights = await adapter.search(DXB_LHR)
    assert isinstance(flights, list) and len(flights) == MAX_RETURN_FETCHES


# ── Booking options and post_data ───────────────────────────────────────────


async def test_post_data_preserved_verbatim() -> None:
    """Opaque bytes through normalization — never reordered, re-encoded or pruned."""
    adapter = FlightsAdapter()
    ref = await _full_ref(adapter)
    result = await adapter.resolve_booking_options(ref)

    swiss = result.options[0]
    assert swiss.vendor == "SWISS"
    assert swiss.scope == "together"

    raw_option = result.raw_payload["booking_options"][0]["together"]
    envelope = swiss.booking_request.post_data
    assert envelope[POST_DATA_ENVELOPE_KEY] == raw_option["booking_request"]["post_data"]
    assert swiss.booking_request.url == "https://www.google.com/travel/clk/f"


async def test_split_and_malformed_options_normalized_or_skipped() -> None:
    adapter = FlightsAdapter()
    ref = await _full_ref(adapter)
    result = await adapter.resolve_booking_options(ref)

    scopes = sorted({o.scope for o in result.options})
    assert scopes == ["departing", "returning", "together"]

    # Two options with no usable booking_request are dropped, not half-built.
    assert len(result.options) == 4
    vendors = {o.vendor for o in result.options}
    assert "Ghost Travel" not in vendors and "No URL" not in vendors

    # `together` first, cheapest first within scope.
    assert [o.vendor for o in result.options[:2]] == ["SWISS", "Booking.com"]


async def test_option_lists_are_always_marked_partial() -> None:
    """SerpApi has an open bug returning fewer options than the live page."""
    adapter = FlightsAdapter()
    ref = await _full_ref(adapter)
    result = await adapter.resolve_booking_options(ref)

    assert result.options_partial is True
    assert result.escape_hatch_url.startswith("https://www.google.com/travel/flights?")
    assert "DXB" in result.escape_hatch_url and "LHR" in result.escape_hatch_url


async def test_resolved_option_marketed_as_covers_the_whole_itinerary() -> None:
    """End to end: search → resolve, and the option sold must be the trip.

    `marketed_as` is read out of the raw payload rather than through
    `option_flight_numbers`, so this asserts against what the provider actually
    said instead of re-running the adapter's own interpretation of it. If the
    two ever disagree, this is the test that notices.

    A round-trip handoff whose option covers only the outbound lands on a
    one-way checkout at half the fare, and re-price agrees with itself because
    it re-resolves the same pin.
    """
    adapter = FlightsAdapter()
    swiss = await _swiss(adapter)
    ref = encode_ref(adapter._search_params(DXB_LHR), swiss.selected_flights_json)

    booking_request = await adapter.resolve(ref)
    result = await adapter.resolve_booking_options(ref)
    chosen = result.options[0]
    assert chosen.booking_request == booking_request

    raw_block = next(
        block
        for option in result.raw_payload["booking_options"]
        for block in [option.get("together")]
        if block and block.get("book_with") == chosen.vendor
    )
    marketed = {"".join(m.split()) for m in raw_block["marketed_as"]}

    pinned = set(swiss.flight_numbers)
    assert pinned == {"LX243", "LX316", "LX317", "LX242"}
    assert pinned <= marketed, f"option sells {sorted(marketed)}, trip needs {sorted(pinned)}"


async def test_resolve_returns_the_best_option() -> None:
    adapter = FlightsAdapter()
    ref = await _full_ref(adapter)
    booking_request = await adapter.resolve(ref)

    assert booking_request.vendor == "SWISS"
    assert booking_request.post_data is not None


async def test_resolve_refuses_an_outbound_only_round_trip(redis_available: bool) -> None:
    """An outbound-only pin on a round trip returns a half-price one-way.

    Google Flights answers it happily, so nothing downstream would notice.
    Refused before the call is made — the request never leaves the process,
    so the quota counter must not move either.
    """
    adapter = FlightsAdapter()
    ref = await _outbound_ref(adapter)
    calls_before = await _turn_calls()

    with pytest.raises(IncompleteItineraryError):
        await adapter.resolve(ref)

    assert await _turn_calls() == calls_before


# ── Coverage: options must sell the whole pinned trip ───────────────────────


def test_option_flight_numbers_strips_whitespace() -> None:
    """`marketed_as` carries the spaced form; the pin carries the stripped one."""
    assert option_flight_numbers({"marketed_as": ["AS 631", "AS 731"]}) == {"AS631", "AS731"}
    assert option_flight_numbers({}) is None


def test_itinerary_flight_numbers_covers_both_legs() -> None:
    sfj = {
        "outbound": [{"flight_number": "AS631"}, {"flight_number": "AS731"}],
        "return": [{"flight_number": "AS1290"}],
    }
    assert itinerary_flight_numbers(sfj) == {"AS631", "AS731", "AS1290"}
    assert itinerary_flight_numbers(sfj, legs=("outbound",)) == {"AS631", "AS731"}


async def test_options_covering_only_the_outbound_are_refused() -> None:
    """The observed JFK→LAX failure: complete pin, options sell the outbound.

    `marketed_as` lists AS 631 / AS 731 and the price is $170 against a $344
    round-trip best. The response is well-formed, both options parse cleanly,
    and a handoff would land on a one-way checkout at half the fare.
    """
    adapter = FlightsAdapter()
    ref = encode_ref(
        adapter._search_params(JFK_LAX_ROUND_TRIP),
        {
            "outbound": [
                {
                    "flight_number": "AS631",
                    "departure_id": "JFK",
                    "arrival_id": "SEA",
                    "date": "2027-03-10",
                },
                {
                    "flight_number": "AS731",
                    "departure_id": "SEA",
                    "arrival_id": "LAX",
                    "date": "2027-03-10",
                },
            ],
            "return": [
                {
                    "flight_number": "AS1290",
                    "departure_id": "LAX",
                    "arrival_id": "JFK",
                    "date": "2027-03-15",
                },
            ],
        },
    )

    with pytest.raises(PartialItineraryCoverageError) as caught:
        await adapter.resolve(ref)

    assert caught.value.missing == {"AS1290"}
    assert caught.value.covered == {"AS631", "AS731"}
    assert "AS1290" in str(caught.value)


async def test_reprice_refuses_an_undercovered_itinerary_too() -> None:
    """Re-price re-resolves the same pin, so it would confirm the wrong fare.

    It has to fail on exactly the same condition, or the guard has a hole
    precisely where the money is.
    """
    adapter = FlightsAdapter()
    ref = encode_ref(
        adapter._search_params(JFK_LAX_ROUND_TRIP),
        {
            "outbound": [
                {
                    "flight_number": "AS631",
                    "departure_id": "JFK",
                    "arrival_id": "SEA",
                    "date": "2027-03-10",
                },
                {
                    "flight_number": "AS731",
                    "departure_id": "SEA",
                    "arrival_id": "LAX",
                    "date": "2027-03-10",
                },
            ],
            "return": [
                {
                    "flight_number": "AS1290",
                    "departure_id": "LAX",
                    "arrival_id": "JFK",
                    "date": "2027-03-15",
                },
            ],
        },
    )

    with pytest.raises(PartialItineraryCoverageError):
        await adapter.reprice(ref)


async def test_split_options_are_checked_against_their_own_leg() -> None:
    """A `departing` block sells the outbound and nothing more — that's correct.

    Holding it to the whole itinerary would reject legitimate split options.
    """
    adapter = FlightsAdapter()
    result = await adapter.resolve_booking_options(await _full_ref(adapter))

    departing = next(o for o in result.options if o.scope == "departing")
    returning = next(o for o in result.options if o.scope == "returning")
    assert departing.vendor == "Expedia"
    assert returning.vendor == "Expedia"


async def test_unverifiable_options_are_rejected_not_assumed_good() -> None:
    """No `marketed_as` means the coverage claim can't be checked.

    Unverifiable is rejected: the failure this guards against looks entirely
    well-formed, so absence of evidence is not evidence of coverage.
    """
    adapter = FlightsAdapter()
    result = await adapter.resolve_booking_options(await _full_ref(adapter))

    # Kayak is priced, has a usable booking_request, and would have been handed
    # off — but it never says what it sells, so it doesn't survive the check.
    vendors = {o.vendor for o in result.options}
    assert "Kayak" not in vendors
    assert "SWISS" in vendors

    assert adapter._covers({"LX243"}, None) is False
    assert adapter._covers({"LX243"}, {"LX243", "LX316"}) is True
    assert adapter._covers(set(), None) is True


# ── Reprice ─────────────────────────────────────────────────────────────────


async def test_reprice_reads_the_live_option_price() -> None:
    adapter = FlightsAdapter()
    ref = await _full_ref(adapter)
    priced = await adapter.reprice(ref)

    assert isinstance(priced, Priced)
    assert priced.status is PriceStatus.available
    assert priced.price_usd == Decimal("640")
    assert priced.observed_at.tzinfo is not None
    assert priced.ref == ref
    # The whole itinerary is still decodable from the ref months later.
    assert decode_ref(priced.ref)[1]["return"][0]["flight_number"] == "LX317"


async def test_reprice_carries_the_post_data_that_came_with_this_price() -> None:
    """The handoff must POST *this* body, not the one captured at item_pending.

    `post_data` expires and the veto window can be twelve hours wide, so a
    `Priced` without a fresh `booking_request` sends the user to a dead form.
    """
    adapter = FlightsAdapter()
    ref = await _full_ref(adapter)
    priced = await adapter.reprice(ref)

    assert priced.booking_request is not None
    assert priced.booking_request.vendor == "SWISS"

    options = await adapter.resolve_booking_options(ref)
    fresh = options.raw_payload["booking_options"][0]["together"]["booking_request"]["post_data"]
    assert priced.booking_request.post_data[POST_DATA_ENVELOPE_KEY] == fresh


async def test_reprice_split_only_is_priced_but_not_handoff_eligible() -> None:
    """Both legs sell, no single link charges the trip. The third case.

    `if priced.price_usd: hand_off()` reads as correct and would send the user
    to a link that buys half the itinerary, so this must not come back as
    `available` — and must not come back as `unavailable` either, because the
    trip exists and has a real total.
    """
    adapter = FlightsAdapter()
    ref = encode_ref(
        adapter._search_params(JFK_LAX_ROUND_TRIP),
        {
            "outbound": [
                {
                    "flight_number": "AS12",
                    "departure_id": "JFK",
                    "arrival_id": "LAX",
                    "date": "2027-03-10",
                },
            ],
            "return": [
                {
                    "flight_number": "AS1291",
                    "departure_id": "LAX",
                    "arrival_id": "JFK",
                    "date": "2027-03-15",
                },
            ],
        },
    )

    priced = await adapter.reprice(ref)
    assert priced.status is PriceStatus.split_options_only
    assert priced.price_usd == Decimal("405")  # 210 departing + 195 returning
    # No single request buys this, so there is nothing to hand off.
    assert priced.booking_request is None


async def test_reprice_reports_unavailable_rather_than_guessing() -> None:
    """No booking options means no price — not the last one we saw."""
    adapter = FlightsAdapter()
    ref = await _outbound_ref(adapter)
    query, sfj = decode_ref(ref)
    sold_out = encode_ref(
        query,
        {
            **sfj,
            "return": [
                {
                    "flight_number": "EK002",
                    "departure_id": "LHR",
                    "arrival_id": "DXB",
                    "date": "2027-03-20",
                }
            ],
        },
    )

    priced = await adapter.reprice(sold_out)
    # Sold out, not "purchasable as two separate bookings" — the third case is
    # the reason PriceStatus is an enum and not two booleans.
    assert priced.status is PriceStatus.unavailable
    assert priced.price_usd is None
    assert priced.booking_request is None
    assert priced.unavailable_reason and "google.com/travel/flights" in priced.unavailable_reason


async def test_resolve_raises_no_booking_options_not_incomplete() -> None:
    """A complete-but-unsellable itinerary is the world, not a caller bug."""
    adapter = FlightsAdapter()
    query, sfj = decode_ref(await _outbound_ref(adapter))
    sold_out = encode_ref(
        query,
        {
            **sfj,
            "return": [
                {
                    "flight_number": "EK002",
                    "departure_id": "LHR",
                    "arrival_id": "DXB",
                    "date": "2027-03-20",
                }
            ],
        },
    )

    with pytest.raises(NoBookingOptionsError) as caught:
        await adapter.resolve(sold_out)
    assert caught.value.escape_hatch_url.startswith("https://www.google.com/travel/flights?")
