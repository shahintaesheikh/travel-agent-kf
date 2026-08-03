"""Cache adapter with TTL tier as a required positional argument.

Never default a TTL tier — a fare silently landing in the 30-day tier looks
fresh and is fiction. Misclassification is invisible at runtime, so it has to
be impossible at authoring time.

Backed by Redis, not a process-local dict. The web service and the arq worker
are separate processes; a dict lives in one heap and the other cannot see it.
**The TTL is the storage call** (`SET ... EX`), never separate bookkeeping —
that is what makes expiry impossible to forget.

Quota is deliberately *not* touched here. A cache hit must not count against
the ceiling, or the ceiling limits your own reads rather than outbound spend.
Adapters put the quota increment inside the `fetch` closure, which runs only
on a miss:

    async def fetch() -> dict:
        await quota_counter.increment("serpapi", trip_id)   # only on a miss
        return await client.get(...)

    data = await get_or_fetch(cache_key("serpapi", "flights", h),
                              TTLTier.FLIGHT_SEARCH, fetch)
"""

from __future__ import annotations

import enum
import hashlib
import importlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from app.shared.redis_client import RedisUnavailable, get_redis

log = logging.getLogger(__name__)

T = TypeVar("T")

KEY_PREFIX = "cache"


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


def ttl_seconds(tier: TTLTier) -> int | None:
    """TTL for a tier in seconds; None for the indefinite tier."""
    return _TTL_SECONDS[tier]


# ── Keys ────────────────────────────────────────────────────────────────────


def args_hash(args: dict[str, Any]) -> str:
    """sha256 over normalized arguments (US-017).

    Sorted and separator-canonical, so `{"a":1,"b":2}` and `{"b":2,"a":1}`
    produce one key rather than two.
    """
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def cache_key(provider: str, tool: str, hashed_args: str) -> str:
    """`{provider}:{tool}:{sha256(normalized_args)}` — US-017 key format.

    `get_or_fetch` adds the `cache:` prefix that separates this namespace from
    `quota:` and arq's in the one Key Value instance.
    """
    return f"{provider}:{tool}:{hashed_args}"


# ── Serialization ───────────────────────────────────────────────────────────
#
# Redis stores bytes, so values are JSON-encoded in a small envelope. Pydantic
# models round-trip by qualified name; reconstruction is restricted to
# `app.models` so a poisoned cache entry can't name an arbitrary import target.

_MODEL_ROOT = "app.models"


def _model_path(cls: type[BaseModel]) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def _load_model(path: str) -> type[BaseModel]:
    module_name, _, qualname = path.partition(":")
    if module_name != _MODEL_ROOT and not module_name.startswith(_MODEL_ROOT + "."):
        raise ValueError(f"refusing to load {path!r}: outside {_MODEL_ROOT}")
    obj: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    if not (isinstance(obj, type) and issubclass(obj, BaseModel)):
        raise ValueError(f"{path!r} is not a pydantic model")
    return obj


def _encode(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        env = {"model": _model_path(type(value)), "data": value.model_dump(mode="json")}
    elif (
        isinstance(value, list)
        and value
        and all(isinstance(v, BaseModel) for v in value)
        and len({type(v) for v in value}) == 1
    ):
        env = {
            "model_list": _model_path(type(value[0])),
            "data": [v.model_dump(mode="json") for v in value],
        }
    else:
        env = {"json": True, "data": value}
    return json.dumps(env, separators=(",", ":"), default=str).encode()


def _decode(raw: bytes) -> Any:
    env = json.loads(raw)
    if "model" in env:
        return _load_model(env["model"]).model_validate(env["data"])
    if "model_list" in env:
        cls = _load_model(env["model_list"])
        return [cls.model_validate(item) for item in env["data"]]
    return env["data"]


# ── get_or_fetch ────────────────────────────────────────────────────────────


async def get_or_fetch(
    key: str,
    tier: TTLTier,  # required and positional — a call without one fails at type-check
    fetch: Callable[[], Awaitable[T]],
) -> T:
    """Return the cached value, or fetch and cache it.

    Args:
        key: Cache key, conventionally from `cache_key()`. Prefixed with
            `cache:` before it reaches Redis.
        tier: Required TTL tier. Misclassification is impossible at authoring
            time; the TTL rides on the write itself.
        fetch: Async callable producing the value on a miss. Adapters put the
            quota increment in here so hits don't count against the ceiling.

    Returns:
        The cached or freshly fetched value.

    Raises:
        TypeError: If `tier` is not a `TTLTier`.
    """
    # Runtime guard behind the type-checker: catches a string or None slipping
    # through from an untyped call site.
    if not isinstance(tier, TTLTier):
        raise TypeError(
            f"tier is required and positional; got {type(tier).__name__}. Pass a TTLTier member."
        )

    ttl = _TTL_SECONDS[tier]
    redis_key = f"{KEY_PREFIX}:{key}"

    # A dead cache is a miss. Debug level — unremarkable and self-correcting.
    try:
        raw = await get_redis().get(redis_key)
        if raw is not None:
            return _decode(raw)
    except RedisUnavailable as exc:
        log.debug("cache read failed, treating as miss: %s: %s", redis_key, exc)
    except (ValueError, json.JSONDecodeError) as exc:
        # A stored entry we can no longer decode — a model changed shape since
        # it was written. Treat as a miss and let the write below replace it.
        log.debug("cache entry undecodable, treating as miss: %s: %s", redis_key, exc)

    value = await fetch()

    try:
        payload = _encode(value)
        if ttl is None:
            # GEO_STABLE only: place_ids and coordinates do not go stale.
            # Every other tier goes through SET ... EX so the TTL cannot be
            # forgotten in separate bookkeeping.
            await get_redis().set(redis_key, payload)
        else:
            await get_redis().set(redis_key, payload, ex=ttl)
    except RedisUnavailable as exc:
        log.debug("cache write failed, value not cached: %s: %s", redis_key, exc)
    except (TypeError, ValueError) as exc:
        log.debug("cache value not serializable, not cached: %s: %s", redis_key, exc)

    return value


async def clear_cache() -> None:
    """Delete every `cache:` key. Used in tests.

    Scans rather than FLUSHDB so quota counters and arq's namespace survive.
    """
    try:
        r = get_redis()
        async for k in r.scan_iter(match=f"{KEY_PREFIX}:*", count=500):
            await r.delete(k)
    except RedisUnavailable as exc:
        log.debug("cache clear failed: %s", exc)
