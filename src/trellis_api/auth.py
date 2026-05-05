"""API key authentication for the Trellis REST API.

Single-shared-secret authentication via the ``X-API-Key`` header,
gated on a ``TRELLIS_API_KEY`` environment variable. When the env
var is unset, the dependency is a no-op and the API stays open —
intended for local development and CI. Production deployments set
the env var and every mutating route (and most reads) require the
matching header.

Health and version probes (``/healthz``, ``/readyz``,
``/api/version``) deliberately stay open so orchestrator probes
work without holding the secret. Static UI under ``/ui`` also stays
open; auth lives at the API layer.

This is deliberately the cheapest possible auth that closes the
"unauthenticated REST on a corporate network" gap. JWT, OAuth, or
per-tenant identity belong in a follow-up PR — by then the deploy
plumbing for secret rotation is also in place.
"""

from __future__ import annotations

import os
import secrets

import structlog
from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

logger = structlog.get_logger(__name__)

#: Env var name. Reading the value at request time (not import time)
#: lets tests monkeypatch the secret without restarting the app.
API_KEY_ENV = "TRELLIS_API_KEY"

#: Header name. ``auto_error=False`` so the dependency can return its
#: own 401 with a structured body instead of FastAPI's default plain
#: ``{"detail": "Not authenticated"}``.
_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def _expected_key() -> str | None:
    """Return the configured API key, or None if unset / empty."""
    value = os.environ.get(API_KEY_ENV)
    if not value:
        return None
    return value


def warn_if_unauthenticated() -> None:
    """Log a one-time warning at startup if no API key is configured.

    Called from the FastAPI lifespan so the operator sees the warning
    in their normal boot logs rather than discovering the gap by
    accident later.
    """
    if _expected_key() is None:
        logger.warning(
            "api_key_unset",
            env_var=API_KEY_ENV,
            message=(
                "TRELLIS_API_KEY is not set; the REST API will accept "
                "every request without authentication. Set the env var "
                "before exposing the API beyond loopback."
            ),
        )


def require_api_key(api_key: str | None = Security(_API_KEY_HEADER)) -> None:
    """FastAPI dependency: require ``X-API-Key`` to match ``TRELLIS_API_KEY``.

    No-op when ``TRELLIS_API_KEY`` is unset (dev / CI). When set,
    rejects requests missing the header or carrying a wrong value
    with HTTP 401. Uses ``secrets.compare_digest`` for constant-time
    comparison — a small but free defence against timing oracles.
    """
    expected = _expected_key()
    if expected is None:
        return
    if api_key is None or not secrets.compare_digest(api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid X-API-Key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
