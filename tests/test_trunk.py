"""Tripwire tests for the S1 trunk.

These verify the foundation is solid before Wave A opens. Not a test suite —
focused assertions on the things that fail silently.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import redis.asyncio as aioredis
from pydantic import ValidationError

from app.models.types import (
    BookingRequest,
    NormalizedActivity,
    NormalizedFlight,
    NormalizedLodging,
    Priced,
    PriceStatus,
    PriceUnitMismatch,
    price_drift,
)
from app.shared import cache as cache_mod
from app.shared import redis_client
from app.shared.cache import TTLTier, cache_key, clear_cache, get_or_fetch, ttl_seconds
from app.shared.cassettes import CassetteTransport, request_fingerprint
from app.shared.config import settings
from app.shared.quota import QuotaExceeded, quota_counter, start_turn
from app.travel.base import ProviderAdapter, SearchQuery
from tests.toy_adapter import ToyFlightAdapter, ToyLodgingAdapter

# ── Cache rejects a call with no TTL tier ───────────────────────────────────


class TestCacheTierRequired:
    async def test_missing_tier_raises_type_error(self) -> None:
        """A call without a TTL tier must fail at type-check or runtime."""
        with pytest.raises(TypeError, match="tier is required"):
            await get_or_fetch("some_key", None, lambda: "value")  # type: ignore[arg-type]

    async def test_wrong_tier_type_raises_type_error(self) -> None:
        """Passing a string instead of TTLTier must raise."""
        with pytest.raises(TypeError, match="tier is required"):
            await get_or_fetch("some_key", "flight_search", lambda: "value")  # type: ignore[arg-type]


# ── Tier TTLs match AGENTS.md §3 ────────────────────────────────────────────


class TestTierTTLs:
    def test_each_tier_maps_to_documented_ttl(self) -> None:
        assert ttl_seconds(TTLTier.FLIGHT_SEARCH) == 15 * 60
        assert ttl_seconds(TTLTier.LODGING_SEARCH) == 30 * 60
        assert ttl_seconds(TTLTier.ACTIVITY_SEARCH) == 6 * 60 * 60
        # Contractual ceiling, not a tuning knob.
        assert ttl_seconds(TTLTier.PLACES_CONTENT) == 30 * 24 * 60 * 60
        assert ttl_seconds(TTLTier.GEO_STABLE) is None

    def test_every_tier_has_a_ttl(self) -> None:
        """A new tier without a TTL entry must not slip through."""
        for tier in TTLTier:
            assert tier in cache_mod._TTL_SECONDS


# ── Cache round-trip and expiry ─────────────────────────────────────────────


class TestCacheBehaviour:
    async def test_cache_round_trip(self, redis_available: bool) -> None:
        await clear_cache()
        calls = 0

        async def fetch() -> str:
            nonlocal calls
            calls += 1
            return "cached_value"

        assert await get_or_fetch("t:round_trip", TTLTier.FLIGHT_SEARCH, fetch) == "cached_value"
        assert await get_or_fetch("t:round_trip", TTLTier.FLIGHT_SEARCH, fetch) == "cached_value"
        assert calls == 1, "second call should have been served from cache"

    async def test_entry_expires_at_tier_ttl(
        self, redis_available: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Present before the TTL, absent after.

        A 1-second tier rather than sleep(900). The original defect was that
        `_TTL_SECONDS` was read and discarded, so entries lived for the
        process lifetime — this is the test that would have caught it.
        """
        monkeypatch.setitem(cache_mod._TTL_SECONDS, TTLTier.FLIGHT_SEARCH, 1)
        calls = 0

        async def fetch() -> str:
            nonlocal calls
            calls += 1
            return f"value_{calls}"

        assert await get_or_fetch("t:expiry", TTLTier.FLIGHT_SEARCH, fetch) == "value_1"
        # Still inside the window.
        assert await get_or_fetch("t:expiry", TTLTier.FLIGHT_SEARCH, fetch) == "value_1"
        assert calls == 1

        await asyncio.sleep(1.2)

        # Expired: fetch runs again.
        assert await get_or_fetch("t:expiry", TTLTier.FLIGHT_SEARCH, fetch) == "value_2"
        assert calls == 2

    async def test_ttl_rides_on_the_write(self, redis_available: bool) -> None:
        """The stored key carries a TTL — not separate bookkeeping."""

        async def fetch() -> dict:
            return {"fare": 640}

        await get_or_fetch("t:ttl_set", TTLTier.FLIGHT_SEARCH, fetch)
        ttl = await redis_client.get_redis().ttl("cache:t:ttl_set")
        assert 0 < ttl <= 15 * 60

    async def test_geo_stable_has_no_expiry(self, redis_available: bool) -> None:
        """place_id and coordinates do not go stale."""

        async def fetch() -> dict:
            return {"place_id": "ChIJ..."}

        await get_or_fetch("t:geo", TTLTier.GEO_STABLE, fetch)
        # -1 is redis for "key exists, no TTL".
        assert await redis_client.get_redis().ttl("cache:t:geo") == -1

    async def test_pydantic_values_round_trip(self, redis_available: bool) -> None:
        """Normalized models survive the Redis hop with no field loss."""
        adapter = ToyFlightAdapter()
        flights = await adapter.search(SearchQuery(origin="JFK", destination="LHR"))

        async def fetch() -> list[NormalizedFlight]:
            return flights

        await get_or_fetch("t:models", TTLTier.FLIGHT_SEARCH, fetch)

        async def unused() -> list[NormalizedFlight]:
            raise AssertionError("should have been a cache hit")

        cached = await get_or_fetch("t:models", TTLTier.FLIGHT_SEARCH, unused)
        assert [f.model_dump() for f in cached] == [f.model_dump() for f in flights]

    async def test_cache_key_format(self) -> None:
        """US-017: {provider}:{tool}:{sha256(normalized_args)}."""
        h = cache_mod.args_hash({"b": 2, "a": 1})
        assert cache_key("serpapi", "flights", h) == f"serpapi:flights:{h}"

    async def test_arg_order_does_not_fork_the_key(self) -> None:
        assert cache_mod.args_hash({"a": 1, "b": 2}) == cache_mod.args_hash({"b": 2, "a": 1})


