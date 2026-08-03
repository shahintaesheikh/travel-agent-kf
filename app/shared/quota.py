"""Quota enforcement — returned as a value, never raised.

Enforced in the adapter, not in agent instructions. The agent cannot be
trusted to self-limit, and fan-out control is the reason quota sits at the
adapter boundary at all.

Backed by Redis, not a process-local dict. Counting in a dict meant each
process counted to 6 independently, so the real ceiling was 6 x however many
processes existed — web, worker, and every extra Render web instance.

`EXPIRE` is set **only when `INCR` returns 1**. Calling it on every increment
resets the clock, so a steadily-used counter never expires and the hourly
window becomes permanent.
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

from app.shared.config import settings
from app.shared.redis_client import RedisUnavailable, get_redis

log = logging.getLogger(__name__)

KEY_PREFIX = "quota"

# A turn is bounded by a single agent invocation; the TTL is a leak-stopper for
# keys whose turn never formally ends, not a semantic limit.
_TURN_TTL_SECONDS = 15 * 60
_HOUR_TTL_SECONDS = 60 * 60

# Per-turn identity is request-scoped, so concurrent turns in one process don't
# share a counter. S2 sets this from graph state once the agent exists.
_current_turn: ContextVar[str] = ContextVar("quota_turn_id", default="default")


class QuotaExceeded:
    """Structured result returned when a provider call exceeds quota.

    Never raised as an exception. The adapter returns this value and the agent
    sees a structured message it can respond to.
    """

    def __init__(self, provider: str, reason: str):
        self.provider = provider
        self.reason = reason

    def __str__(self) -> str:
        return f"Quota exceeded for {self.provider}: {self.reason}"

    def __repr__(self) -> str:
        return f"QuotaExceeded(provider={self.provider!r}, reason={self.reason!r})"

    def __bool__(self) -> bool:
        return False


def start_turn(turn_id: str | None = None) -> str:
    """Begin a new turn, returning its id. Counters key off this."""
    tid = turn_id or uuid.uuid4().hex
    _current_turn.set(tid)
    return tid


def current_turn() -> str:
    return _current_turn.get()


def _hour_bucket() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H")


class QuotaCounter:
    """Redis-backed quota counter. Shared across every process.

    Ceilings come from settings: 6 calls per agent turn, 40 per trip-hour.
    """

    def _turn_key(self, provider: str, turn_id: str | None) -> str:
        return f"{KEY_PREFIX}:turn:{turn_id or current_turn()}:{provider}"

    def _hour_key(self, provider: str, trip_id: str | None) -> str:
        # PRD US-018 writes this as `quota:{provider}:{hour}`; the limit it
        # states is *per trip*-hour, so trip_id is part of the key when known.
        if trip_id:
            return f"{KEY_PREFIX}:{provider}:{trip_id}:{_hour_bucket()}"
        return f"{KEY_PREFIX}:{provider}:{_hour_bucket()}"

    async def check(
        self,
        provider: str,
        trip_id: str | None = None,
        turn_id: str | None = None,
    ) -> QuotaExceeded | None:
        """Would another call exceed quota? Returns `QuotaExceeded` or None.

        Call before the outbound request. Never raises — a dead Redis fails
        open, because refusing to plan a trip is worse than briefly losing a
        ceiling. That failure is logged at warning: no ceiling at all is
        exactly when unmetered spending goes unnoticed.
        """
        try:
            r = get_redis()
            turn_calls = int(await r.get(self._turn_key(provider, turn_id)) or 0)
            if turn_calls >= settings.quota_calls_per_turn:
                return QuotaExceeded(
                    provider, f"Max {settings.quota_calls_per_turn} calls per turn"
                )

            hour_calls = int(await r.get(self._hour_key(provider, trip_id)) or 0)
            if hour_calls >= settings.quota_calls_per_trip_hour:
                return QuotaExceeded(
                    provider,
                    f"Max {settings.quota_calls_per_trip_hour} calls per trip-hour",
                )
        except RedisUnavailable as exc:
            log.warning(
                "QUOTA UNENFORCED: Redis unreachable during check for %s: %s. "
                "Provider calls are proceeding with no ceiling.",
                provider,
                exc,
            )
            return None
        return None

    async def increment(
        self,
        provider: str,
        trip_id: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        """Count one outbound call.

        Call this **only when the request actually leaves the process** — put
        it inside the `fetch` closure passed to `get_or_fetch`, so a cache hit
        doesn't consume quota.
        """
        try:
            r = get_redis()
            for key, ttl in (
                (self._turn_key(provider, turn_id), _TURN_TTL_SECONDS),
                (self._hour_key(provider, trip_id), _HOUR_TTL_SECONDS),
            ):
                count = await r.incr(key)
                # Only on creation. Re-expiring on every increment would keep
                # a busy counter alive forever and make the window permanent.
                if count == 1:
                    await r.expire(key, ttl)
        except RedisUnavailable as exc:
            log.warning(
                "QUOTA UNENFORCED: Redis unreachable during increment for %s: %s. "
                "This call went uncounted.",
                provider,
                exc,
            )

    async def peek(
        self,
        provider: str,
        trip_id: str | None = None,
        turn_id: str | None = None,
    ) -> tuple[int, int]:
        """(turn_calls, trip_hour_calls). Diagnostics and tests."""
        try:
            r = get_redis()
            return (
                int(await r.get(self._turn_key(provider, turn_id)) or 0),
                int(await r.get(self._hour_key(provider, trip_id)) or 0),
            )
        except RedisUnavailable:
            return (0, 0)

    async def reset_turn(self, turn_id: str | None = None) -> str:
        """Drop the current turn's counters and begin a new turn."""
        try:
            r = get_redis()
            async for k in r.scan_iter(
                match=f"{KEY_PREFIX}:turn:{turn_id or current_turn()}:*", count=500
            ):
                await r.delete(k)
        except RedisUnavailable as exc:
            log.warning("quota turn reset failed: %s", exc)
        return start_turn()


# Module-level singleton
quota_counter = QuotaCounter()
