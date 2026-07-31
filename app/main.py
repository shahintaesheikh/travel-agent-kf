"""FastAPI app factory.

One web service, one worker, one database, one cache.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api import api_router
from app.api.health import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Travel Agent",
        version="0.1.0",
        description="Private two-user trip planning agent",
    )

    app.include_router(health_router, prefix="/api")
    app.include_router(api_router)

    return app


app = create_app()