# ── Fail open: a dead cache is a miss, not an outage ────────────────────────


class TestCacheFailsOpen:
    async def test_unreachable_redis_still_returns_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Connection failure must fail open, not crash."""
        monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:6399/0")
        redis_client.reset_redis()
        try:

            async def fetch() -> str:
                return "fetched_anyway"

            assert await get_or_fetch("t:dead", TTLTier.FLIGHT_SEARCH, fetch) == "fetched_anyway"
        finally:
            redis_client.reset_redis()


# ── Quota is shared state, and returns a value rather than raising ─────────


class TestQuota:
    def test_quota_exceeded_is_falsy_and_not_an_exception(self) -> None:
        qe = QuotaExceeded("serpapi", "too many calls")
        assert not qe
        assert str(qe) == "Quota exceeded for serpapi: too many calls"
        assert not issubclass(QuotaExceeded, Exception)

    async def test_check_returns_none_when_under_limit(self, redis_available: bool) -> None:
        start_turn()
        assert await quota_counter.check("serpapi") is None

    async def test_check_returns_exceeded_at_the_turn_ceiling(self, redis_available: bool) -> None:
        start_turn()
        for _ in range(settings.quota_calls_per_turn):
            await quota_counter.increment("serpapi")

        result = await quota_counter.check("serpapi")
        assert isinstance(result, QuotaExceeded)
        assert "per turn" in result.reason

    async def test_trip_hour_ceiling(self, redis_available: bool) -> None:
        start_turn()
        r = redis_client.get_redis()
        key = quota_counter._hour_key("serpapi", "trip-1")
        await r.set(key, settings.quota_calls_per_trip_hour)

        result = await quota_counter.check("serpapi", trip_id="trip-1")
        assert isinstance(result, QuotaExceeded)
        assert "per trip-hour" in result.reason

    async def test_counter_is_shared_across_connections(
        self, redis_available: bool, redis_url: str
    ) -> None:
        """The defect this replaced: a module-level dict counted per process.

        Incrementing through one connection must be visible from an entirely
        separate client — which is what a second process is.
        """
        turn = start_turn()
        await quota_counter.increment("serpapi")
        await quota_counter.increment("serpapi")

        other = aioredis.from_url(redis_url, decode_responses=True)
        try:
            seen = await other.get(f"quota:turn:{turn}:serpapi")
        finally:
            await other.aclose()

        assert seen == "2", "quota counter is not shared — still per-process"

    async def test_expire_is_set_once_and_not_refreshed(self, redis_available: bool) -> None:
        """EXPIRE only when INCR returns 1.

        Re-expiring on every increment keeps a steadily-used counter alive
        forever and makes the hourly window permanent.
        """
        start_turn()
        r = redis_client.get_redis()
        await quota_counter.increment("serpapi", trip_id="trip-ttl")
        key = quota_counter._hour_key("serpapi", "trip-ttl")

        # Age the key, then increment again — the TTL must keep counting down
        # rather than resetting to a full hour.
        await r.expire(key, 100)
        await quota_counter.increment("serpapi", trip_id="trip-ttl")
        assert 0 < await r.ttl(key) <= 100

    async def test_cache_hit_does_not_consume_quota(self, redis_available: bool) -> None:
        """Count only when the request actually leaves the process.

        Otherwise the ceiling limits your own reads rather than outbound spend.
        """
        start_turn()
        await clear_cache()

        async def fetch() -> dict:
            await quota_counter.increment("serpapi", trip_id="trip-q")
            return {"fare": 640}

        key = cache_key("serpapi", "flights", cache_mod.args_hash({"o": "JFK"}))
        await get_or_fetch(key, TTLTier.FLIGHT_SEARCH, fetch)
        await get_or_fetch(key, TTLTier.FLIGHT_SEARCH, fetch)
        await get_or_fetch(key, TTLTier.FLIGHT_SEARCH, fetch)

        _turn_calls, hour_calls = await quota_counter.peek("serpapi", trip_id="trip-q")
        assert hour_calls == 1, "cache hits consumed quota"

    async def test_quota_fails_open_when_redis_is_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No ceiling beats refusing to plan a trip — but it must not raise."""
        monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:6399/0")
        redis_client.reset_redis()
        try:
            assert await quota_counter.check("serpapi") is None
            await quota_counter.increment("serpapi")  # must not raise
        finally:
            redis_client.reset_redis()


