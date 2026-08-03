"""Cassette transport — intercepts httpx requests to record/replay provider calls.

Every provider is recorded at one interception point: a custom httpx transport.
`PROVIDER_MODE=replay` (default) reads from disk and never touches the network.
`PROVIDER_MODE=record` makes real requests and writes responses to disk.
`PROVIDER_MODE=live` passes through without recording.

Cassettes are namespaced per provider so lanes never collide.

**Credentials are stripped twice, for two different reasons.**

*Before hashing*, so a cassette survives a key rotation. Hashing the raw URL
put `api_key` in the digest: rotate the key and every hash changes for requests
that didn't, and a cassette recorded with a real key can never be matched on
replay — which burns approved live calls and produces unusable fixtures.

*Before writing*, because cassettes are committed. Unredacted means a provider
key lands in git history.

Redaction is by key name, never by matching against a value — a value-based
strip silently stops working the moment a key rotates.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from app.shared.config import settings

CASSETTE_DIR = Path("fixtures/cassettes")

REDACTED = "<REDACTED>"

# Matched case-insensitively against parameter, body and header names.
# Covers both carriers in this stack: SerpApi's `api_key` query param and
# omkar's `API-Key` header.
_CREDENTIAL_NAMES = {
    "api_key",
    "apikey",
    "api-key",
    "x-api-key",
    "access_token",
    "authorization",
    "token",
    "secret",
    "password",
}


def _is_credential(name: str) -> bool:
    return name.lower().replace("_", "-") in {
        n.lower().replace("_", "-") for n in _CREDENTIAL_NAMES
    }


def _strip_credentials(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop credential-named entries entirely. Used before hashing."""
    return {k: v for k, v in mapping.items() if not _is_credential(k)}


def _redact(value: Any) -> Any:
    """Replace credential-named entries with a placeholder, recursively.

    Used before writing: the shape stays visible for debugging, the secret
    doesn't.
    """
    if isinstance(value, dict):
        return {k: (REDACTED if _is_credential(k) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _canonical(args: dict[str, Any]) -> str:
    """Sorted, separator-canonical JSON so key order can't fork a cassette."""
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_args(args: dict[str, Any]) -> str:
    """Deterministic hash over normalized arguments."""
    return hashlib.sha256(_canonical(args).encode()).hexdigest()[:32]


def _base_url(url: httpx.URL) -> str:
    """Scheme, host and path — no query string, which carries `api_key`."""
    return str(url.copy_with(query=None, fragment=None))


def _body_for_hash(request: httpx.Request) -> Any:
    """Request body, credential-stripped, or None when there isn't one.

    POST bodies must participate in the hash: two different POSTs to one
    endpoint are two different cassettes.
    """
    try:
        raw = request.content
    except (httpx.RequestNotRead, RuntimeError):
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return hashlib.sha256(raw).hexdigest()
    if isinstance(parsed, dict):
        return _strip_credentials(parsed)
    return parsed


def request_fingerprint(request: httpx.Request) -> str:
    """The cassette hash for a request — credential-independent by construction."""
    return _normalize_args(
        {
            "method": request.method.upper(),
            "url": _base_url(request.url),
            "params": _strip_credentials(dict(request.url.params)),
            "body": _body_for_hash(request),
        }
    )


class CassetteTransport(httpx.AsyncBaseTransport):
    """httpx async transport that records/replays provider calls.

    Usage:
        client = httpx.AsyncClient(transport=CassetteTransport("serpapi"))
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self._mode = settings.provider_mode

    @property
    def _dir(self) -> Path:
        # Resolved per call rather than pinned in __init__, so tests that
        # repoint CASSETTE_DIR take effect on transports already constructed.
        return CASSETTE_DIR / self.provider

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        cassette_path = self._dir / f"{request_fingerprint(request)}.json"

        if self._mode == "replay":
            return self._read_cassette(cassette_path)

        transport = httpx.AsyncHTTPTransport()
        response = await transport.handle_async_request(request)

        if self._mode == "record":
            await response.aread()
            self._write_cassette(cassette_path, request, response)

        return response

    def _read_cassette(self, path: Path) -> httpx.Response:
        if not path.exists():
            raise FileNotFoundError(
                f"No cassette at {path}. Record one first with PROVIDER_MODE=record, "
                f"or set PROVIDER_MODE=live for passthrough."
            )
        data = json.loads(path.read_text())
        body = data["body"]
        content = json.dumps(body).encode() if isinstance(body, (dict, list)) else body.encode()
        return httpx.Response(
            status_code=data["status"],
            headers=data.get("headers", {}),
            content=content,
        )

    def _write_cassette(self, path: Path, request: httpx.Request, response: httpx.Response) -> None:
        body: Any = response.text
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            pass

        data = {
            # The request is stored redacted, for debugging which call produced
            # this fixture. It is not what the hash is computed from.
            "request": {
                "method": request.method.upper(),
                "url": _base_url(request.url),
                "params": _redact(dict(request.url.params)),
                "headers": _redact(dict(request.headers)),
            },
            "status": response.status_code,
            "headers": _redact(dict(response.headers)),
            "body": _redact(body),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str))


def make_client(provider: str) -> httpx.AsyncClient:
    """Create an httpx async client with cassette transport for the given provider."""
    return httpx.AsyncClient(transport=CassetteTransport(provider), timeout=60.0)
