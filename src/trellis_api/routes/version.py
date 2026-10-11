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
import os
from pathlib import Path

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
from trellis.core.write_config import WriteBehaviourConfig, resolve_overridden_by
from trellis.core.write_provenance import get_write_provenance
from trellis.stores.settings_store import load_settings_values
from trellis_api.auth import AuthContext, authenticate_optional
from trellis_api.routes.health import OPS_DETAIL_PUBLIC, resolve_ops_detail
from trellis_wire.dtos import VersionResponse

router = APIRouter()


def _resolve_stores_dir() -> Path:
    """``<data_dir>/stores``, read the same two env vars registry.py does.

    Deliberately **not** ``StoreRegistry.from_config_dir(...).stores_dir``:
    this route has no store dependency today
    (``test_version_route_needs_no_store`` — calling it twice with no
    store fixture configured must not raise) and a settings-aware read
    must not add one. Mirrors
    :meth:`trellis.stores.registry.StoreRegistry.from_config_dir`'s own
    inline resolution rather than importing it, which is the same
    ``TRELLIS_CONFIG_DIR`` / ``TRELLIS_DATA_DIR`` pair with the same
    defaults.
    """
    default_config_dir = str(Path.home() / ".trellis")
    config_dir = Path(os.environ.get("TRELLIS_CONFIG_DIR", default_config_dir))
    data_dir = Path(os.environ.get("TRELLIS_DATA_DIR", str(config_dir / "data")))
    return data_dir / "stores"


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

    ``write_behaviour_settings`` is gated the same way, for the same
    reason — a knob's ``overridden_by`` sits beside its env-sourced
    counterpart in ``write_provenance.env_flags``, so splitting the gate
    between them would publish half the same fact.  Unlike
    ``write_provenance`` (a stamp frozen once per process) this is read
    fresh from ``<data_dir>/stores/settings.json`` on every call, same as
    ``trellis admin write-config`` — this process's container may have
    no store configured at all, in which case every row reports
    ``"env"``/``"default"`` and never ``"settings"``.
    """
    provenance = None
    settings_rows = None
    if ctx is not None or resolve_ops_detail() == OPS_DETAIL_PUBLIC:
        provenance = copy.deepcopy(get_write_provenance())
        settings_values = load_settings_values(_resolve_stores_dir())
        config = WriteBehaviourConfig.from_env_and_settings(settings=settings_values)
        settings_rows = config.describe(
            overridden_by=resolve_overridden_by(settings=settings_values)
        )
    return VersionResponse(
        api_major=API_MAJOR,
        api_minor=API_MINOR,
        api_version=api_version_string(),
        wire_schema=WIRE_SCHEMA,
        sdk_min=SDK_MIN,
        package_version=get_version(),
        mcp_tools_version=MCP_TOOLS_VERSION,
        write_provenance=provenance,
        write_behaviour_settings=settings_rows,
    )