# ── Cassette hashing is credential-independent ──────────────────────────────


class TestCassetteCredentials:
    def _request(self, key: str) -> httpx.Request:
        return httpx.Request(
            "GET",
            "https://serpapi.com/search",
            params={"engine": "google_flights", "departure_id": "DXB", "api_key": key},
        )

    def test_two_keys_resolve_to_one_cassette(self) -> None:
        """Rotate the key and every hash would otherwise change for requests
        that didn't."""
        a = request_fingerprint(self._request("key_aaaaaaaa"))
        b = request_fingerprint(self._request("key_bbbbbbbb"))
        assert a == b

    def test_header_credential_ignored_too(self) -> None:
        """omkar carries its key in an `API-Key` header, not a query param."""
        base = "https://api.omkar.cloud/airbnb/search"
        one = httpx.Request("GET", base, params={"q": "Tokyo"}, headers={"API-Key": "ok_111"})
        two = httpx.Request("GET", base, params={"q": "Tokyo"}, headers={"API-Key": "ok_222"})
        assert request_fingerprint(one) == request_fingerprint(two)

    def test_param_order_does_not_fork_the_cassette(self) -> None:
        one = httpx.Request("GET", "https://serpapi.com/search?a=1&b=2")
        two = httpx.Request("GET", "https://serpapi.com/search?b=2&a=1")
        assert request_fingerprint(one) == request_fingerprint(two)

    def test_different_queries_still_differ(self) -> None:
        """Stripping credentials must not collapse genuinely distinct calls."""
        one = self._request("k")
        two = httpx.Request(
            "GET",
            "https://serpapi.com/search",
            params={"engine": "google_flights", "departure_id": "JFK", "api_key": "k"},
        )
        assert request_fingerprint(one) != request_fingerprint(two)

    def test_post_body_participates_in_the_hash(self) -> None:
        """Two different POSTs to one endpoint are two cassettes."""
        url = "https://serpapi.com/search"
        one = httpx.Request("POST", url, json={"selected_flights": ["LX243"]})
        two = httpx.Request("POST", url, json={"selected_flights": ["LX316"]})
        assert request_fingerprint(one) != request_fingerprint(two)

    def test_written_cassette_contains_no_credential(self, tmp_path: Path) -> None:
        """Cassettes are committed. Unredacted means a key in git history."""
        secret_param = "sk_live_param_secret"
        secret_header = "ok_live_header_secret"
        request = httpx.Request(
            "GET",
            "https://serpapi.com/search",
            params={"engine": "google_flights", "api_key": secret_param},
            headers={"API-Key": secret_header, "Authorization": "Bearer tok_secret"},
        )
        response = httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps({"best_flights": [], "api_key": secret_param}).encode(),
        )

        transport = CassetteTransport("test_provider")
        path = tmp_path / "cassette.json"
        transport._write_cassette(path, request, response)

        written = path.read_text()
        for secret in (secret_param, secret_header, "tok_secret"):
            assert secret not in written, f"{secret!r} leaked into the cassette"
        # The shape is still there for debugging.
        assert "<REDACTED>" in written
        assert "google_flights" in written


