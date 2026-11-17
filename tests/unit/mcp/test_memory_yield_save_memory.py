"""``analyze memory-yield`` classifies a real ``save_memory`` write by its surface.

``save_memory`` emits ``MEMORY_STORED`` with ``requested_by`` and also an
``evidence.ingest`` audit row; either way the report must place the document
under ``mcp:save_memory`` rather than ``unknown``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.analyze.memory_yield import BASIS_EVENT, summarize_memory_yield
from trellis.mcp.tools.memory import SAVE_MEMORY_SURFACE

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

save_memory = unwrap_tool(server_mod.save_memory)


def test_save_memory_is_classified_by_its_surface(
    temp_registry: StoreRegistry,
) -> None:
    save_memory(content="An invented fact about a fictional widget.")
    report = summarize_memory_yield(
        temp_registry.knowledge.document_store,
        temp_registry.operational.event_log,
        days=30,
    )
    paths = {row.path: row for row in report.paths}
    assert set(paths) == {SAVE_MEMORY_SURFACE}
    row = paths[SAVE_MEMORY_SURFACE]
    assert (row.basis, row.created, row.never_served) == (BASIS_EVENT, 1, 1)
