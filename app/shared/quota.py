"""Quota enforcement — returned as a value, never raised.

Enforced in the adapter, not in agent instructions. The agent cannot be
trusted to self-limit.
"""

from __future__ import annotations

from app.shared.config import settings


class QuotaExceeded:
    """Structured result returned when a provider call exceeds quota.

    Never raised as an exception. The adapter returns this value and the
    agent sees a structured message it can respond to.
    """

    def __init__(self, provider: str, reason: str):
        self.provider = provider
        self.reason = reason

    def __str__(self) -> str:
        return f"Quota exceeded for {self.provider}: {self.reason}"

    def __bool__(self) -> bool:
        return False


class QuotaCounter:
    """In-memory quota counter. Tracks calls per turn and per trip-hour.

    Production should use Redis for the per-trip-hour counter to survive
    restarts. The per-turn counter resets each turn regardless.
    """

    def __init__(self) -> None:
        self._turn_calls: dict[str, int] = {}
        self._trip_hour_calls: dict[str, int] = {}

    def check(self, provider: str, trip_id: str | None = None) -> QuotaExceeded | None:
        """Check if a call would exceed quota. Returns QuotaExceeded or None."""
        if self._turn_calls.get(provider, 0) >= settings.quota_calls_per_turn:
            return QuotaExceeded(
                provider, f"Max {settings.quota_calls_per_turn} calls per turn"
            )
        if trip_id:
            key = f"{provider}:{trip_id}"
            if self._trip_hour_calls.get(key, 0) >= settings.quota_calls_per_trip_hour:
                return QuotaExceeded(
                    provider,
                    f"Max {settings.quota_calls_per_trip_hour} calls per trip-hour",
                )
        return None

    def increment(self, provider: str, trip_id: str | None = None) -> None:
        """Increment counters after a successful call."""
        self._turn_calls[provider] = self._turn_calls.get(provider, 0) + 1
        if trip_id:
            key = f"{provider}:{trip_id}"
            self._trip_hour_calls[key] = self._trip_hour_calls.get(key, 0) + 1

    def reset_turn(self) -> None:
        """Reset the per-turn counter. Called at the start of each turn."""
        self._turn_calls.clear()


# Module-level singleton
quota_counter = QuotaCounter()