class TestCassetteRoundTrip:
    @pytest.fixture(autouse=True)
    def _tmp_cassette_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("app.shared.cassettes.CASSETTE_DIR", tmp_path)

    def test_record_then_replay(self, tmp_path: Path) -> None:
        transport = CassetteTransport("test_provider")
        request = httpx.Request("GET", "https://serpapi.com/search?engine=x&api_key=k")
        response = httpx.Response(
            200, headers={"content-type": "application/json"}, content=b'{"result": "ok"}'
        )

        path = tmp_path / "test_provider" / f"{request_fingerprint(request)}.json"
        transport._write_cassette(path, request, response)

        # A different key must land on the same cassette.
        rotated = httpx.Request("GET", "https://serpapi.com/search?engine=x&api_key=rotated")
        replayed = transport._read_cassette(
            tmp_path / "test_provider" / f"{request_fingerprint(rotated)}.json"
        )
        assert replayed.status_code == 200
        assert json.loads(replayed.text) == {"result": "ok"}

    def test_missing_cassette_is_a_clear_error(self, tmp_path: Path) -> None:
        transport = CassetteTransport("test_provider")
        with pytest.raises(FileNotFoundError, match="PROVIDER_MODE=record"):
            transport._read_cassette(tmp_path / "nope.json")


# ── Priced is frozen and expresses three outcomes ──────────────────────────


