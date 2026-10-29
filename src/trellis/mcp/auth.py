"""Transport selection and scoped API-key auth for the MCP server.

The MCP server speaks stdio by default, where the parent agent host is
the trust boundary and no credential is meaningful. Setting
``TRELLIS_MCP_TRANSPORT=http`` turns it into a network listener, at
which point it needs the same scoped-credential model the REST surface
already has.

This module is the thin glue between the two. The token machinery
itself — token format, hashing, revocation, ``admin``-implies-all — is
transport-agnostic and lives in :mod:`trellis.auth`; the FastAPI
equivalent of this module is ``trellis_api.auth``. Nothing here may
import ``trellis_api``: MCP is a peer surface, not a REST client.

Two facts about FastMCP shape the design:

* ``_get_auth_context()`` returns ``skip_auth=True`` whenever the active
  transport is stdio, so per-tool ``auth=`` checks are inert there. Tools
  can therefore be decorated unconditionally at import time — there is no
  transport-conditional registration and no bypass branch to maintain.
* An ``AuthCheck`` is just ``Callable[[AuthContext], bool]``. FastMCP
  ships :func:`fastmcp.server.auth.require_scopes`, but it does flat
  subset matching and would deny an ``admin``-scoped key on a ``read``
  tool. :func:`trellis_scope` routes through
  :func:`trellis.auth.scopes_satisfy` instead so ``admin`` keeps implying
  every other scope, exactly as it does over REST.

Scope checks are also what FastMCP filters ``tools/list`` on, so a
``read``-scoped key does not merely fail to call ``execute_mutation`` —
it never sees it.

:func:`trellis_scope` decides whether a tool is callable *at all*, which
is all a check made before the tool body runs can decide. A decision
that depends on the call's own arguments — ``execute_mutation`` naming
one destructive operation inside an otherwise ``mutate``-scoped call —
has to be made from inside the tool, against the same ambient state,
which is what :func:`caller_has_scope` is for.

One consequence worth knowing: FastMCP's in-memory transport is neither
stdio nor authenticated, so an in-process host embedding this server
(``Client(mcp)``) gets an empty tool list until it opts out with
``set_auth_enforced(enforced=False)``. That is the correct trust model —
the host process is the boundary, as it is under stdio — but it is not
self-evident, so the first denial logs an explanation.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio.to_thread
import structlog
from fastmcp.server.auth import AccessToken, TokenVerifier

from trellis.auth import ALL_SCOPES, clearance_of, scopes_satisfy
from trellis.auth import verify_token as verify_api_key
from trellis.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastmcp.server.auth import AuthContext

    from trellis.schemas.classification import Sensitivity
    from trellis.stores.base.api_key import ApiKeyRecord, ApiKeyStore

logger = structlog.get_logger(__name__)

#: ``stdio`` (default) | ``http``. Anything else refuses to start.
TRANSPORT_ENV = "TRELLIS_MCP_TRANSPORT"
HOST_ENV = "TRELLIS_MCP_HOST"
PORT_ENV = "TRELLIS_MCP_PORT"
PATH_ENV = "TRELLIS_MCP_PATH"

#: ``off`` | ``required`` (default). Read only under the http transport.
#: Deliberately *not* ``TRELLIS_AUTH_MODE``: that one infers ``required``
#: from the legacy ``TRELLIS_API_KEY`` shared secret, so sharing it would
#: make enabling REST auth silently flip the MCP surface's posture.
AUTH_MODE_ENV = "TRELLIS_MCP_AUTH_MODE"

#: Permit a non-loopback bind while auth is ``off``. Unrecognised values
#: read as "not allowed" — the escape hatch fails closed.
ALLOW_INSECURE_BIND_ENV = "TRELLIS_MCP_ALLOW_INSECURE_BIND"

TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"
_VALID_TRANSPORTS = frozenset({TRANSPORT_STDIO, TRANSPORT_HTTP})

AUTH_MODE_OFF = "off"
AUTH_MODE_REQUIRED = "required"
#: No ``optional`` mode. It exists over REST to migrate an installed base
#: of un-credentialed callers; this surface is new and has none.
_VALID_AUTH_MODES = frozenset({AUTH_MODE_OFF, AUTH_MODE_REQUIRED})

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8421
#: Matches FastMCP's own ``streamable_http_path`` default.
DEFAULT_PATH = "/mcp"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_MIN_PORT = 1
_MAX_PORT = 65535


@dataclass(frozen=True)
class HttpSettings:
    """Resolved listener configuration for the ``http`` transport."""

    host: str
    port: int
    path: str
    auth_mode: str

    @property
    def auth_enforced(self) -> bool:
        return self.auth_mode == AUTH_MODE_REQUIRED


def _resolve_enum_env(env_var: str, valid: frozenset[str], default: str) -> str:
    """Resolve a lowercase-enum env var, raising loudly on a bad value.

    Unset / empty → ``default``. Any other unrecognised value is a
    ``ConfigError`` rather than a silent guess — the shared skeleton
    behind :func:`resolve_transport` and :func:`resolve_mcp_auth_mode`.
    """
    raw = os.environ.get(env_var)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value not in valid:
        msg = (
            f"Invalid {env_var}={raw!r}; expected one of"
            f" {sorted(valid)}. Refusing to guess."
        )
        raise ConfigError(msg, setting=env_var)
    return value


def resolve_transport() -> str:
    """Return the effective transport, raising loudly on a bad value.

    Unset / empty → ``stdio``, preserving the historical behaviour of a
    bare ``trellis-mcp`` invocation.
    """
    return _resolve_enum_env(TRANSPORT_ENV, _VALID_TRANSPORTS, TRANSPORT_STDIO)


def resolve_mcp_auth_mode() -> str:
    """Return the effective MCP auth mode, raising loudly on a bad value.

    Unset / empty → ``required``. The http transport is opt-in, so a
    deployment that reaches this code has already chosen to listen on a
    socket; defaulting that to unauthenticated would be the wrong way
    round.
    """
    return _resolve_enum_env(AUTH_MODE_ENV, _VALID_AUTH_MODES, AUTH_MODE_REQUIRED)


def _is_loopback(host: str) -> bool:
    """Whether ``host`` names only the local machine.

    ``""`` is deliberately NOT loopback: an empty bind host means
    ``INADDR_ANY`` (all interfaces), the most exposed bind, so the
    fail-closed check must treat it as routable.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname we can't classify without resolving it. Treat as
        # routable — the fail-closed check should err toward refusing.
        return False


