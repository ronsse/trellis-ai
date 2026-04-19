"""Trellis SDK — HTTP-only client for the Trellis REST API.

Zero runtime dependency on ``trellis`` core: the SDK talks to a
running ``trellis-api`` instance (or an in-process ASGI-transport
shim for tests — see :func:`trellis.testing.in_memory_client`).

This boundary is enforced by a structural test in
``tests/unit/sdk/test_isolation.py``.
"""

from trellis_sdk.async_client import AsyncTrellisClient
from trellis_sdk.client import TrellisClient
from trellis_sdk.exceptions import (
    TrellisAPIError,
    TrellisError,
    TrellisRateLimitError,
    TrellisVersionMismatchError,
)

__all__ = [
    "AsyncTrellisClient",
    "TrellisAPIError",
    "TrellisClient",
    "TrellisError",
    "TrellisRateLimitError",
    "TrellisVersionMismatchError",
]