class TestPriced:
    def _priced(self, **kw) -> Priced:
        fields = {
            "ref": "LX243/2027-03-10",
            "status": PriceStatus.available,
            "price_usd": Decimal("640.00"),
            "price_unit": "total",
            "observed_at": datetime.now(UTC),
        }
        return Priced(**{**fields, **kw})

    def test_is_frozen(self) -> None:
        """The money path compares this against a stored price; a mutated copy
        would silently change that decision."""
        priced = self._priced()
        with pytest.raises(ValidationError):
            priced.price_usd = Decimal("1.00")  # type: ignore[misc]

    def test_unavailable_carries_no_price(self) -> None:
        gone = Priced(
            ref="LX243/2027-03-10",
            status=PriceStatus.unavailable,
            price_unit="total",
            observed_at=datetime.now(UTC),
            unavailable_reason="itinerary no longer sold",
        )
        assert gone.price_usd is None
        assert gone.status is PriceStatus.unavailable

    def test_split_options_is_distinct_from_unavailable(self) -> None:
        """Two bookings required is not the same as sold out — the itinerary
        exists and is priced, it just cannot be one handoff."""
        split = self._priced(status=PriceStatus.split_options_only)
        assert split.status is not PriceStatus.unavailable
        assert split.price_usd is not None
        assert split.status is not PriceStatus.available

    def test_booking_request_is_optional(self) -> None:
        assert self._priced().booking_request is None
        with_req = self._priced(
            booking_request=BookingRequest(
                url="https://www.google.com/travel/clk/f",
                post_data={"opaque": "payload"},
                vendor="swiss",
            )
        )
        assert with_req.booking_request is not None
        assert with_req.booking_request.post_data == {"opaque": "payload"}

    def test_observed_at_is_utc_aware(self) -> None:
        """System timestamps are UTC-aware — AGENTS.md §2."""
        assert self._priced().observed_at.tzinfo is not None

    def test_price_unit_is_required(self) -> None:
        """No default. An unlabelled price is the defect — flights store a
        total and lodging per-night, and one field name cannot silently mean
        both."""
        with pytest.raises(ValidationError):
            Priced(
                ref="tokyo-hotel-1",
                status=PriceStatus.available,
                price_usd=Decimal("150.00"),
                observed_at=datetime.now(UTC),
            )

    def test_price_unit_is_required_even_when_unavailable(self) -> None:
        """The unit is knowable whether or not a price came back. Making it
        conditional reintroduces the defect on the path that matters most."""
        with pytest.raises(ValidationError):
            Priced(
                ref="tokyo-hotel-1",
                status=PriceStatus.unavailable,
                observed_at=datetime.now(UTC),
                unavailable_reason="listing withdrawn",
            )


# ── Re-price refuses to compare across units ────────────────────────────────


class TestPriceDrift:
    def _priced(self, unit: str, price: str = "150.00", **kw) -> Priced:
        fields = {
            "ref": "tokyo-hotel-1",
            "status": PriceStatus.available,
            "price_usd": Decimal(price),
            "price_unit": unit,
            "observed_at": datetime.now(UTC),
        }
        return Priced(**{**fields, **kw})

    def test_per_night_against_a_stay_total_raises(self) -> None:
        """The defect this exists for: a 4-night stay stored at $150/night,
        re-priced against the $600 total, computes a 300% "move" and marks a
        perfectly fine listing stale on the first check."""
        with pytest.raises(PriceUnitMismatch, match="per_night"):
            price_drift(self._priced("per_night"), Decimal("600.00"), "total")

    def test_total_against_a_per_night_price_raises(self) -> None:
        """Mismatch in either direction — the flight side is just as wrong."""
        with pytest.raises(PriceUnitMismatch):
            price_drift(self._priced("total", "640.00"), Decimal("160.00"), "per_night")

    def test_matching_units_compute_signed_drift(self) -> None:
        rose = price_drift(self._priced("per_night", "165.00"), Decimal("150.00"), "per_night")
        assert rose == Decimal("0.1")
        fell = price_drift(self._priced("per_night", "135.00"), Decimal("150.00"), "per_night")
        assert fell < 0

    def test_unchanged_price_has_zero_drift(self) -> None:
        assert price_drift(self._priced("total", "640.00"), Decimal("640.00"), "total") == 0

    def test_unavailable_has_no_price_to_compare(self) -> None:
        """Check `status` before comparing — `unavailable` is not a price of
        zero, and a caller reaching here has skipped the gate."""
        gone = Priced(
            ref="tokyo-hotel-1",
            status=PriceStatus.unavailable,
            price_unit="per_night",
            observed_at=datetime.now(UTC),
            unavailable_reason="listing withdrawn",
        )
        with pytest.raises(ValueError, match="no price"):
            price_drift(gone, Decimal("150.00"), "per_night")

    def test_mismatch_is_raised_not_returned(self) -> None:
        """Unlike QuotaExceeded, which is an expected outcome returned as a
        value. A cross-unit comparison is a caller bug whose result looks
        entirely plausible — silence is the whole hazard."""
        assert issubclass(PriceUnitMismatch, Exception)


