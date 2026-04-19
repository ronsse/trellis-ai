"""Shared HTTP helpers for the sync and async clients.

Both :class:`~trellis_sdk.client.TrellisClient` and
:class:`~trellis_sdk.async_client.AsyncTrellisClient` funnel every
outbound request through functions here so we get one implementation
of:

* Response-to-typed-exception mapping (404 / 409 / 429 / 5xx / …)
* ``Retry-After`` header parsing (seconds or HTTP-date)
* ``GET /api/version`` handshake + compatibility check

The clients own their own ``httpx.Client`` / ``httpx.AsyncClient``
instances so callers control lifecycle; this module is pure
functions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import structlog

from trellis_sdk.exceptions import (
    TrellisAPIError,
    TrellisRateLimitError,
    TrellisVersionMismatchError,
)

if TYPE_CHECKING:
    import httpx

logger = structlog.get_logger(__name__)

# Major version this SDK was built against.  The handshake rejects
# any server with a different major.  Source of truth for this
# constant is the shipped constant in :mod:`trellis.api_version` —
# the SDK deliberately hardcodes its own expectation so a client
# package that's pinned to an old SDK still refuses to talk to a
# newer incompatible server.
SDK_API_MAJOR = 1
SDK_API_MINOR = 0
SDK_WIRE_SCHEMA = "0.1.0"
SDK_VERSION = "0.1.0"

_HTTP_OK_MAX = 299
_HTTP_NOT_FOUND = 404
_HTTP_RATE_LIMITED = 429


def raise_for_status(resp: httpx.Response, *, request_path: str) -> None:
    """Convert a non-2xx response into a typed :class:`TrellisError`.

    * ``404`` is *not* raised here — many SDK methods translate it to
      a ``None`` return; leave that decision to the caller.
    * ``429`` maps to :class:`TrellisRateLimitError` with
      ``Retry-After`` parsed.
    * Any other non-2xx maps to :class:`TrellisAPIError` with the
      parsed JSON body attached.
    """
    status = resp.status_code
    if status <= _HTTP_OK_MAX:
        return
    if status == _HTTP_NOT_FOUND:
        return  # caller decides
    body = _safe_json(resp)
    message = _extract_message(body) or resp.text or f"HTTP {status}"
    if status == _HTTP_RATE_LIMITED:
        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
        raise TrellisRateLimitError(
            message,
            retry_after_seconds=retry_after,
            body=body,
            request_path=request_path,
        )
    raise TrellisAPIError(status, message, body=body, request_path=request_path)


def check_handshake(
    response_body: dict[str, Any],
    *,
    sdk_version: str = SDK_VERSION,
    sdk_api_major: int = SDK_API_MAJOR,
    sdk_api_minor: int = SDK_API_MINOR,
) -> None:
    """Validate a ``GET /api/version`` payload.

    Raises :class:`TrellisVersionMismatchError` on incompatible
    major or when the SDK's own version is below the server's
    ``sdk_min``.  Logs a warning on minor drift (server minor older
    than what the SDK was built for) but does not raise — additive
    server features are safe to miss.
    """
    server_major = int(response_body.get("api_major", -1))
    server_minor = int(response_body.get("api_minor", 0))
    server_sdk_min = str(response_body.get("sdk_min", "0.0.0"))

    if server_major != sdk_api_major:
        msg = (
            f"Server API major is {server_major}; this SDK requires "
            f"{sdk_api_major}. Upgrade the SDK or pin the server."
        )
        raise TrellisVersionMismatchError(
            msg,
            server_api_major=server_major,
            server_api_minor=server_minor,
            sdk_min=server_sdk_min,
        )

    if _version_lt(sdk_version, server_sdk_min):
        msg = (
            f"This SDK version ({sdk_version}) is below the server's "
            f"minimum supported SDK ({server_sdk_min}). Upgrade the SDK."
        )
        raise TrellisVersionMismatchError(
            msg,
            server_api_major=server_major,
            server_api_minor=server_minor,
            sdk_min=server_sdk_min,
        )

    if server_minor < sdk_api_minor:
        logger.warning(
            "sdk_server_minor_drift",
            server_api_minor=server_minor,
            sdk_expected_minor=sdk_api_minor,
            msg=(
                "Server API is older than this SDK was built against. "
                "Newer SDK features may be unavailable."
            ),
        )


def _version_lt(a: str, b: str) -> bool:
    """Return ``True`` when ``a`` is strictly less than ``b`` using
    tuple-of-ints comparison.  Non-numeric components are ignored
    conservatively (treated as 0), which is fine for the handshake
    use case — we're not implementing full semver.
    """
    return _parse_version(a) < _parse_version(b)


def _parse_version(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for raw in v.split("."):
        digits = "".join(c for c in raw if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _extract_message(body: Any) -> str | None:
    if isinstance(body, dict):
        for key in ("message", "detail", "error"):
            val = body.get(key)
            if isinstance(val, str):
                return val
    return None


def _parse_retry_after(header: str | None) -> float | None:
    """Parse ``Retry-After`` as seconds-int or HTTP-date.

    Returns the delta in seconds.  ``None`` on missing or unparseable
    header.  Negative deltas (past HTTP-dates) clamp to ``0``.
    """
    if not header:
        return None
    stripped = header.strip()
    # Seconds form (integer).
    if stripped.isdigit():
        return float(stripped)
    # HTTP-date form.
    try:
        when = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = (when - datetime.now(tz=UTC)).total_seconds()
    return max(0.0, delta)


__all__ = [
    "SDK_API_MAJOR",
    "SDK_API_MINOR",
    "SDK_VERSION",
    "SDK_WIRE_SCHEMA",
    "check_handshake",
    "raise_for_status",
]
