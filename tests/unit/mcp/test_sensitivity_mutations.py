"""MCP ``execute_mutation`` honours the bearer's clearance (#194, #1023)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import as_http, as_stdio
from tests.unit.retrieve.test_sensitivity import label
from trellis.schemas.classification import sensitivity_of

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

execute_mutation = unwrap_tool(server_mod.execute_mutation)

BODY = "failover runbook zebra ledger: rotate the vault seal keys. " * 4
MUTATE = ["mutate"]
MUTATE_RESTRICTED = ["mutate", "restricted"]


def _ingest(doc_id: str, level: str | None = None) -> dict[str, Any]:
    return {
        "evidence": {
            "doc_id": doc_id,
            "content": "replacement",
            "metadata": label(level) if level else {},
        }
    }


def _run(operation: str, args: dict[str, Any]) -> dict[str, Any]:
    return json.loads(execute_mutation(operation=operation, args=args))


def _level(registry: StoreRegistry, doc_id: str) -> str:
    row = registry.knowledge.document_store.get(doc_id)
    assert row is not None
    return sensitivity_of(row["metadata"])


class TestEvidenceIngest:
    def test_a_token_below_the_label_cannot_overwrite(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("doc", BODY, label("restricted"))
        with as_http(MUTATE):
            out = _run("evidence.ingest", _ingest("doc"))
        assert out["status"] == "rejected"
        stored = temp_registry.knowledge.document_store.get("doc")
        assert stored is not None
        assert stored["content"] == BODY
        assert _level(temp_registry, "doc") == "restricted"

    def test_a_token_below_the_label_cannot_overwrite_at_the_same_label(
        self, temp_registry: StoreRegistry
    ) -> None:
        """No downgrade here, so only the clearance check can refuse."""
        temp_registry.knowledge.document_store.put("doc", BODY, label("confidential"))
        with as_http(MUTATE):
            out = _run("evidence.ingest", _ingest("doc", "confidential"))
        assert out["status"] == "rejected"
        stored = temp_registry.knowledge.document_store.get("doc")
        assert stored is not None
        assert stored["content"] == BODY

    def test_a_cleared_token_cannot_lower_the_label(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("doc", BODY, label("restricted"))
        with as_http(MUTATE_RESTRICTED):
            out = _run("evidence.ingest", _ingest("doc", "confidential"))
        assert out["status"] == "rejected"
        assert _level(temp_registry, "doc") == "restricted"

    @pytest.mark.parametrize("written", ["confidential", "restricted"])
    def test_a_cleared_token_may_rewrite_at_the_same_or_a_higher_label(
        self, temp_registry: StoreRegistry, written: str
    ) -> None:
        temp_registry.knowledge.document_store.put("doc", BODY, label("confidential"))
        with as_http(MUTATE_RESTRICTED):
            out = _run("evidence.ingest", _ingest("doc", written))
        assert out["status"] == "success", out
        assert _level(temp_registry, "doc") == written

    def test_a_trusted_stdio_caller_still_may_not_lower_a_label(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("doc", BODY, label("restricted"))
        with as_stdio():
            out = _run("evidence.ingest", _ingest("doc"))
        assert out["status"] == "rejected"
        assert _level(temp_registry, "doc") == "restricted"


class TestClassificationSet:
    def test_a_token_without_restricted_cannot_relabel_a_restricted_doc(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("doc", BODY, label("restricted"))
        with as_http(["mutate", "confidential"]):
            out = _run(
                "classification.set", {"doc_id": "doc", "sensitivity": "internal"}
            )
        assert out["status"] == "rejected"
        assert _level(temp_registry, "doc") == "restricted"

    def test_a_restricted_token_relabels_and_a_read_token_loses_the_document(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("doc", BODY, {"title": "t"})
        get_context = unwrap_tool(server_mod.get_context)
        with as_http(["read"]):
            assert "zebra ledger" in get_context("failover runbook")
        with as_http(MUTATE_RESTRICTED):
            out = _run(
                "classification.set", {"doc_id": "doc", "sensitivity": "restricted"}
            )
        assert out["status"] == "success", out
        with as_http(["read"]):
            assert "zebra ledger" not in get_context("failover runbook")
        with as_http(["read", "restricted"]):
            assert "zebra ledger" in get_context("failover runbook")
