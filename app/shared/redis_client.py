"""Single Redis connection pool, shared by cache, quota and /health.

One Key Value instance backs all three. Key prefixes keep them apart —
`cache:{provider}:{tool}:{hash}`, `quota:{provider}:...:{hour}` — and arq owns
its own namespace.

**Connection failure fails open, never crashes**, but asymmetrically. Callers
decide the log level: a dead cache is a miss and is unremarkable; a dead quota
means no ceiling at all, which is exactly when unmetered spending goes
unnoticed. See `cache.get_or_fetch` and `quota.QuotaCounter`.
"""

from __future__ import annotations

import redis.asyncio as aioredis
from redis.exceptions import RedisError

# Re-exported so cache/quota/health catch one name rather than importing from
# redis directly. TimeoutError and ConnectionError both subclass RedisError.
RedisUnavailable = RedisError

_pool: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return the process-wide Redis client.

    Lazily constructed so importing this module never opens a socket — tests
    and Alembic import the app without a live instance.
    """
    global _pool
    if _pool is None:
        # Import here: settings reads .env at construction, and module-level
        # import order shouldn't force that during collection.
        from app.shared.config import settings

        _pool = aioredis.from_url(
            settings.redis_url,
            decode_responses=False,  # cache stores JSON bytes; quota uses ints
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
    return _pool


async def close_redis() -> None:
    """Close the pool. Used by tests and app shutdown."""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


def reset_redis() -> None:
    """Drop the cached client without closing it. Tests use this after
    pointing `settings.redis_url` somewhere else."""
    global _pool
    _pool = None
