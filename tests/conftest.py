"""Shared fixtures.

Redis-backed tests run against a real instance, not a fake — the defect being
guarded against (per-process dicts pretending to be shared state) is invisible
to a fake. If no instance is reachable, those tests skip loudly rather than
passing vacuously.
"""

from __future__ import annotations

import httpx
import pytest
import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.main import app
from app.shared import redis_client
from app.shared.config import settings


@pytest.fixture(scope="session")
def redis_url() -> str:
    return settings.redis_url


@pytest.fixture
async def redis_available(redis_url: str) -> bool:
    """Skip the test unless a real Redis answers."""
    client = aioredis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1)
    try:
        await client.ping()
    except RedisError:
        pytest.skip(f"no Redis at {redis_url} — start one with `brew services start redis`")
    finally:
        await client.aclose()
    return True


@pytest.fixture(autouse=True)
async def _clean_redis(request: pytest.FixtureRequest):
    """Drop this project's keys around each test, leaving arq's namespace alone.

    Also rebuilds the connection pool per test. pytest-asyncio gives each test
    a fresh event loop, and a pool cached from a previous one raises
    `RuntimeError: Event loop is closed` on first use.
    """
    if "redis_available" not in request.fixturenames:
        yield
        return

    async def _purge() -> None:
        try:
            r = redis_client.get_redis()
            for prefix in ("cache:*", "quota:*"):
                async for k in r.scan_iter(match=prefix, count=500):
                    await r.delete(k)
        except RedisError:
            pass

    redis_client.reset_redis()
    await _purge()
    yield
    await _purge()
    await redis_client.close_redis()


@pytest.fixture
async def client() -> httpx.AsyncClient:
    """FastAPI async test client."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
