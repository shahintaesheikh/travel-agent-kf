"""Tripwires for the activities adapter — lane A4, US-016.

Normalizer assertions run against the cassettes in `fixtures/cassettes/`, so they
are free and deterministic. Per AGENTS.md §8 there are no ranking-quality or
agent-behaviour tests here: non-deterministic and low value at two users.

The cassettes are hand-authored from the shapes documented in
`serpapi-tripadvisor` and `serpapi-google-events`, not live recordings —
recording costs money and requires approval (AGENTS.md §0).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.shared.cache import TTLTier, clear_cache
from app.shared.config import settings
from app.shared.quota import QuotaExceeded, quota_counter
from app.travel.adapters.activities import (
    _MAX_EVENT_PAGES,
    _SSRC_THINGS_TO_DO,
    _TRIPADVISOR_LIMIT,
    ActivitiesAdapter,
    normalize_events,
    normalize_tripadvisor,
)
from app.travel.base import ProviderAdapter, SearchQuery

pytestmark = pytest.mark.normalizer

CASSETTES = Path("fixtures/cassettes")


def _cassette_body(provider: str, params: dict) -> dict:
    """Read the cassette the adapter would hit for these params.

    Goes through the trunk's `request_fingerprint` rather than re-deriving the
    hash. A local copy of the naming rule drifts silently: it did, when the
    trunk made the fingerprint credential-independent, and every normalizer
    test started reading a path that no cassette had ever been written to.
    """
    import httpx

    from app.shared.cassettes import request_fingerprint
    from app.travel.adapters.activities import SERPAPI_URL

    args_hash = request_fingerprint(httpx.Request("GET", SERPAPI_URL, params=params))
    return json.loads((CASSETTES / provider / f"{args_hash}.json").read_text())["body"]


@pytest.fixture(autouse=True)
def _fixed_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's real key out of the replayed request.

    No longer load-bearing for cassette naming — the trunk fingerprint strips
    credentials before hashing — but a real key has no business in a test.
    Deliberately sync and Redis-free: the normalizer tests read cassettes off
    disk and must stay runnable with nothing else up.
    """
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")


@pytest.fixture
async def clean_state(redis_available: bool):
    """Empty cache and quota counters around a test that reaches the adapter.

    Cache and quota are Redis-backed now, so both sides of this are `await`ed
    and every consumer is async. Requesting `redis_available` is what arms
    conftest's `_clean_redis`: it rebuilds the connection pool per test —
    pytest-asyncio hands each test a fresh event loop, and a pool cached from a
    closed one raises `RuntimeError: Event loop is closed` on first use — and
    skips loudly rather than passing vacuously when no instance answers.
    """
    await clear_cache()
    await quota_counter.reset_turn()
    yield
    await clear_cache()
    await quota_counter.reset_turn()


@pytest.fixture
def adapter() -> ActivitiesAdapter:
    return ActivitiesAdapter()


LISBON = SearchQuery(destination="Lisbon", kind="attraction", limit=10)
PORTO = SearchQuery(destination="Porto", kind="attraction", limit=10)


# ── Request shape ───────────────────────────────────────────────────────────


class TestTripadvisorRequest:
    def test_ssrc_is_things_to_do_not_all(self, adapter: ActivitiesAdapter) -> None:
        """Uppercase `A` is Things to Do; lowercase `a` is All.

        The easiest silent mistake in the engine — a lowercase `a` returns
        hotels and restaurants mixed into an attractions search.
        """
        assert _SSRC_THINGS_TO_DO == "A"
        assert adapter.tripadvisor_params(LISBON)["ssrc"] == "A"

    def test_limit_capped_near_thirty(self, adapter: ActivitiesAdapter) -> None:
        """Higher values return records holding only ids, with no error."""
        assert _TRIPADVISOR_LIMIT <= 30
        assert adapter.tripadvisor_params(LISBON)["limit"] == _TRIPADVISOR_LIMIT

    def test_experience_and_attraction_share_one_ssrc(self, adapter: ActivitiesAdapter) -> None:
        """Both kinds query ssrc=A; the split is on `place_type` in the response."""
        experience = SearchQuery(destination="Lisbon", kind="experience", limit=10)
        assert adapter.tripadvisor_params(experience)["ssrc"] == "A"

    def test_events_page_in_steps_of_ten(self, adapter: ActivitiesAdapter) -> None:
        """Google Events pages on `start`, unlike every other engine here."""
        query = SearchQuery(destination="Lisbon", kind="event", limit=10)
        assert "start" not in adapter.events_params(query)
        assert adapter.events_params(query, start=10)["start"] == 10


# ── Tripadvisor normalizer ──────────────────────────────────────────────────


