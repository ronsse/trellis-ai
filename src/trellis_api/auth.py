"""Scoped API-key authentication for the Trellis REST API.

Three modes via ``TRELLIS_AUTH_MODE``:

* ``off`` — every request passes with all scopes (dev / CI).
* ``optional`` — requests without a credential pass with all scopes
  (migration mode); requests *with* a credential must present a valid
  one or get 401.
* ``required`` — every request must present a valid credential.

When ``TRELLIS_AUTH_MODE`` is unset the mode is inferred for backwards
compatibility: ``required`` if the legacy ``TRELLIS_API_KEY`` shared
secret is set, else ``off``. An invalid value raises
:class:`~trellis.errors.ConfigError` — loud at startup via
:func:`warn_if_unauthenticated`, never silently downgraded.

Credentials are accepted on either header (``X-API-Key`` wins when
both are present)::

    X-API-Key: trellis_ak_<key_id>.<secret>
    Authorization: Bearer trellis_ak_<key_id>.<secret>

Two credential kinds are honoured:

* **Scoped keys** minted by ``trellis admin api-keys create`` and
  verified against the operational-plane :class:`ApiKeyStore`
  (sha256 of the secret half, constant-time compare).
* **Legacy shared secret** — a token exactly equal to
  ``TRELLIS_API_KEY`` is granted all scopes (``name="shared-secret"``)
  so existing deployments keep working while they migrate.

Per-router scope enforcement lives in :func:`require_scope`; the
router→scope map is wired in :mod:`trellis_api.app`. Health and
version probes (``/healthz``, ``/readyz``, ``/api/version``) stay
unauthenticated so orchestrator probes work without a key.

401 responses are deliberately undifferentiated ("missing or invalid
API credentials") — the failure category (malformed / unknown /
revoked / mismatch) is logged server-side only, so callers cannot
probe which key ids exist.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn

import structlog
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from trellis.auth import ALL_SCOPES, scopes_satisfy, verify_token
from trellis.errors import ConfigError

logger = structlog.get_logger(__name__)

#: Legacy shared-secret env var. Reading the value at request time (not
#: import time) lets tests monkeypatch the secret without restarting.
API_KEY_ENV = "TRELLIS_API_KEY"

#: Auth-mode env var — ``off`` | ``optional`` | ``required``.
AUTH_MODE_ENV = "TRELLIS_AUTH_MODE"

AUTH_MODE_OFF = "off"
AUTH_MODE_OPTIONAL = "optional"
AUTH_MODE_REQUIRED = "required"
_VALID_MODES = frozenset({AUTH_MODE_OFF, AUTH_MODE_OPTIONAL, AUTH_MODE_REQUIRED})

#: ``auto_error=False`` on both headers so the dependency can return
#: its own 401 with a structured body instead of FastAPI's default.
_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
_AUTHORIZATION_HEADER = APIKeyHeader(name="Authorization", auto_error=False)

_BEARER_PREFIX = "bearer "


@dataclass(frozen=True)
class AuthContext:
    """Resolved per-request identity + grants.

    ``key_id`` / ``name`` are ``None`` for anonymous passthrough
    (modes ``off`` / ``optional`` without a credential); the legacy
    shared secret resolves to ``key_id=None, name="shared-secret"``.
    """

    key_id: str | None
    name: str | None
    scopes: frozenset[str]
    mode: str


def resolve_auth_mode() -> str:
    """Return the effective auth mode, raising loudly on a bad value.

    Unset / empty env var → backwards-compat inference: ``required``
    when the legacy ``TRELLIS_API_KEY`` is set, else ``off``.
    """
    raw = os.environ.get(AUTH_MODE_ENV)
    if raw is None or not raw.strip():
        return AUTH_MODE_REQUIRED if os.environ.get(API_KEY_ENV) else AUTH_MODE_OFF
    mode = raw.strip().lower()
    if mode not in _VALID_MODES:
        msg = (
            f"Invalid {AUTH_MODE_ENV}={raw!r}; expected one of"
            f" {sorted(_VALID_MODES)}. Refusing to guess an auth posture."
        )
        raise ConfigError(msg, setting=AUTH_MODE_ENV)
    return mode


def warn_if_unauthenticated() -> None:
    """Log a startup warning when the effective mode is permissive.

    Called from the FastAPI lifespan so the operator sees the warning
    in their normal boot logs. Also the startup chokepoint for an
    invalid ``TRELLIS_AUTH_MODE`` — :func:`resolve_auth_mode` raises
    here, crashing uvicorn before it accepts a request.
    """
    mode = resolve_auth_mode()
    if mode in (AUTH_MODE_OFF, AUTH_MODE_OPTIONAL):
        logger.warning(
            "api_auth_permissive",
            mode=mode,
            env_var=AUTH_MODE_ENV,
            message=(
                f"Effective auth mode is '{mode}': requests without "
                "credentials are accepted with full scopes. Set "
                f"{AUTH_MODE_ENV}=required (and mint keys with "
                "'trellis admin api-keys create') before exposing the "
                "API beyond loopback."
            ),
        )


def _extract_token(api_key: str | None, authorization: str | None) -> str | None:
    """Pick the presented token: ``X-API-Key`` wins over Bearer."""
    if api_key:
        return api_key
    if authorization and authorization.lower().startswith(_BEARER_PREFIX):
        return authorization[len(_BEARER_PREFIX) :].strip() or None
    return None


def _raise_401(reason: str) -> NoReturn:
    """Reject with an undifferentiated 401; log the real reason."""
    logger.warning("api_auth_rejected", reason=reason)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing or invalid API credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def authenticate(
    api_key: str | None = Security(_API_KEY_HEADER),
    authorization: str | None = Security(_AUTHORIZATION_HEADER),
) -> AuthContext:
    """FastAPI dependency: resolve the request's :class:`AuthContext`.

    Mode semantics:

    * ``off`` — skip all checks, grant all scopes.
    * ``optional`` — no credential → all scopes (migration mode);
      credential present but invalid → 401.
    * ``required`` — missing or invalid credential → 401.
    """
    mode = resolve_auth_mode()
    if mode == AUTH_MODE_OFF:
        return AuthContext(key_id=None, name=None, scopes=ALL_SCOPES, mode=mode)

    token = _extract_token(api_key, authorization)
    if token is None:
        if mode == AUTH_MODE_OPTIONAL:
            return AuthContext(key_id=None, name=None, scopes=ALL_SCOPES, mode=mode)
        _raise_401("missing_credential")

    # Legacy shared secret — exact match grants all scopes so existing
    # single-secret deployments keep working while they migrate.
    expected = os.environ.get(API_KEY_ENV)
    if expected and secrets.compare_digest(token, expected):
        return AuthContext(
            key_id=None, name="shared-secret", scopes=ALL_SCOPES, mode=mode
        )

    # Imported here (like the route modules do at call depth) to avoid
    # the module-level cycle: app.py imports this module at import time.
    from trellis_api.app import get_registry  # noqa: PLC0415

    record = verify_token(token, get_registry().operational.api_key_store)
    if record is None:
        # verify_token already logged the category (malformed / unknown
        # / revoked / mismatch); the response stays undifferentiated.
        _raise_401("invalid_credential")
    return AuthContext(
        key_id=record.key_id,
        name=record.name,
        scopes=frozenset(record.scopes),
        mode=mode,
    )


def authenticate_optional(
    api_key: str | None = Security(_API_KEY_HEADER),
    authorization: str | None = Security(_AUTHORIZATION_HEADER),
) -> AuthContext | None:
    """Like :func:`authenticate`, but a *missing* credential resolves to
    ``None`` instead of 401.

    For endpoints that are public-but-minimal and reveal extra detail to
    authenticated callers (``/readyz`` backend breakdown, gated
    ``/metrics``). Orchestrator probes send no credential and must keep
    working; callers that *present* a credential must present a valid
    one — an invalid token still raises 401, never silently downgraded
    to anonymous.

    Mode semantics follow :func:`authenticate`: in ``off`` every request
    resolves to a full-scope context, and in ``optional`` a request
    without a credential resolves to the anonymous full-scope context —
    only ``required`` mode produces ``None`` here.
    """
    mode = resolve_auth_mode()
    if mode == AUTH_MODE_REQUIRED and _extract_token(api_key, authorization) is None:
        return None
    return authenticate(api_key, authorization)


def require_scope(scope: str) -> Callable[..., AuthContext]:
    """Dependency factory: require ``scope`` (or ``admin``) on the caller.

    Raises :class:`ValueError` at wiring time for an unknown scope so a
    typo in the router map fails at import, not per-request.
    """
    if scope not in ALL_SCOPES:
        msg = f"Unknown scope {scope!r}; known scopes: {sorted(ALL_SCOPES)}"
        raise ValueError(msg)

    def _require(
        ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
    ) -> AuthContext:
        if not scopes_satisfy(ctx.scopes, scope):
            logger.warning(
                "api_auth_forbidden",
                required_scope=scope,
                key_id=ctx.key_id,
                key_name=ctx.name,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"credential lacks required scope '{scope}'",
            )
        return ctx

    return _require
