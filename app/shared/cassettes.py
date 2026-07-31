"""Cassette transport — intercepts httpx requests to record/replay provider calls.

Every provider is recorded at one interception point: a custom httpx transport.
`PROVIDER_MODE=replay` (default) reads from disk and never touches the network.
`PROVIDER_MODE=record` makes real requests and writes responses to disk.
`PROVIDER_MODE=live` passes through without recording.

Cassettes are namespaced per provider so lanes never collide.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from app.shared.config import settings

CASSETTE_DIR = Path("fixtures/cassettes")

# Fields to redact before writing cassettes (cassettes get committed).
_REDACT_KEYS = {"api_key", "API-Key", "API_Key", "apikey"}
_REDACT_HEADERS = {"api-key", "x-api-key", "authorization"}


def _normalize_args(args: dict[str, Any]) -> str:
    """Produce a deterministic hash from normalized arguments."""
    normalized = json.dumps(args, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


def _redact_body(body: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive fields from a request body."""
    if isinstance(body, dict):
        return {
            k: ("<REDACTED>" if k in _REDACT_KEYS else _redact_body(v))
            for k, v in body.items()
        }
    if isinstance(body, list):
        return [_redact_body(item) for item in body]
    return body


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Redact sensitive headers."""
    return {
        k: ("<REDACTED>" if k.lower() in _REDACT_HEADERS else v)
        for k, v in headers.items()
    }


class CassetteTransport(httpx.AsyncBaseTransport):
    """httpx async transport that records/replays provider calls.

    Usage:
        client = httpx.AsyncClient(transport=CassetteTransport("serpapi"))
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self._mode = settings.provider_mode
        self._dir = CASSETTE_DIR / provider
        self._dir.mkdir(parents=True, exist_ok=True)

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        args_hash = _normalize_args(
            {
                "url": str(request.url),
                "method": request.method,
                "params": dict(request.url.params),
            }
        )
        cassette_path = self._dir / f"{args_hash}.json"

        if self._mode == "replay":
            return self._read_cassette(cassette_path)

        # Build a real transport for live/record modes
        transport = httpx.AsyncHTTPTransport()
        response = await transport.handle_async_request(request)

        if self._mode == "record":
            self._write_cassette(cassette_path, request, response)

        return response

    def _read_cassette(self, path: Path) -> httpx.Response:
        if not path.exists():
            raise FileNotFoundError(
                f"No cassette at {path}. Record one first with PROVIDER_MODE=record, "
                f"or set PROVIDER_MODE=live for passthrough."
            )
        data = json.loads(path.read_text())
        return httpx.Response(
            status_code=data["status"],
            headers=data["headers"],
            content=json.dumps(data["body"]).encode()
            if isinstance(data["body"], (dict, list))
            else data["body"].encode(),
        )

    def _write_cassette(
        self, path: Path, request: httpx.Request, response: httpx.Response
    ) -> None:
        body: Any = response.text
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            pass

        # Redact request body and headers before writing
        redacted_body = body
        if isinstance(body, dict):
            redacted_body = _redact_body(body)

        data = {
            "status": response.status_code,
            "headers": _redact_headers(dict(response.headers)),
            "body": redacted_body,
        }
        path.write_text(json.dumps(data, indent=2, default=str))


def make_client(provider: str) -> httpx.AsyncClient:
    """Create an httpx async client with cassette transport for the given provider."""
    return httpx.AsyncClient(transport=CassetteTransport(provider), timeout=60.0)
