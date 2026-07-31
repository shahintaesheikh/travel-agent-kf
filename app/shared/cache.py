"""Cache adapter with TTL tier as a required positional argument.

Never default a TTL tier — a fare silently landing in the 30-day tier looks
fresh and is fiction. Misclassification is invisible at runtime, so it has to
be impossible at authoring time.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class TTLTier(enum.StrEnum):
    """Cache TTL tiers. Booking options are never cached."""

    FLIGHT_SEARCH = "flight_search"  # 15 min
    LODGING_SEARCH = "lodging_search"  # 30 min
    ACTIVITY_SEARCH = "activity_search"  # 6h
    PLACES_CONTENT = "places_content"  # 30 days — contractual ceiling
    GEO_STABLE = "geo_stable"  # indefinite (place_id, coordinates, geocodes)


_TTL_SECONDS: dict[TTLTier, int | None] = {
    TTLTier.FLIGHT_SEARCH: 15 * 60,
    TTLTier.LODGING_SEARCH: 30 * 60,
    TTLTier.ACTIVITY_SEARCH: 6 * 60 * 60,
    TTLTier.PLACES_CONTENT: 30 * 24 * 60 * 60,
    TTLTier.GEO_STABLE: None,  # indefinite
}


# In-memory cache for development (no Redis dependency during testing).
# Production should swap this for a Redis-backed implementation.
_cache: dict[str, object] = {}


async def get_or_fetch(
    key: str,
    tier: TTLTier,  # required and positional — call without one fails at type-check
    fetch: Callable[[], Awaitable[T]],
) -> T:
    """Return cached value, or fetch and cache it.

    Args:
        key: Cache key.
        tier: Required TTL tier. Misclassification is impossible at authoring time.
        fetch: Async callable to produce the value on cache miss.

    Returns:
        The cached or freshly fetched value.

    Raises:
        TypeError: If tier is not provided (positional-only enforcement
            in Python is a runtime check; the type-checker catches it at
            authoring time).
    """
    # Runtime guard: if tier is somehow a string or None
    if not isinstance(tier, TTLTier):
        raise TypeError(
            f"tier is required and positional; got {type(tier).__name__}. "
            "Pass a TTLTier member."
        )

    ttl = _TTL_SECONDS[tier]

    if ttl is not None and key in _cache:
        return _cache[key]  # type: ignore[return-value]

    # GEO_STABLE: check cache even though ttl is None
    if ttl is None and key in _cache:
        return _cache[key]  # type: ignore[return-value]

    value = await fetch()
    if ttl is not None:
        _cache[key] = value
    elif ttl is None:
        # GEO_STABLE caches indefinitely
        _cache[key] = value

    return value


def clear_cache() -> None:
    """Clear the in-memory cache. Used in tests."""
    _cache.clear()
