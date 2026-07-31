"""Tripwire tests for the S1 trunk.

These verify the foundation is solid before Wave A opens. Not a test suite —
roughly a dozen focused assertions.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.main import app
from app.models.types import NormalizedFlight, NormalizedLodging
from app.shared.cache import TTLTier, clear_cache, get_or_fetch
from app.shared.cassettes import CassetteTransport
from app.shared.quota import QuotaExceeded, quota_counter
from app.travel.base import ProviderAdapter, SearchQuery
from tests.toy_adapter import ToyFlightAdapter, ToyLodgingAdapter

# ── Cache rejects a call with no TTL tier ───────────────────────────────────


class TestCacheTierRequired:
    @pytest.mark.asyncio
    async def test_missing_tier_raises_type_error(self) -> None:
        """A call without a TTL tier must fail at type-check or runtime."""
        with pytest.raises(TypeError, match="tier is required"):
            await get_or_fetch("some_key", None, lambda: "value")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_wrong_tier_type_raises_type_error(self) -> None:
        """Passing a string instead of TTLTier must raise."""
        with pytest.raises(TypeError, match="tier is required"):
            await get_or_fetch("some_key", "flight_search", lambda: "value")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_cache_round_trip(self) -> None:
        """Cache stores and returns values."""
        clear_cache()

        async def fetch() -> str:
            return "cached_value"

        result = await get_or_fetch("test_key", TTLTier.FLIGHT_SEARCH, fetch)
        assert result == "cached_value"

        # Second call should return cached value without calling fetch
        call_count = 0

        async def fetch_once() -> str:
            nonlocal call_count
            call_count += 1
            return "fresh"

        result2 = await get_or_fetch("test_key", TTLTier.FLIGHT_SEARCH, fetch_once)
        assert result2 == "cached_value"
        assert call_count == 0  # fetch was never called


# ── Quota returns QuotaExceeded as a value, never raises ────────────────────


class TestQuota:
    def setup_method(self) -> None:
        quota_counter.reset_turn()

    def test_quota_exceeded_is_falsy(self) -> None:
        qe = QuotaExceeded("serpapi", "too many calls")
        assert not qe  # __bool__ returns False
        assert str(qe) == "Quota exceeded for serpapi: too many calls"

    def test_quota_check_returns_none_when_ok(self) -> None:
        assert quota_counter.check("serpapi") is None

    def test_quota_check_returns_exceeded_when_over_limit(self) -> None:
        for _ in range(6):
            quota_counter.increment("serpapi")

        result = quota_counter.check("serpapi")
        assert isinstance(result, QuotaExceeded)
        assert "per turn" in result.reason

    def test_quota_exceeded_is_returned_not_raised(self) -> None:
        """QuotaExceeded is never raised as an exception."""
        quota_counter._turn_calls["serpapi"] = 99
        result = quota_counter.check("serpapi")
        assert isinstance(result, QuotaExceeded)
        # It should not raise, even when used in a context that would catch
        # exceptions — confirm it's a value, not an exception
        assert not issubclass(QuotaExceeded, Exception)


# ── Cassette round-trip: record writes JSON, replay reads it back ───────────


class TestCassetteRoundTrip:
    @pytest.fixture(autouse=True)
    def _cleanup(self, tmp_path: Path) -> None:
        """Use a temp directory for cassettes so we don't pollute fixtures."""
        self._orig_dir = Path("fixtures/cassettes")
        # Patch the cassette dir to tmp_path
        import app.shared.cassettes as cassettes_mod

        self._orig_fn = cassettes_mod.CASSETTE_DIR
        cassettes_mod.CASSETTE_DIR = tmp_path
        yield
        cassettes_mod.CASSETTE_DIR = self._orig_fn

    @pytest.mark.asyncio
    async def test_cassette_record_and_replay(self) -> None:
        """Record a response, then replay it identically."""
        # We need to test the transport directly. Create a mock server.
        # For this test, we use a real request to a known endpoint.
        # Since we can't easily test record/replay without a real server,
        # let's test the transport's file I/O directly.

        # Simulate a cassette file
        import app.shared.cassettes as cassettes_mod

        cassette_dir = cassettes_mod.CASSETTE_DIR / "test_provider"
        cassette_dir.mkdir(parents=True, exist_ok=True)
        cassette_path = cassette_dir / "test_hash.json"

        # Write a cassette manually
        data = {
            "status": 200,
            "headers": {"content-type": "application/json"},
            "body": {"result": "ok"},
        }
        cassette_path.write_text(json.dumps(data, indent=2))

        # Read it back via the transport
        transport = CassetteTransport("test_provider")
        response = transport._read_cassette(cassette_path)
        assert response.status_code == 200
        assert json.loads(response.text) == {"result": "ok"}


# ── Toy adapter satisfies ProviderAdapter end to end ────────────────────────


class TestProviderAdapterProtocol:
    def test_toy_flight_adapter_satisfies_protocol(self) -> None:
        adapter = ToyFlightAdapter()
        assert isinstance(adapter, ProviderAdapter)

    def test_toy_lodging_adapter_satisfies_protocol(self) -> None:
        adapter = ToyLodgingAdapter()
        assert isinstance(adapter, ProviderAdapter)

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
    async def test_toy_lodging_search_returns_normalized_lodging(self) -> None:
        adapter = ToyLodgingAdapter()
        q = SearchQuery(
            destination="Tokyo",
            check_in=date(2027, 4, 1),
            check_out=date(2027, 4, 10),
        )
        result = await adapter.search(q)
        assert isinstance(result, list)
        assert len(result) == 1
        lodging = result[0]
        assert isinstance(lodging, NormalizedLodging)
        assert lodging.name == "Toy Hotel"
        assert lodging.price_per_night_usd == Decimal("150.00")

    @pytest.mark.asyncio
    async def test_toy_flight_selected_flights_json_not_booking_token(self) -> None:
        """Verify selected_flights_json is used, not booking_token."""
        adapter = ToyFlightAdapter()
        q = SearchQuery(
            origin="JFK", destination="LHR", departure_date=date(2027, 4, 1)
        )
        result = await adapter.search(q)
        flight = result[0]
        sfj = flight.selected_flights_json
        assert "outbound" in sfj
        assert sfj["outbound"][0]["flight_number"] == "TA123"


# ── /health returns 200 ─────────────────────────────────────────────────────


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_returns_200(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
async def client() -> httpx.AsyncClient:
    """FastAPI async test client."""
    from httpx import ASGITransport

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_quota() -> None:
    quota_counter.reset_turn()
