"""Admin-scoped API-key tools.

``list_api_keys`` and ``revoke_api_key``.
"""

from __future__ import annotations

import json

import structlog

from trellis.auth import SCOPE_ADMIN, public_listing_row
from trellis.mcp.app import mcp
from trellis.mcp.auth import current_principal_id, trellis_scope
from trellis.mcp.boundary import _result_message
from trellis.mcp.wiring import _get_registry
from trellis.mutate import CommandStatus, build_api_keys_executor
from trellis.mutate.api_key_commands import build_revoke_command

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# API-key administration. Admin scope only, so an ``external`` key (which
# satisfies ``read`` and nothing else) is refused by the scope check. There is
# deliberately NO mint tool: a token is a credential, and minting one over a
# channel an agent drives would hand the agent a way to widen its own access.
# Minting stays on ``trellis admin api-keys create`` and the admin REST route.
# ---------------------------------------------------------------------------


#: The one text ``list_api_keys`` answers an escaped exception with.
_LIST_API_KEYS_FAULT = "The listing failed on the server. See the server log."


@mcp.tool(auth=trellis_scope(SCOPE_ADMIN))
def list_api_keys() -> str:
    """List every API key, live and revoked. Never returns a secret or its hash.

    Returns:
        A JSON object with ``status``, ``keys`` (each ``key_id``, ``name``,
        ``scopes``, ``created_at``, ``revoked``, ``revoked_at``) and ``count``,
        or ``message`` on failure. This tool never raises to MCP.
    """
    try:
        rows = [
            public_listing_row(r)
            for r in _get_registry().operational.api_key_store.list()
        ]
    # GRACEFUL-DEGRADATION: MCP tool surface -- never raises to the client.
    except Exception as exc:
        # A fixed text for the reason ``revoke_api_key`` gives below.
        logger.exception("list_api_keys_failed", error_type=type(exc).__name__)
        return json.dumps({"status": "error", "message": _LIST_API_KEYS_FAULT})
    return json.dumps({"status": "ok", "keys": rows, "count": len(rows)})


#: The one text ``revoke_api_key`` answers an escaped exception with. Neutral
#: about the key's state: the fault can land after the revoke committed.
_REVOKE_API_KEY_FAULT = (
    "The revoke failed on the server and its outcome is unknown. Check the key "
    "with list_api_keys before retrying, and see the server log."
)


@mcp.tool(auth=trellis_scope(SCOPE_ADMIN))
def revoke_api_key(key_id: str) -> str:
    """Revoke a live API key, through the governed mutation pipeline.

    An unknown or already-revoked key id is refused, and so is revoking the
    key this session is authenticated with. A caller the server cannot
    identify (a stdio session, or HTTP with enforcement off) is refused too,
    and pointed at ``trellis admin api-keys revoke``: revoking a credential
    needs a named actor on the audit event.

    Args:
        key_id: The 12-hex-char key id (see ``list_api_keys``).

    Returns:
        A JSON object with ``status`` (``ok`` on success), ``key_id``, and
        ``message`` plus ``reason`` when refused, and ``warnings`` when the
        revoke took effect but its audit event was not recorded. This tool
        never raises to MCP.
    """
    principal = current_principal_id()
    if principal is None:
        logger.warning("api_key_write_refused_unidentified", action="revoke")
        return json.dumps(
            {
                "status": "rejected",
                "reason": "unidentified_caller",
                "message": (
                    "Revoking an API key needs an identified admin credential, "
                    "and this session has none. Use "
                    "'trellis admin api-keys revoke <key_id>' on the server host."
                ),
            }
        )
    try:
        command = build_revoke_command(
            key_id,
            requested_by="mcp:revoke_api_key",
            actor_key_id=principal,
        )
        result = build_api_keys_executor(_get_registry()).execute(command)
    # GRACEFUL-DEGRADATION: MCP tool surface -- never raises to the client.
    except Exception as exc:
        # The caller gets a fixed text, as REST's server fault does: what
        # escapes here is a store's or driver's own error (a host, a user, a
        # file path), which ``_exception_detail``'s deny-list lets through,
        # and the tool is admin-scoped, so the caller is trusted with the
        # outcome but not with the server's topology. The class name and the
        # traceback go to the operator's log only.
        logger.exception("revoke_api_key_failed", error_type=type(exc).__name__)
        return json.dumps({"status": "error", "message": _REVOKE_API_KEY_FAULT})

    if result.status != CommandStatus.SUCCESS:
        return json.dumps(
            {
                "status": result.status.value,
                "message": _result_message(result),
                "reason": result.metadata.get("rejection_reason"),
            }
        )
    return json.dumps(
        {
            "status": "ok",
            "key_id": key_id,
            **({"warnings": list(result.warnings)} if result.warnings else {}),
        }
    )