# ── NormalizedActivity prices events without fabricating one ────────────────


class TestNormalizedActivityPricing:
    def _activity(self, **kw) -> NormalizedActivity:
        fields = {
            "external_id": "ev-1",
            "name": "Yayoi Kusama at Mori Art Museum",
            "rating": None,
            "kind": "event",
            "link": "https://example.com/events/ev-1",
        }
        return NormalizedActivity(**{**fields, **kw})

    def test_priced_event_normalizes_without_raising(self) -> None:
        """F8, the live crash: a Google Events record carrying
        `extracted_price` used to fail validation against a single-value
        Literal, so a real search returning a $45 ticket crashed the adapter."""
        event = self._activity(price_basis="actual", price_usd=Decimal("45.00"))
        assert event.price_basis == "actual"
        assert event.price_usd == Decimal("45.00")

    def test_unpriced_event_is_uncounted_not_zero(self) -> None:
        """An event without `extracted_price` is uncounted. Zero would be a
        fabricated price that the budget silently believes."""
        event = self._activity()
        assert event.price_basis == "uncounted"
        assert event.price_usd is None

    def test_tripadvisor_defaults_to_uncounted(self) -> None:
        """The engine returns no price at all for experiences or attractions,
        and must never be given one — FR-37 excludes them from the budget."""
        experience = self._activity(external_id="ta-1", kind="experience", rating=4.5)
        assert experience.price_basis == "uncounted"
        assert experience.price_usd is None

    def test_price_without_actual_basis_is_rejected(self) -> None:
        """Non-null price ⟺ `actual`, enforced by the model rather than by
        convention. An uncounted item is excluded from the budget, so a price
        here would be counted by nobody and trusted by somebody."""
        with pytest.raises(ValidationError):
            self._activity(price_basis="uncounted", price_usd=Decimal("45.00"))

    def test_actual_basis_without_a_price_is_rejected(self) -> None:
        """The other half of the biconditional. `actual` means a real observed
        price, not a missing one."""
        with pytest.raises(ValidationError):
            self._activity(price_basis="actual")

    def test_price_level_estimate_is_not_an_activity_basis(self) -> None:
        """It belongs to NormalizedPlace.price_level; activities have no path
        to it."""
        with pytest.raises(ValidationError):
            self._activity(price_basis="price_level_estimate", price_usd=Decimal("45.00"))


# ── Toy adapter satisfies ProviderAdapter end to end ────────────────────────


