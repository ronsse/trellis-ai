"""Version handshake route.

Exposes :data:`trellis.api_version` constants.  Lives at
``/api/version`` — deliberately *outside* the ``/api/v1`` prefix
because it's meta-info about which major/minor is running, not itself
versioned.

The compatibility fields never touch the store layer and are safe to
call without auth: clients must be able to check compatibility before
authenticating.  ``write_provenance`` is ops detail (build sha, effective
write-behaviour environment) and follows the same posture as the
``/readyz`` backend breakdown — see :func:`api_version`.
"""

from __future__ import annotations

import copy

from fastapi import APIRouter, Depends

from trellis.api_version import (
    API_MAJOR,
    API_MINOR,
    MCP_TOOLS_VERSION,
    SDK_MIN,
    WIRE_SCHEMA,
    api_version_string,
)
from trellis.core.base import get_version
from trellis.core.write_provenance import get_write_provenance
from trellis_api.auth import AuthContext, authenticate_optional
from trellis_api.routes.health import OPS_DETAIL_PUBLIC, resolve_ops_detail
from trellis_wire.dtos import VersionResponse

router = APIRouter()


@router.get("/api/version", response_model=VersionResponse, tags=["version"])
def api_version(
    ctx: AuthContext | None = Depends(authenticate_optional),  # noqa: B008 — FastAPI DI idiom
) -> VersionResponse:
    """Return API version metadata for client compatibility checks.

    SDK clients call this on first use.  The compatibility fields are
    static — no IO, no store access — so it's cheap to poll and stays
    public.

    ``write_provenance`` — the build identity and write-behaviour
    environment this server stamps onto every event it emits — is ops
    detail, and is gated exactly like the ``/readyz`` backend breakdown:
    authenticated callers get it, and so does everyone when the effective
    auth mode is permissive or ``TRELLIS_OPS_DETAIL=public``.  A container
    image that has drifted from the host working tree is otherwise
    invisible, and the deployments that most need to see it are the
    unauthenticated dev/LAN ones; a deployment that has chosen
    ``TRELLIS_AUTH_MODE=required`` gets to keep its commit sha and enabled
    ingest behaviours off an anonymous response.

    An image built by ``make docker-build`` cannot drift — code and
    metadata are frozen together — so the stamp's ``stamp_stale`` /
    ``source_tree_commit`` keys are absent here in the deployment shape
    this route was written for.  They appear when the API is served from
    an editable install whose working tree has moved on.
    """
    provenance = None
    if ctx is not None or resolve_ops_detail() == OPS_DETAIL_PUBLIC:
        provenance = copy.deepcopy(get_write_provenance())
    return VersionResponse(
        api_major=API_MAJOR,
        api_minor=API_MINOR,
        api_version=api_version_string(),
        wire_schema=WIRE_SCHEMA,
        sdk_min=SDK_MIN,
        package_version=get_version(),
        mcp_tools_version=MCP_TOOLS_VERSION,
        write_provenance=provenance,
    )
