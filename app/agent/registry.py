"""Tool auto-discovery registry.

One file per tool in `app/agent/tools/`; collected at import by scanning the
package. This is what makes tool lanes parallelizable — each tool is its own
file, its own lane, and the registry finds them automatically.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from typing import Any

import app.agent.tools as tools_package

# Registry: tool_name -> callable
_registry: dict[str, Callable[..., Any]] = {}


def _discover() -> None:
    """Scan app.agent.tools for all public callables and register them."""
    _registry.clear()
    for _importer, modname, ispkg in pkgutil.iter_modules(tools_package.__path__):
        if ispkg or modname.startswith("_"):
            continue
        module = importlib.import_module(f"app.agent.tools.{modname}")
        for attr_name in dir(module):
            if attr_name.startswith("_"):
                continue
            attr = getattr(module, attr_name)
            if callable(attr) and getattr(attr, "_is_tool", False):
                tool_name = getattr(attr, "_tool_name", attr_name)
                _registry[tool_name] = attr


def register_tool(name: str | None = None) -> Callable:
    """Decorator that marks a function as a discoverable tool.

    Usage:
        @register_tool()
        async def search_flights(...): ...

        @register_tool("my_tool")
        async def my_func(...): ...
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or func.__name__
        func._is_tool = True  # type: ignore[attr-defined]
        func._tool_name = tool_name  # type: ignore[attr-defined]
        return func

    return decorator


def get_tools() -> dict[str, Callable[..., Any]]:
    """Return the registry of all discovered tools."""
    if not _registry:
        _discover()
    return dict(_registry)


def list_tool_names() -> list[str]:
    """Return the names of all registered tools."""
    return sorted(get_tools().keys())
