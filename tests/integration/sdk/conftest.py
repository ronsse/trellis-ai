"""SDK-side fixtures for live HTTP round-trip tests.

The shared ``live_api_server`` fixture (see
``tests/integration/_live_server.py``) spawns the same uvicorn process
the API smoke matrix uses. Tests here drive the public Python SDK
clients — :class:`trellis_sdk.TrellisClient` and
:class:`trellis_sdk.AsyncTrellisClient` — against that real HTTP
target. They prove the SDK round-trips real httpx connection pooling,
TLS handshake (when run against an https endpoint), and JSON decode
paths that the in-process Starlette ``TestClient`` skips.
"""

from __future__ import annotations

# Re-import the live_api_server fixture so pytest registers it in this
# conftest's namespace. Cross-directory fixture sharing isn't automatic;
# this is the standard pattern.
from tests.integration._live_server import live_api_server  # noqa: F401
