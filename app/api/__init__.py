"""API package — router include-by-loop.

Each API module exposes a `router`; the aggregator iterates. This is what
makes API lanes parallelizable — each endpoint module is its own lane and
the aggregator finds them automatically.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from fastapi import APIRouter

import app.api as api_package

# The top-level router that includes all discovered sub-routers
api_router = APIRouter(prefix="/api")


def _discover_and_include() -> None:
    """Scan app.api for modules with a `router` attribute and include them."""
    for _importer, modname, ispkg in pkgutil.iter_modules(api_package.__path__):
        if ispkg or modname.startswith("_"):
            continue
        module = importlib.import_module(f"app.api.{modname}")
        router: Any = getattr(module, "router", None)
        if router is not None and isinstance(router, APIRouter):
            api_router.include_router(router, prefix=f"/{modname}")


# Discover and include on import
_discover_and_include()
