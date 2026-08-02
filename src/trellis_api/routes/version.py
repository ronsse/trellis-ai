"""Version handshake route.

Exposes :data:`trellis.api_version` constants.  Lives at
``/api/version`` — deliberately *outside* the ``/api/v1`` prefix
because it's meta-info about which major/minor is running, not itself
versioned.

This route never touches the store layer and is safe to call without
auth (when auth is eventually added, the version route should stay
public so clients can check compatibility before authenticating).
"""

from __future__ import annotations

from fastapi import APIRouter

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
from trellis_wire.dtos import VersionResponse

router = APIRouter()


@router.get("/api/version", response_model=VersionResponse, tags=["version"])
def api_version() -> VersionResponse:
    """Return API version metadata for client compatibility checks.

    SDK clients call this on first use.  Static — no IO, no store
    access — so it's cheap to poll and safe to leave public.

    Also carries ``write_provenance``: the build identity and
    write-behaviour flags this server stamps onto every event it emits.
    A container image that has drifted from the host working tree is
    otherwise invisible; this makes it one request away.  Resolved once
    per process, so the route stays IO-free.
    """
    return VersionResponse(
        api_major=API_MAJOR,
        api_minor=API_MINOR,
        api_version=api_version_string(),
        wire_schema=WIRE_SCHEMA,
        sdk_min=SDK_MIN,
        package_version=get_version(),
        mcp_tools_version=MCP_TOOLS_VERSION,
        write_provenance=dict(get_write_provenance()),
    )
