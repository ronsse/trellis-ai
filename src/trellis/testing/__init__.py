"""Test helpers for spinning up an in-process Trellis runtime.

The HTTP-only SDK needs a live API endpoint to talk to.  In tests we
don't want to bind a port — so these helpers mount the FastAPI app
via ``httpx.ASGITransport`` and return an SDK client that speaks
directly to it.  Round-trip in microseconds, no network, no flaky
port allocation.

Usage::

    from trellis.testing import in_memory_client

    def test_foo(tmp_path):
        with in_memory_client(tmp_path / "stores") as client:
            trace_id = client.ingest_trace({...})

This module lives inside ``trellis`` core (not ``trellis_sdk``) on
purpose: it imports from both sides (``StoreRegistry`` + FastAPI app
from core, ``TrellisClient`` from SDK), which is exactly the
layering the in-memory shim is for.
"""

from trellis.testing.inmemory import (
    in_memory_async_client,
    in_memory_client,
)

__all__ = ["in_memory_async_client", "in_memory_client"]