class TestTripadvisorNormalizer:
    @pytest.fixture
    def places_body(self, adapter: ActivitiesAdapter) -> dict:
        return _cassette_body("tripadvisor", adapter.tripadvisor_params(LISBON))

    def test_maps_a_recorded_response_with_no_field_loss(self, places_body: dict) -> None:
        results = normalize_tripadvisor(places_body, "attraction")
        oceanario = next(r for r in results if r.external_id == "1152144")

        assert oceanario.name == "Oceanário de Lisboa"
        assert oceanario.rating == 4.5
        assert oceanario.kind == "attraction"
        assert oceanario.link.endswith("Oceanario_de_Lisboa.html")

    def test_attraction_and_product_are_routed_apart(self, places_body: dict) -> None:
        """A beach is not a bookable product and a tour is not a landmark."""
        attractions = normalize_tripadvisor(places_body, "attraction")
        experiences = normalize_tripadvisor(places_body, "experience")

        assert {r.name for r in attractions} == {
            "Oceanário de Lisboa",
            "Miradouro da Senhora do Monte",
        }
        assert {r.name for r in experiences} == {
            "Lisbon: Tram 28 Guided Ride and Old Town Walking Tour",
            "Sintra and Cascais Full-Day Small-Group Tour",
        }
        assert not {r.external_id for r in attractions} & {r.external_id for r in experiences}

    def test_product_results_link_to_the_specific_product_page(self, places_body: dict) -> None:
        """US-016: ATTRACTION_PRODUCT links land on the product, not a search page."""
        experiences = normalize_tripadvisor(places_body, "experience")
        assert experiences
        assert all("AttractionProductReview" in r.link for r in experiences)

    def test_poi_reads_attractions_and_keeps_its_own_kind(self, places_body: dict) -> None:
        pois = normalize_tripadvisor(places_body, "poi")
        assert pois
        assert all(r.kind == "poi" for r in pois)

    def test_non_activity_place_types_are_dropped(self, places_body: dict) -> None:
        """EATERY belongs to the places lane; GEO is a destination, not an item."""
        names = {r.name for r in normalize_tripadvisor(places_body, "attraction")}
        assert "Cervejaria Ramiro" not in names
        assert "Lisbon" not in names

    def test_truncated_records_are_dropped_not_emitted_half_empty(self, places_body: dict) -> None:
        """The high-`limit` trap: a record with an id and nothing else."""
        results = normalize_tripadvisor(places_body, "attraction")
        assert all(r.name and r.link for r in results)
        assert "27044991" not in {r.external_id for r in results}

    def test_every_tripadvisor_result_is_uncounted(self, places_body: dict) -> None:
        """The engine returns no price at all, and none is inferred from text."""
        for kind in ("attraction", "experience", "poi"):
            for result in normalize_tripadvisor(places_body, kind):
                assert result.price_basis == "uncounted"
                assert result.price_usd is None

    def test_locations_key_parses_instead_of_places(self, adapter: ActivitiesAdapter) -> None:
        """Some queries return `locations`; a missing `places` is not empty."""
        body = _cassette_body("tripadvisor", adapter.tripadvisor_params(PORTO))
        assert "places" not in body

        results = normalize_tripadvisor(body, "attraction")
        assert {r.name for r in results} == {"Livraria Lello", "Ponte Luís I"}

    def test_neither_key_present_returns_empty_without_crashing(self) -> None:
        assert normalize_tripadvisor({"search_metadata": {}}, "attraction") == []


# ── Google Events normalizer ────────────────────────────────────────────────


TRIP_WINDOW = (date(2027, 3, 10), date(2027, 3, 16))