def _resolve_port() -> int:
    raw = os.environ.get(PORT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_PORT
    try:
        port = int(raw.strip())
    except ValueError:
        msg = f"Invalid {PORT_ENV}={raw!r}; expected an integer."
        raise ConfigError(msg, setting=PORT_ENV) from None
    if not _MIN_PORT <= port <= _MAX_PORT:
        msg = f"Invalid {PORT_ENV}={raw!r}; expected {_MIN_PORT}..{_MAX_PORT}."
        raise ConfigError(msg, setting=PORT_ENV)
    return port


def resolve_http_settings() -> HttpSettings:
    """Resolve and validate the http listener configuration.

    Fail-closed bind: an unauthenticated server may only listen on
    loopback. Binding a routable interface with ``auth_mode=off``
    requires setting :data:`ALLOW_INSECURE_BIND_ENV` explicitly, matching
    the posture the REST security model already takes.
    """
    host = (os.environ.get(HOST_ENV) or DEFAULT_HOST).strip() or DEFAULT_HOST
    path = (os.environ.get(PATH_ENV) or DEFAULT_PATH).strip() or DEFAULT_PATH
    port = _resolve_port()
    auth_mode = resolve_mcp_auth_mode()

    if auth_mode == AUTH_MODE_OFF and not _is_loopback(host):
        allowed = os.environ.get(ALLOW_INSECURE_BIND_ENV, "").strip().lower() in _TRUTHY
        if not allowed:
            msg = (
                f"Refusing to bind {host}:{port} with {AUTH_MODE_ENV}=off — that"
                " serves the whole memory system unauthenticated to the network."
                f" Set {AUTH_MODE_ENV}=required and mint a key with 'trellis admin"
                f" api-keys create', or set {ALLOW_INSECURE_BIND_ENV}=1 if the"
                " listener is genuinely on a trusted interface."
            )
            raise ConfigError(msg, setting=AUTH_MODE_ENV)
        logger.warning(
            "mcp_insecure_bind_allowed",
            host=host,
            port=port,
            override=ALLOW_INSECURE_BIND_ENV,
            message="MCP is listening off-loopback with authentication disabled.",
        )

    return HttpSettings(host=host, port=port, path=path, auth_mode=auth_mode)


@dataclass
class _AuthState:
    """Whether per-tool scope checks are enforced in this process.

    Defaults to ``True`` so a server constructed outside :func:`main` —
    an ad-hoc ``mcp.http_app()``, a misconfigured deployment — fails
    closed rather than serving every tool to an anonymous caller.
    :func:`main` relaxes it only for an explicit ``auth_mode=off``.

    Note this is invisible to the stdio transport, where FastMCP skips
    the checks entirely. It bites exactly one case: an **in-process**
    host driving the server over FastMCP's in-memory transport, which is
    neither stdio nor authenticated. Such a host sees an empty tool list
    until it calls :func:`set_auth_enforced` — so warn once, loudly,
    rather than leaving it to guess.
    """

    enforced: bool = True
    warned_anonymous: bool = False


_auth_state = _AuthState()


def set_auth_enforced(*, enforced: bool) -> None:
    """Set whether :func:`trellis_scope` checks enforce scopes.

    In-process hosts that embed the server over FastMCP's in-memory
    transport should call ``set_auth_enforced(enforced=False)``: the
    parent process is the trust boundary there, exactly as it is under
    stdio, and there is no credential to present.
    """
    _auth_state.enforced = enforced
    _auth_state.warned_anonymous = False


def _warn_anonymous_once() -> None:
    """Explain the empty tool list the first time we deny an anonymous call."""
    if _auth_state.warned_anonymous:
        return
    _auth_state.warned_anonymous = True
    logger.warning(
        "mcp_tool_denied_anonymous",
        message=(
            "Denying tools to a caller with no credential. Over http, mint a "
            "key with 'trellis admin api-keys create' and send it as "
            "'Authorization: Bearer <token>'. If you are embedding the server "
            "in-process, call trellis.mcp.auth.set_auth_enforced(enforced=False) "
            "— the host process is the trust boundary there."
        ),
    )


def trellis_scope(required: str) -> Callable[[AuthContext], bool]:
    """Build a FastMCP ``AuthCheck`` requiring the ``required`` scope.

    Raises :class:`ValueError` at decoration time on an unknown scope, so
    a typo at a tool's call site crashes import rather than silently
    denying (or allowing) every request to that tool.

    Under stdio this check is never invoked — FastMCP short-circuits with
    ``skip_auth=True``.
    """
    if required not in ALL_SCOPES:
        msg = f"Unknown scope {required!r}; known scopes: {sorted(ALL_SCOPES)}"
        raise ValueError(msg)

    def check(ctx: AuthContext) -> bool:
        if not _auth_state.enforced:
            return True
        token = ctx.token
        if token is None:
            _warn_anonymous_once()
            return False
        return scopes_satisfy(frozenset(token.scopes), required)

    check.__name__ = f"require_scope_{required}"
    return check


def caller_has_scope(scope: str) -> bool:
    """Whether the *current* MCP caller holds *scope* (``admin`` implies all).

    Unlike :func:`trellis_scope`, which FastMCP calls *before* a tool
    runs and which is never invoked at all under stdio, this reads
    ambient state from **inside** a tool's function body. Reach for it
    when a scope decision depends on something only the tool body knows
    — e.g. one destructive operation inside an otherwise ``mutate``-
    scoped batch — rather than something knowable at decoration time.
    General-purpose and meant to be reused by future per-call checks
    (read-side included), not special-cased to any one tool.

    * **stdio** — always ``True``. FastMCP's own dispatch (see
      ``fastmcp.server.server._get_auth_context``) treats stdio as
      ``skip_auth=True`` for exactly this reason: the parent agent host
      *is* the trust boundary there, and there is no credential to read.
      Determined the same way FastMCP determines it internally — the
      ``_current_transport`` context var set by the stdio run-loop and
      the http request path — never by guessing from an environment
      variable, since the transport layer owns none.
    * **enforcement disabled** (:func:`set_auth_enforced`) — always
      ``True``, the same escape hatch :func:`trellis_scope` honours, for
      the same reason: an in-process host driving this server over
      FastMCP's in-memory transport is its own trust boundary too, and
      has no credential to present either.
    * **otherwise (http, enforced)** — read the bearer's
      :class:`~fastmcp.server.auth.AccessToken` via
      :func:`fastmcp.server.dependencies.get_access_token` and check its
      scopes with :func:`trellis.auth.scopes_satisfy`. No token, or a
      token lacking *scope*, is refused. Fails closed: the absence of
      proof of a scope is never treated as the presence of it.

    Raises :class:`ValueError` at the call site on an unknown scope, the
    same contract :func:`trellis_scope` and :func:`scopes_satisfy` take.
    """
    if scope not in ALL_SCOPES:
        msg = f"Unknown scope {scope!r}; known scopes: {sorted(ALL_SCOPES)}"
        raise ValueError(msg)

    # Mirrors fastmcp.server.server._get_auth_context's own stdio test —
    # late import avoids a cycle with fastmcp.server.context at module
    # load, same reason FastMCP's own code imports it late there.
    from fastmcp.server.context import _current_transport  # noqa: PLC0415

    if _current_transport.get() == TRANSPORT_STDIO:
        return True
    if not _auth_state.enforced:
        return True

    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    if token is None:
        return False
    return scopes_satisfy(frozenset(token.scopes), scope)


def current_clearance() -> Sensitivity:
    """The highest document sensitivity the *current* MCP caller may read (#194).

    Same transport detection as :func:`caller_has_scope`. **stdio** and
    **enforcement disabled** are trusted-local: the parent host is the trust
    boundary and holds the store anyway, so the caller reads everything
    (``"restricted"``). Otherwise the bearer's scopes decide
    (:func:`trellis.auth.clearance_of`), and a call with no credential reads
    ``"internal"``: it cannot normally reach a tool at all, and if it does,
    fail closed.
    """
    from fastmcp.server.context import _current_transport  # noqa: PLC0415

    if _current_transport.get() == TRANSPORT_STDIO or not _auth_state.enforced:
        return "restricted"

    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return clearance_of(token.scopes) if token is not None else "internal"


def current_principal_id() -> str | None:
    """The current MCP caller's key id, or ``None`` with no credential to derive from.

    Mirrors :func:`caller_has_scope`'s own transport detection exactly,
    for the same reason an audited identity needs it: **stdio** and
    **enforcement disabled** both mean there is no credential to derive
    an identity from (the parent host is the trust boundary), so both
    return ``None`` -- a caller reading this is expected to fall back to
    its own caller-supplied audit label in that case, unchanged from
    before this function existed. Under **http, enforced**, the bearer's
    :class:`~fastmcp.server.auth.AccessToken` carries ``client_id``,
    which :func:`TrellisApiKeyVerifier.verify_token` sets to the
    presented key's ``ApiKeyRecord.key_id`` -- the MCP-transport
    equivalent of the REST boundary's ``AuthContext.key_id``.
    """
    from fastmcp.server.context import _current_transport  # noqa: PLC0415

    if _current_transport.get() == TRANSPORT_STDIO:
        return None
    if not _auth_state.enforced:
        return None

    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.client_id if token is not None else None


class TrellisApiKeyVerifier(TokenVerifier):
    """Verify ``Authorization: Bearer trellis_ak_…`` against the key store.

    Returning ``None`` for every failure mode — malformed, unknown,
    revoked, secret mismatch, store outage — is deliberate: FastMCP
    answers a bare 401, so a caller cannot probe which key ids exist.
    :func:`trellis.auth.verify_token` logs the real category server-side.

    Claude Code sends a static bearer token and treats a 401 as a failed
    connection rather than falling back to OAuth discovery, so no
    authorization-server metadata is advertised.
    """

    def __init__(
        self,
        store_provider: Callable[[], ApiKeyStore],
        *,
        base_url: str | None = None,
    ) -> None:
        super().__init__(base_url=base_url)
        self._store_provider = store_provider

    def _verify_sync(self, token: str) -> ApiKeyRecord | None:
        return verify_api_key(token, self._store_provider())

    async def verify_token(self, token: str) -> AccessToken | None:
        """Resolve a presented token to an :class:`AccessToken`, or ``None``."""
        try:
            # The key store is sync and does database I/O; keep it off the
            # event loop that is serving other tool calls.
            record = await anyio.to_thread.run_sync(self._verify_sync, token)
        except Exception as exc:
            # GRACEFUL-DEGRADATION: a store outage must read as "not
            # authenticated", never as "authenticated". Fail closed.
            #
            # Log the exception *type* only. ``logger.exception`` would
            # render this frame's locals — which include the caller's
            # bearer token — and ``str(exc)`` can carry a DSN with its
            # password. Same discipline as ``trellis.auth.verify_token``,
            # which logs a failure category and never the credential.
            logger.error(  # noqa: TRY400 — see above; no traceback on purpose
                "mcp_api_key_verify_error",
                error_type=type(exc).__name__,
            )
            return None

        if record is None:
            return None

        return AccessToken(
            token=token,
            client_id=record.key_id,
            scopes=list(record.scopes),
            expires_at=None,
        )
