"""Thin wrapper around :class:`trellis_sdk.TrellisClient`.

Purpose of wrapping rather than using ``TrellisClient`` directly:

* One place to set org defaults (base URL, timeouts, user-agent).
* One place to plug in future auth (Bearer token, AWS SigV4) when it
  lands upstream — every caller already goes through this constructor.
* One place to override the SDK version-handshake behavior if you need
  to relax it during a rollout.

The ``factory()`` helper gives you three modes depending on how your
code path was invoked:

* ``factory()`` with ``TRELLIS_URL`` set → remote mode against that URL.
* ``factory(in_memory=True)`` → an ASGI-backed in-process client for
  tests and demos (no server process needed).
* ``factory(base_url=...)`` → explicit override (useful for notebooks).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from trellis_sdk import TrellisClient

_DEFAULT_TIMEOUT_SECONDS = 30.0

# Update this if/when your org standardizes on a base URL for prod.
_ENV_BASE_URL = "TRELLIS_URL"


@contextmanager
def factory(
    *,
    base_url: str | None = None,
    in_memory: bool = False,
    stores_dir: Path | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> Iterator[TrellisClient]:
    """Yield a configured :class:`TrellisClient`, cleaning up on exit.

    Precedence for the base URL: explicit ``base_url`` arg →
    ``TRELLIS_URL`` env var → error (must pick one or set
    ``in_memory=True``).
    """
    if in_memory:
        from trellis.testing import in_memory_client  # noqa: PLC0415

        if stores_dir is None:
            import tempfile  # noqa: PLC0415

            with (
                tempfile.TemporaryDirectory() as scratch,
                in_memory_client(Path(scratch) / "stores") as client,
            ):
                yield client
            return
        with in_memory_client(stores_dir) as client:
            yield client
        return

    url = base_url or os.environ.get(_ENV_BASE_URL)
    if not url:
        msg = (
            f"No base URL. Pass base_url= or set {_ENV_BASE_URL}=... "
            "in the environment; or use in_memory=True for local runs."
        )
        raise RuntimeError(msg)

    client = TrellisClient(base_url=url, timeout=timeout)
    try:
        yield client
    finally:
        client.close()


__all__ = ["factory"]