class TestEventsNormalizer:
    @pytest.fixture
    def events_body(self, adapter: ActivitiesAdapter) -> dict:
        query = SearchQuery(destination="Lisbon", kind="event", limit=10)
        return _cassette_body("google_events", adapter.events_params(query))

    def test_the_cassette_still_carries_usd_priced_records(self, events_body: dict) -> None:
        """The tripwire that keeps every other events assertion honest.

        A cassette filtered down to unpriced records makes this whole class pass
        whether or not the per-item basis works — the input that exercises
        `actual` would simply never arrive. So the fixture's own shape is
        asserted rather than assumed, and the tests below read it unfiltered.
        """
        usd_priced = [
            r
            for r in events_body["events_results"]
            if r.get("extracted_price") is not None and str(r.get("price", "")).startswith("$")
        ]
        assert {r["title"] for r in usd_priced} == {
            "Fado Night at Casa de Linhares",  # in window
            "Rock in Rio Lisboa",  # priced but off-dates
        }

    def test_events_outside_the_trip_window_are_dropped(self, events_body: dict) -> None:
        """An off-dates festival is noise on the canvas.

        Read unfiltered: Rock in Rio is USD-priced *and* off-dates, so a body
        stripped of priced records would prove the window filter against an
        input that no longer contains the case.
        """
        names = {r.name for r in normalize_events(events_body, TRIP_WINDOW)}
        assert "Rock in Rio Lisboa" not in names  # Jun 20, trip ends Mar 16

    def test_year_resolves_against_the_trip_not_today(self) -> None:
        """`"Jan 2"` on a Dec 28 – Jan 4 trip belongs to the *following* year.

        Naive parsing lands it in the current year and drops a valid event.
        """
        body = {
            "events_results": [
                {
                    "title": "New Year Concert",
                    "date": {"start_date": "Jan 2", "when": "Fri 9:00 PM"},
                    "link": "https://example.com/nye",
                }
            ]
        }
        window = (date(2027, 12, 28), date(2028, 1, 4))
        assert len(normalize_events(body, window)) == 1
        # The same event against a window that never reaches January.
        assert normalize_events(body, (date(2027, 3, 1), date(2027, 3, 8))) == []

    def test_unparseable_date_is_kept_not_silently_dropped(self) -> None:
        """Upstream shapes are not guaranteed; a parse failure is not evidence
        that an event falls outside the trip."""
        body = {
            "events_results": [{"title": "Ongoing Exhibition", "link": "https://example.com/x"}]
        }
        assert len(normalize_events(body, TRIP_WINDOW)) == 1

    def test_missing_venue_and_ticket_info_do_not_crash(self, events_body: dict) -> None:
        """All three have vanished in past upstream regressions."""
        body = {"events_results": [r for r in events_body["events_results"] if "venue" not in r]}
        results = normalize_events(body, TRIP_WINDOW)
        assert [r.name for r in results] == ["Feira da Ladra Night Market"]
        # Event-level link absent — ticket_info supplied it.
        assert results[0].link.startswith("https://ticketline.sapo.pt/")

    def test_events_without_a_price_are_uncounted(self, events_body: dict) -> None:
        """No price is `uncounted` with no number — never `actual` at zero.

        `actual` with nothing behind it is worse than `uncounted`: the budget
        layer counts it as zero and reports headroom that doesn't exist.
        """
        by_name = {r.name: r for r in normalize_events(events_body, TRIP_WINDOW)}
        walking_tour = by_name["Lisbon Street Art Walking Tour"]
        assert walking_tour.price_basis == "uncounted"
        assert walking_tour.price_usd is None

    def test_non_usd_price_is_uncounted_not_converted(self, events_body: dict) -> None:
        """`extracted_price` carries no currency. A €40 ticket written into a USD
        budget line is a fabricated price, which is worse than an honest gap."""
        by_name = {r.name: r for r in normalize_events(events_body, TRIP_WINDOW)}
        benfica = by_name["Benfica vs Sporting — Liga Portugal"]
        assert benfica.price_basis == "uncounted"
        assert benfica.price_usd is None

    def test_extracted_price_sets_basis_actual_per_item(self, events_body: dict) -> None:
        """US-016 / FR-37: events are the one activity source with real numbers,
        so the basis is per item, never a per-provider constant.

        The whole recorded body goes in, priced records included — this is the
        input that raised before the type was widened, so a green run here is
        the evidence that the widening actually reached the adapter.
        """
        results = normalize_events(events_body, TRIP_WINDOW)
        by_name = {r.name: r for r in results}

        fado = by_name["Fado Night at Casa de Linhares"]
        assert fado.price_basis == "actual"
        assert fado.price_usd == Decimal("45")

        assert by_name["Lisbon Street Art Walking Tour"].price_basis == "uncounted"
        assert by_name["Benfica vs Sporting — Liga Portugal"].price_basis == "uncounted"

    def test_a_usd_priced_event_survives_a_window_that_includes_it(self, events_body: dict) -> None:
        """The second priced record, normalized rather than filtered out by dates.

        `test_extracted_price_sets_basis_actual_per_item` only ever reaches one
        priced item, because the other falls outside the March window. Widening
        the window puts Rock in Rio through the same path.
        """
        summer = (date(2027, 6, 1), date(2027, 6, 30))
        by_name = {r.name: r for r in normalize_events(events_body, summer)}

        rock_in_rio = by_name["Rock in Rio Lisboa"]
        assert rock_in_rio.price_basis == "actual"
        assert rock_in_rio.price_usd == Decimal("120")

    def test_price_basis_and_price_usd_never_disagree(self, events_body: dict) -> None:
        """The trunk validator's rule, asserted on real adapter output.

        `price_usd` is non-null if and only if the basis is `actual`. The model
        raises on a violation, so this fails as an error rather than a mismatch
        — which is the point: the adapter must not be able to construct one.
        """
        wide = (date(2027, 1, 1), date(2027, 12, 31))
        for result in normalize_events(events_body, wide):
            assert (result.price_usd is not None) == (result.price_basis == "actual")

    def test_events_carry_no_booking_request(self, events_body: dict) -> None:
        """Reference tier: no approval, no re-price, no handoff machinery."""
        for result in normalize_events(events_body, TRIP_WINDOW):
            assert not hasattr(result, "booking_request")