class TestProviderAdapterProtocol:
    def test_toy_flight_adapter_satisfies_protocol(self) -> None:
        assert isinstance(ToyFlightAdapter(), ProviderAdapter)

    def test_toy_lodging_adapter_satisfies_protocol(self) -> None:
        assert isinstance(ToyLodgingAdapter(), ProviderAdapter)

    async def test_toy_flight_search_returns_normalized_flights(self) -> None:
        adapter = ToyFlightAdapter()
        q = SearchQuery(
            origin="JFK",
            destination="LHR",
            departure_date=date(2027, 4, 1),
            return_date=date(2027, 4, 10),
        )
        result = await adapter.search(q)
        assert isinstance(result, list)
        assert len(result) == 1
        flight = result[0]
        assert isinstance(flight, NormalizedFlight)
        assert flight.carrier == "ToyAir"
        assert flight.price_usd == Decimal("299.00")
        assert flight.booking_request is not None
        assert flight.booking_request.vendor == "toyair"

    async def test_toy_lodging_search_returns_normalized_lodging(self) -> None:
        adapter = ToyLodgingAdapter()
        q = SearchQuery(destination="Tokyo", check_in=date(2027, 4, 1), check_out=date(2027, 4, 10))
        result = await adapter.search(q)
        assert isinstance(result, list)
        lodging = result[0]
        assert isinstance(lodging, NormalizedLodging)
        assert lodging.name == "Toy Hotel"
        assert lodging.price_per_night_usd == Decimal("150.00")

    async def test_toy_flight_reprice_returns_priced_in_totals(self) -> None:
        """F10: the toy adapter is the reference example later lanes copy
        from, and it declared `-> dict` after the Protocol was typed against
        `Priced`. `runtime_checkable` only checks method presence, so nothing
        failed — a wrong example propagates silently."""
        priced = await ToyFlightAdapter().reprice("TA123/2027-04-01")
        assert isinstance(priced, Priced)
        assert priced.price_unit == "total"
        assert priced.ref == "TA123/2027-04-01"
        assert priced.observed_at.tzinfo is not None

    async def test_toy_lodging_reprice_matches_the_stored_price_unit(self) -> None:
        """Lodging stores per-night, so its re-price must too — comparable to
        `NormalizedLodging.price_per_night_usd` without conversion."""
        adapter = ToyLodgingAdapter()
        lodging = (await adapter.search(SearchQuery(destination="Tokyo")))[0]
        priced = await adapter.reprice(lodging.property_id)

        assert isinstance(priced, Priced)
        assert priced.price_unit == "per_night"
        assert price_drift(priced, lodging.price_per_night_usd, "per_night") == 0

    async def test_selected_flights_json_not_booking_token(self) -> None:
        """Persist flight numbers and dates — durable and replayable. Not an
        opaque token that expires."""
        adapter = ToyFlightAdapter()
        result = await adapter.search(
            SearchQuery(origin="JFK", destination="LHR", departure_date=date(2027, 4, 1))
        )
        sfj = result[0].selected_flights_json
        assert "outbound" in sfj
        assert sfj["outbound"][0]["flight_number"] == "TA123"


# ── /health checks its dependencies, not a literal ──────────────────────────


class TestHealthEndpoint:
    async def test_healthy_reports_each_dependency(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.api.health._check_postgres", lambda: _true())
        monkeypatch.setattr("app.api.health._check_redis", lambda: _true())

        response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "checks": {"postgres": True, "redis": True},
        }

    async def test_degraded_names_the_culprit_and_returns_503(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare literal reports healthy while Postgres is unreachable —
        the one moment the check exists for."""
        monkeypatch.setattr("app.api.health._check_postgres", lambda: _true())
        monkeypatch.setattr("app.api.health._check_redis", lambda: _false())

        response = await client.get("/api/health")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["checks"] == {"postgres": True, "redis": False}

    async def test_unreachable_dependency_does_not_500(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An endpoint that 500s during an outage is indistinguishable from an
        endpoint that is itself broken.

        Breaks the real dependency rather than the check function, so the
        check's own error handling is what's under test.
        """

        def _boom(*args, **kwargs):
            raise ConnectionRefusedError("nothing listening")

        monkeypatch.setattr("app.api.health.async_session_maker", _boom)
        monkeypatch.setattr("app.api.health._check_redis", lambda: _true())

        response = await client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["checks"]["postgres"] is False


async def _true() -> bool:
    return True


async def _false() -> bool:
    return False
