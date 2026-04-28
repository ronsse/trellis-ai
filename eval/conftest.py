"""Pytest fixtures shared across eval scenarios.

Scenario-specific fixtures live alongside each scenario; this file is for
fixtures every scenario can reuse (in-memory registry, temp data dirs,
deterministic seeds).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from trellis.stores.registry import StoreRegistry


@pytest.fixture
def sqlite_registry(tmp_path: Path) -> Iterator[StoreRegistry]:
    """A throw-away SQLite-backed registry rooted at ``tmp_path``.

    Suitable for scenarios that don't care about backend behaviour. For
    multi-backend equivalence work, build the registry inside the
    scenario itself so the configuration is explicit.
    """
    config = {
        "knowledge": {
            "graph": {"backend": "sqlite"},
            "vector": {"backend": "sqlite"},
            "document": {"backend": "sqlite"},
            "blob": {"backend": "local"},
        },
        "operational": {
            "trace": {"backend": "sqlite"},
            "event_log": {"backend": "sqlite"},
        },
    }
    with StoreRegistry(config=config, stores_dir=tmp_path) as registry:
        yield registry
