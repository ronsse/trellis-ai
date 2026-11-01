"""A registry whose operational ``event_log`` accessor fails on demand.

``StoreRegistry.operational.event_log`` is a property that opens the backing
file, so "the event log would not open" is a failure of the *access*, and a
second access can succeed where the first did not. Patching ``emit`` cannot
express that; wrapping the accessor can.
"""

from __future__ import annotations

import sqlite3
from typing import Any

DRIVER_TEXT = "disk I/O error SECRET-DRIVER-TEXT"


class _FlakyOperational:
    def __init__(self, inner: Any, fail_first: int) -> None:
        self._inner = inner
        self._left = fail_first

    @property
    def event_log(self) -> Any:
        if self._left > 0:
            self._left -= 1
            raise sqlite3.OperationalError(DRIVER_TEXT)
        return self._inner.event_log

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class FlakyRegistry:
    """Delegates to ``inner``; the first ``fail_first`` event-log accesses raise."""

    def __init__(self, inner: Any, *, fail_first: int) -> None:
        self._inner = inner
        self.operational = _FlakyOperational(inner.operational, fail_first)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
