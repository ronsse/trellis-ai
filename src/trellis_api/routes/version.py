"""Version handshake route.

Exposes :data:`trellis.api_version` constants and the current
:data:`trellis_api.deprecation.ROUTE_DEPRECATIONS` list.  Lives at
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
    SDK_MIN,
    WIRE_SCHEMA,
    api_version_string,
)
from trellis.core.base import get_version
from trellis_api.deprecation import ROUTE_DEPRECATIONS
from trellis_api.models import DeprecationNotice, VersionResponse

router = APIRouter()


@router.get("/api/version", response_model=VersionResponse, tags=["version"])
def api_version() -> VersionResponse:
    """Return API version metadata for client compatibility checks.

    SDK clients call this on first use.  Static — no IO, no store
    access — so it's cheap to poll and safe to leave public.
    """
    deprecations = [
        DeprecationNotice(
            path=path,
            deprecated_since=entry.deprecated_since.isoformat(),
            sunset_on=entry.sunset_on.isoformat(),
            replacement=entry.replacement,
            reason=entry.reason,
        )
        for path, entry in ROUTE_DEPRECATIONS.items()
    ]
    return VersionResponse(
        api_major=API_MAJOR,
        api_minor=API_MINOR,
        api_version=api_version_string(),
        wire_schema=WIRE_SCHEMA,
        sdk_min=SDK_MIN,
        package_version=get_version(),
        deprecations=deprecations,
    )
