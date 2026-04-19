"""API version constants — single source of truth.

The REST API, the SDK, and the ``trellis admin version`` CLI all import
from this module so the three stay in lockstep.  Bump these when the
contract changes; the version-handshake endpoint (``GET /api/version``)
surfaces them to clients for compatibility checks.

Compatibility rules:

* **api_major** — bump on a breaking change to any ``/api/v<major>/*``
  route.  Clients refuse to talk to a server with a different major.
* **api_minor** — bump on a backwards-compatible addition.  Clients
  warn when the server minor is older than the one they were built
  against (feature may be missing) but continue.
* **wire_schema** — the Pydantic-level DTO schema version.  Independent
  of ``api_major`` because additive field changes can be backwards
  compatible at the wire level even when a route signature changes.
* **sdk_min** — the oldest SDK version the server will accept.  Older
  SDKs may be missing required fields the server now expects.

See [TODO.md — Client Boundary & Extension Contracts — Phase 1 Plan,
Step 1](../../TODO.md#step-1--api-version-handshake--static-openapi-in-ci).
"""

from __future__ import annotations

from trellis.core.base import SCHEMA_VERSION

# Bump when a /api/v<major>/ route breaks backwards compatibility.
API_MAJOR = 1

# Bump on backwards-compatible additions (new routes, new optional
# fields, new enum values that default safely).  Reset to 0 when
# API_MAJOR bumps.
API_MINOR = 0

# The wire-level Pydantic schema version. Sourced from trellis.core.base
# so the DTOs and the handshake stay in sync automatically.
WIRE_SCHEMA = SCHEMA_VERSION

# Minimum SDK package version that can speak this API.  SDKs older
# than this are rejected by the version handshake.
SDK_MIN = "0.1.0"

# MCP tool surface version — versioned independently from API_MAJOR.
#
# The MCP server (``trellis.mcp``) is a separate, narrower contract:
# ~8 agent-shaped tools consumed by LLM agents via the Model Context
# Protocol.  It evolves on its own cadence — adding or changing a
# tool doesn't have to move the REST API major, and moving the REST
# API major doesn't invalidate deployed MCP clients.  Bump this when:
#
# * A tool is removed or renamed (breaking).
# * A tool's arguments or return shape change in a non-additive way.
#
# Additive changes (new tool, new optional argument) don't require a
# bump.  See ``docs/design/adr-mcp-contract.md``.
MCP_TOOLS_VERSION = 1


def api_version_string() -> str:
    """Return the conventional ``"<major>.<minor>"`` string."""
    return f"{API_MAJOR}.{API_MINOR}"


__all__ = [
    "API_MAJOR",
    "API_MINOR",
    "MCP_TOOLS_VERSION",
    "SDK_MIN",
    "WIRE_SCHEMA",
    "api_version_string",
]
