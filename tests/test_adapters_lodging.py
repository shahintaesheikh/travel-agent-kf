"""Tripwires for the lodging adapters — US-013 (hotels), US-014 (Airbnb).

Normalizers run against cassettes: free, deterministic, and the only place the
provider traps are actually checkable. Every trap covered here produces
*plausible wrong data* rather than an error, which is why it gets a test.

Cassettes are hand-authored from the S0.1 spike observations (PRD Appendix D)
and the provider skills — recording live costs money and needs approval
(AGENTS.md §0). See fixtures/cassettes/*/README.md.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.models.types import NormalizedLodging, Priced, PriceStatus
from app.shared.cache import TTLTier, clear_cache
from app.shared.quota import QuotaExceeded, quota_counter
from app.travel.adapters import airbnb as airbnb_mod
from app.travel.adapters import hotels as hotels_mod
from app.travel.adapters.airbnb import AirbnbAdapter, AirbnbAvailability
from app.travel.adapters.hotels import HotelsAdapter, LodgingProviderError
from app.travel.base import ProviderAdapter, SearchQuery

CHECK_IN = date(2027, 9, 10)
CHECK_OUT = date(2027, 9, 14)
NIGHTS = 4


@pytest.fixture(autouse=True)
async def _isolate(redis_available: bool) -> None:
    """Cache and quota are Redis-backed and shared across processes.

    Requesting `redis_available` does two things: it skips rather than passes
    vacuously when no instance is up (quota fails open, so a ceiling test would
    silently assert nothing), and it puts the name in the item's fixture
    closure, which is what makes conftest's `_clean_redis` rebuild the
    connection pool for this test's event loop.
    """
    await clear_cache()
    await quota_counter.reset_turn()


def lodging_query(**overrides: object) -> SearchQuery:
    kwargs: dict = {
        "destination": "Tokyo",
        "check_in": CHECK_IN,
        "check_out": CHECK_OUT,
        "guests": 2,
    }
    kwargs.update(overrides)
    return SearchQuery(**kwargs)  # type: ignore[arg-type]


class _ExplodingClient:
    """Stand-in for a provider that is simply down."""

    async def __aenter__(self) -> _ExplodingClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, *_: object, **__: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")


# ── Hotels ──────────────────────────────────────────────────────────────────


@pytest.mark.normalizer
class TestHotelsNormalizer:
    async def test_maps_cassette_to_typed_output_with_no_field_loss(self) -> None:
        results = await HotelsAdapter().search(lodging_query())

        assert isinstance(results, list)
        assert [r.name for r in results] == ["Hotel Alpha Tokyo", "Hotel Beta Ginza"]

        alpha = results[0]
        assert isinstance(alpha, NormalizedLodging)
        assert alpha.property_id == "TOKEN_A"
        assert alpha.rating == 4.44  # overall_rating arrives as 4.4355693
        assert alpha.price_per_night_usd == Decimal("275.00")
        assert alpha.booking_request.vendor == "google_hotels"
        assert alpha.booking_request.url.startswith("https://hotel-alpha.example/")
        assert alpha.booking_request.post_data is None
        assert alpha.cancellation_terms == "Free cancellation until 2027-09-08"
        assert alpha.observed_at is not None

    async def test_ads_are_never_merged_into_properties(self) -> None:
        """Trap 1: sponsored results are a different array with a different shape."""
        results = await HotelsAdapter().search(lodging_query())

        assert isinstance(results, list)
        assert all("Sponsored" not in r.name for r in results)
        assert all("aclk" not in r.booking_request.url for r in results)

    async def test_extracted_numerics_used_not_currency_strings(self) -> None:
        """Trap 3: `lowest`, not `before_taxes_fees`; and never the "$275" form."""
        results = await HotelsAdapter().search(lodging_query())

        assert isinstance(results, list)
        alpha, beta = results
        assert alpha.price_per_night_usd == Decimal("275.00")  # not 229 before taxes
        # Beta has no rate_per_night at all — derived from the stay total.
        assert beta.price_per_night_usd == Decimal("800.00") / NIGHTS

    async def test_single_property_response_is_parsed(self) -> None:
        """Trap 2: an exact-name query returns details at the top level."""
        results = await HotelsAdapter().search(lodging_query(query="Park Hyatt Tokyo"))

        assert isinstance(results, list)
        assert len(results) == 1
        assert results[0].name == "Park Hyatt Tokyo"
        assert results[0].price_per_night_usd == Decimal("620.00")

    async def test_property_without_price_or_link_is_skipped_not_fabricated(self) -> None:
        """An invented estimate is worse than an honest gap."""
        results = await HotelsAdapter().search(lodging_query())

        assert isinstance(results, list)
        names = [r.name for r in results]
        assert "Hotel Gamma Asakusa" not in names  # no price anywhere
        assert "Hotel Delta Ueno" not in names  # no link to hand off to

    async def test_missing_dates_returns_structured_error(self) -> None:
        """The engine errors without dates; we never spend the call finding out."""
        result = await HotelsAdapter().search(SearchQuery(destination="Tokyo"))

        assert isinstance(result, LodgingProviderError)
        assert result.code == "missing_dates"
        assert not result  # falsy, like QuotaExceeded

    async def test_deep_link_precision_is_recorded_as_unmet(self) -> None:
        """Hotels land on a property page, not a dated checkout. See US-013 notes."""
        assert HotelsAdapter.deep_link_precision is False
        assert AirbnbAdapter.deep_link_precision is True

    @pytest.mark.money
    async def test_reprice_returns_priced_per_night_for_the_stay(self) -> None:
        """`price_usd` is per night — the same basis as the stored price it is
        compared against. A stay total here would read as a 4x price move."""
        adapter = HotelsAdapter()
        ref = hotels_mod.make_ref("TOKEN_A", CHECK_IN, CHECK_OUT, 2)

        result = await adapter.reprice(ref)

        assert isinstance(result, Priced)
        assert result.ref == ref
        assert result.status is PriceStatus.available
        assert result.price_usd == Decimal("289.00")
        assert result.price_usd != Decimal("1156.00")  # the stay total
        assert result.observed_at.tzinfo is not None  # system timestamp, UTC-aware
        # No per-vendor booking URL exists; the caller falls back to the stored one.
        assert result.booking_request is None

    async def test_resolve_returns_the_property_link(self) -> None:
        result = await HotelsAdapter().resolve(
            hotels_mod.make_ref("TOKEN_A", CHECK_IN, CHECK_OUT, 2)
        )

        assert not isinstance(result, (LodgingProviderError, QuotaExceeded))
        assert result.vendor == "google_hotels"
        assert result.post_data is None  # nothing to POST; hotels have no options step


# ── Airbnb ──────────────────────────────────────────────────────────────────


@pytest.mark.normalizer
class TestAirbnbNormalizer:
    async def test_maps_cassette_to_typed_output_with_no_field_loss(self) -> None:
        results = await AirbnbAdapter().search(lodging_query())

        assert isinstance(results, list)
        assert [r.property_id for r in results] == ["1447073736891652317", "222", "444"]

        loft = results[0]
        assert loft.name == "Sunny loft in Shibuya"
        assert loft.rating == 4.87
        assert loft.cancellation_terms == "better_strict_with_grace_period"
        assert loft.booking_request.vendor == "airbnb"
        assert loft.observed_at is not None

    async def test_per_night_derived_from_breakdown_never_from_the_label(self) -> None:
        """`nightly_rate` was null; the label says $99.99 and the label is display text."""
        results = await AirbnbAdapter().search(lodging_query())

        assert isinstance(results, list)
        loft = results[0]
        assert loft.price_per_night_usd == Decimal("57.42")  # 229.68 / 4 nights
        assert loft.price_per_night_usd != Decimal("99.99")  # the label's number

    async def test_per_night_derived_from_the_mislabeled_stay_total(self) -> None:
        """`nightly_rate: 331` is a 4-night total. Read as-is it is 4x too high."""
        results = await AirbnbAdapter().search(lodging_query())

        assert isinstance(results, list)
        machiya = results[1]
        assert machiya.price_per_night_usd == Decimal("82.75")  # 331 / 4
        assert machiya.price_per_night_usd != Decimal("331.00")

    async def test_booking_url_with_dates_and_guests_is_captured(self) -> None:
        """The dated booking_url is the entire reason this provider is in the stack."""
        results = await AirbnbAdapter().search(lodging_query())

        assert isinstance(results, list)
        url = results[0].booking_request.url
        assert "check_in=2027-09-10" in url
        assert "check_out=2027-09-14" in url
        assert "adults=2" in url

    async def test_listing_without_price_is_skipped(self) -> None:
        results = await AirbnbAdapter().search(lodging_query())

        assert isinstance(results, list)
        assert "333" not in [r.property_id for r in results]

    async def test_past_check_in_is_rejected_before_any_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The API answers past dates with total_results: 0 and no error at all."""

        def _no_calls(*_: object, **__: object) -> None:
            raise AssertionError("provider was called with a past check_in date")

        monkeypatch.setattr(airbnb_mod, "make_client", _no_calls)

        result = await AirbnbAdapter().search(
            lodging_query(check_in=date(2020, 1, 1), check_out=date(2020, 1, 5))
        )

        assert isinstance(result, LodgingProviderError)
        assert result.code == "past_check_in"

    async def test_dated_details_500_falls_back_to_undated(self) -> None:
        """Appendix D: the dated details path 500s. Expected failure, not an exception."""
        ref = airbnb_mod.make_ref("1447073736891652317", CHECK_IN, CHECK_OUT, 2)

        result = await AirbnbAdapter().availability(ref)

        assert isinstance(result, AirbnbAvailability)
        assert result.dates_applied is False
        assert result.is_available is None  # unknown for these dates, not "yes"
        assert result.per_night_usd is None  # undated pricing is not this stay's price
        assert result.stay_total_usd == Decimal("339.30")
        assert result.host_id == "12345"  # string here, integer on search
        assert result.cancellation_terms is not None
        assert "Free cancellation before Sep 5" in result.cancellation_terms

    async def test_unavailable_listing_is_surfaced_with_its_reason(self) -> None:
        """ "Minimum 5 nights" is useful to someone planning Thursday to Sunday."""
        result = await AirbnbAdapter().availability(
            airbnb_mod.make_ref("222", CHECK_IN, CHECK_OUT, 2)
        )

        assert isinstance(result, AirbnbAvailability)
        assert result.dates_applied is True
        assert result.is_available is False
        assert result.unavailability_reason == "Minimum stay of 5 nights"
        assert result.per_night_usd == Decimal("90.36")  # 361.43 / 4, rounded

    @pytest.mark.money
    async def test_reprice_on_an_undated_fallback_is_a_failed_reprice(self) -> None:
        """The money path must not read base pricing as a fresh price for the stay.

        There is no honest `Priced` here: `available` would publish a number
        for the wrong dates, `unavailable` would cancel a stay that is fine.
        """
        result = await AirbnbAdapter().reprice(
            airbnb_mod.make_ref("1447073736891652317", CHECK_IN, CHECK_OUT, 2)
        )

        assert isinstance(result, LodgingProviderError)
        assert result.code == "reprice_dates_not_applied"
        assert not isinstance(result, Priced)  # nothing for a handoff to read

    @pytest.mark.money
    async def test_reprice_reports_an_unavailable_listing_with_no_price(self) -> None:
        """`price_usd` is None when the listing is gone — never the approved figure."""
        ref = airbnb_mod.make_ref("222", CHECK_IN, CHECK_OUT, 2)

        result = await AirbnbAdapter().reprice(ref)

        assert isinstance(result, Priced)
        assert result.ref == ref
        assert result.status is PriceStatus.unavailable
        assert result.price_usd is None
        assert result.unavailable_reason == "Minimum stay of 5 nights"
        assert result.booking_request is None  # search-time dated URL is the valid one

    async def test_provider_failure_returns_a_structured_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any omkar failure degrades the app to hotels-only. It never raises."""
        monkeypatch.setattr(airbnb_mod, "make_client", lambda _: _ExplodingClient())

        result = await AirbnbAdapter().search(lodging_query())

        assert isinstance(result, LodgingProviderError)
        assert result.code == "transport_error"
        assert not result

    async def test_resolve_points_at_the_persisted_booking_request(self) -> None:
        """No endpoint returns booking_url; search captured it and it was persisted."""
        result = await AirbnbAdapter().resolve(
            airbnb_mod.make_ref("1447073736891652317", CHECK_IN, CHECK_OUT, 2)
        )

        assert isinstance(result, LodgingProviderError)
        assert result.code == "use_persisted_booking_request"


# ── Cross-cutting adapter contract ──────────────────────────────────────────


class TestLodgingAdapterContract:
    def test_both_adapters_satisfy_the_provider_adapter_protocol(self) -> None:
        assert isinstance(HotelsAdapter(), ProviderAdapter)
        assert isinstance(AirbnbAdapter(), ProviderAdapter)

    @pytest.mark.parametrize(
        ("module", "adapter_cls"),
        [(hotels_mod, HotelsAdapter), (airbnb_mod, AirbnbAdapter)],
    )
    async def test_search_caches_on_the_lodging_tier(
        self, monkeypatch: pytest.MonkeyPatch, module: object, adapter_cls: type
    ) -> None:
        """30 minutes — US-013 and US-014. The tier is required and positional."""
        seen: list[TTLTier] = []
        real = module.get_or_fetch  # type: ignore[attr-defined]

        async def spy(key, tier, fetch):  # noqa: ANN001
            seen.append(tier)
            return await real(key, tier, fetch)

        monkeypatch.setattr(module, "get_or_fetch", spy)

        await adapter_cls().search(lodging_query())

        assert seen == [TTLTier.LODGING_SEARCH]

    async def test_booking_resolution_is_never_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Booking options are resolved at approval and consumed immediately."""

        def _no_cache(*_: object, **__: object) -> None:
            raise AssertionError("a booking resolution step went through the cache")

        monkeypatch.setattr(hotels_mod, "get_or_fetch", _no_cache)
        monkeypatch.setattr(airbnb_mod, "get_or_fetch", _no_cache)

        await HotelsAdapter().resolve(hotels_mod.make_ref("TOKEN_A", CHECK_IN, CHECK_OUT, 2))
        await AirbnbAdapter().availability(
            airbnb_mod.make_ref("1447073736891652317", CHECK_IN, CHECK_OUT, 2)
        )

    @pytest.mark.parametrize("adapter_cls", [HotelsAdapter, AirbnbAdapter])
    async def test_quota_exceeded_is_returned_as_a_value_never_raised(
        self, adapter_cls: type
    ) -> None:
        adapter = adapter_cls()
        for _ in range(6):
            await quota_counter.increment(adapter.provider)

        result = await adapter.search(lodging_query())

        assert isinstance(result, QuotaExceeded)
        assert not result

    @pytest.mark.parametrize("adapter_cls", [HotelsAdapter, AirbnbAdapter])
    async def test_a_failed_search_does_not_occupy_the_cache(self, adapter_cls: type) -> None:
        """A cached failure would be served for the full 30 minutes and never retried."""
        adapter = adapter_cls()
        for _ in range(6):
            await quota_counter.increment(adapter.provider)
        assert isinstance(await adapter.search(lodging_query()), QuotaExceeded)

        await quota_counter.reset_turn()  # next agent turn

        assert isinstance(await adapter.search(lodging_query()), list)
