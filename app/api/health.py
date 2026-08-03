"""Health endpoint and any other root-level routes.

Checks the dependencies rather than returning a literal: a bare `{"status":
"ok"}` reports healthy while Postgres is unreachable, which is the one moment
the check exists for.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response
from sqlalchemy import text

from app.shared.db import async_session_maker
from app.shared.redis_client import get_redis

log = logging.getLogger(__name__)

router = APIRouter()


async def _check_postgres() -> bool:
    try:
        async with async_session_maker() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 — a health check reports, never raises
        log.warning("health: postgres unreachable: %s", exc)
        return False


async def _check_redis() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception as exc:  # noqa: BLE001 — same
        log.warning("health: redis unreachable: %s", exc)
        return False


@router.get("/health")
async def health(response: Response) -> dict:
    """200 when every dependency answers, 503 otherwise.

    Reports per-dependency so a failure says *which* one — a check that only
    returns a status tells you less than one that names the culprit. Both
    probes swallow their exceptions: an endpoint that 500s during an outage is
    indistinguishable from an endpoint that is itself broken.
    """
    checks = {
        "postgres": await _check_postgres(),
        "redis": await _check_redis(),
    }
    healthy = all(checks.values())
    response.status_code = 200 if healthy else 503
    return {"status": "ok" if healthy else "degraded", "checks": checks}