# ── Adapter behaviour ───────────────────────────────────────────────────────


class TestAdapter:
    def test_satisfies_the_provider_protocol(self, adapter: ActivitiesAdapter) -> None:
        assert isinstance(adapter, ProviderAdapter)

    async def test_search_replays_a_cassette_end_to_end(
        self, adapter: ActivitiesAdapter, clean_state: None
    ) -> None:
        results = await adapter.search(LISBON)
        assert isinstance(results, list)
        assert [r.name for r in results] == [
            "Oceanário de Lisboa",
            "Miradouro da Senhora do Monte",
        ]

    async def test_events_search_replays_end_to_end(
        self, adapter: ActivitiesAdapter, clean_state: None
    ) -> None:
        """Events routed to a different engine, scoped to the trip's dates.

        The trip range reaches the adapter through the flight date pair, since
        SearchQuery is trunk-owned and carries no trip-range fields of its own.
        """
        query = SearchQuery(
            destination="Lisbon",
            kind="event",
            limit=10,
            departure_date=date(2027, 3, 10),
            return_date=date(2027, 3, 16),
        )
        results = await adapter.search(query)

        assert [r.kind for r in results] == ["event"] * len(results)
        assert "Rock in Rio Lisboa" not in {r.name for r in results}  # off-dates
        # The priced record reaches the caller through cache and quota intact —
        # the normalizer tests bypass both.
        fado = next(r for r in results if r.name == "Fado Night at Casa de Linhares")
        assert (fado.price_basis, fado.price_usd) == ("actual", Decimal("45"))
        # One page: the cassette returns fewer than 10, so paging stops.
        turn_calls, _ = await quota_counter.peek("google_events")
        assert turn_calls == 1

    async def test_search_respects_the_requested_limit(
        self, adapter: ActivitiesAdapter, clean_state: None
    ) -> None:
        query = SearchQuery(destination="Lisbon", kind="attraction", limit=1)
        results = await adapter.search(query)
        assert len(results) == 1

    async def test_quota_exceeded_returns_as_a_value(
        self, adapter: ActivitiesAdapter, clean_state: None
    ) -> None:
        """Never raised — the agent reasons around it rather than crashing."""
        for _ in range(settings.quota_calls_per_turn):
            await quota_counter.increment("tripadvisor")

        result = await adapter.search(LISBON)
        assert isinstance(result, QuotaExceeded)
        assert not result  # falsy, so `if not results` reads naturally

    async def test_search_uses_the_activity_tier(
        self, adapter: ActivitiesAdapter, clean_state: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """6h. A tier is required and never defaulted — a misclassified TTL is
        invisible at runtime."""
        seen: list[TTLTier] = []
        real = __import__("app.travel.adapters.activities", fromlist=["get_or_fetch"]).get_or_fetch

        async def spy(key, tier, fetch):
            seen.append(tier)
            return await real(key, tier, fetch)

        monkeypatch.setattr("app.travel.adapters.activities.get_or_fetch", spy)
        await adapter.search(LISBON)
        assert seen == [TTLTier.ACTIVITY_SEARCH]

    async def test_second_search_is_served_from_cache(
        self, adapter: ActivitiesAdapter, clean_state: None
    ) -> None:
        """A cache hit costs no quota — the counter is the only observable.

        The cache is shared Redis now, so this reads through `peek()` rather
        than a per-process dict. That dict was the defect the trunk change
        removed: each process counted to 6 on its own.
        """
        await adapter.search(LISBON)
        calls_after_first, _ = await quota_counter.peek("tripadvisor")
        assert calls_after_first == 1

        await adapter.search(LISBON)
        calls_after_second, _ = await quota_counter.peek("tripadvisor")
        assert calls_after_second == calls_after_first

    def test_event_fan_out_is_capped_in_the_adapter(self) -> None:
        """Fan-out limits live here, never in agent instructions."""
        assert _MAX_EVENT_PAGES == 3

    async def test_unknown_kind_is_rejected(self, adapter: ActivitiesAdapter) -> None:
        with pytest.raises(ValueError, match="unknown activity kind"):
            await adapter.search(SearchQuery(destination="Lisbon", kind="hotel"))

    async def test_resolve_and_reprice_refuse(self, adapter: ActivitiesAdapter) -> None:
        """Reference tier — nothing to book, nothing to re-check."""
        with pytest.raises(NotImplementedError, match="reference tier"):
            await adapter.resolve("1152144")
        with pytest.raises(NotImplementedError, match="reference tier"):
            await adapter.reprice("1152144")
