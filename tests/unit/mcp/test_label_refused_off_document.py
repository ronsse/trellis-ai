"""MCP ``execute_mutation`` refuses a sensitivity label on a non-document id (#1131).

The same rule as the REST and CLI doors (see
``tests/unit/api/test_label_refused_off_document.py``): a label is a property of
a live document, so a graph node, a trace and a deleted document's id are
refused for both label verbs, on the bearer and the stdio transport alike.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import trellis.mcp.server as server_mod
from tests.off_document_ids import (
    LIVE,
    OFF_DOCUMENT_KINDS,
    VERBS,
    assert_nothing_written,
    rejections,
    seed_ids,
    stored_label,
)
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import as_http, as_stdio

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

execute_mutation = unwrap_tool(server_mod.execute_mutation)


@pytest.mark.parametrize("transport", ["http", "stdio"])
@pytest.mark.parametrize("operation", sorted(VERBS))
@pytest.mark.parametrize("kind", OFF_DOCUMENT_KINDS)
def test_execute_mutation_refuses_an_id_that_is_not_a_document(
    temp_registry: StoreRegistry, kind: str, operation: str, transport: str
) -> None:
    ids = seed_ids(temp_registry)
    ctx = as_http(["mutate", "restricted"]) if transport == "http" else as_stdio()
    with ctx:
        out = json.loads(
            execute_mutation(
                operation=operation, args={"doc_id": ids[kind], **VERBS[operation]}
            )
        )
    assert out["status"] == "rejected", out
    assert_nothing_written(temp_registry, ids)
    audited = rejections(temp_registry)
    assert [e.payload.get("reason") for e in audited] == ["target_not_found"], [
        e.payload for e in audited
    ]


def test_a_live_document_still_takes_a_label(temp_registry: StoreRegistry) -> None:
    seed_ids(temp_registry)
    with as_http(["mutate", "restricted"]):
        out = json.loads(
            execute_mutation(
                operation="classification.set",
                args={"doc_id": LIVE, "sensitivity": "confidential"},
            )
        )
    assert out["status"] == "success", out
    assert stored_label(temp_registry, LIVE) == "confidential"
