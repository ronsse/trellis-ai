"""MCP ``execute_mutation`` ``evidence.ingest`` and the pointer keys (#1057).

The pack builder withholds items by id, so a document whose ``lifecycle`` says
``superseded`` withholds every item under its id and a document's
``evidence_ref`` makes it the successor another item names. A caller may not
use either key to withhold or redirect. The documented operator restore sends a
document's metadata bag back with ``lifecycle`` set to ``current``, so that form
and a round-tripped, unchanged ``evidence_ref`` stay accepted. Real SQLite
stores and the real handler.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import as_http, as_stdio
from tests.unit.retrieve.test_sensitivity import label
from trellis.schemas.classification import LIFECYCLE_KEY

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

execute_mutation = unwrap_tool(server_mod.execute_mutation)
save_memory = unwrap_tool(server_mod.save_memory)

OLD = "failover runbook old: drain the write queue, promote the replica. " * 4
NEW = "failover runbook new: drain the write queue, then promote. " * 4
MUTATE = ["mutate"]
REF = "doc-successor"
SUPERSEDED = {"state": "superseded", "superseded_by": "doc-fresh"}


def _ingest(
    metadata: Any, doc_id: str = "doc-fresh", content: str = NEW
) -> dict[str, Any]:
    return {"evidence": {"doc_id": doc_id, "content": content, "metadata": metadata}}


def _run(args: dict[str, Any]) -> dict[str, Any]:
    return json.loads(execute_mutation(operation="evidence.ingest", args=args))


def _refused(args: dict[str, Any]) -> McpError:
    with as_http(MUTATE), pytest.raises(McpError) as err:
        execute_mutation(operation="evidence.ingest", args=args)
    assert err.value.error.code == INVALID_PARAMS
    return err.value


def _footprint(registry: StoreRegistry, *doc_ids: str) -> dict[str, Any]:
    """Everything a write could touch: documents, vectors and events."""
    knowledge = registry.knowledge
    return {
        "documents": knowledge.document_store.count(),
        "rows": {d: knowledge.document_store.get(d) for d in doc_ids},
        "vectors": {d: knowledge.vector_store.get(d) for d in doc_ids},
        "events": registry.operational.event_log.count(),
    }


@pytest.fixture(autouse=True)
def _old_document(temp_registry: StoreRegistry) -> None:
    temp_registry.knowledge.document_store.put("doc-old", OLD, {"title": "Old"})


class TestAWithholdingLifecycleIsRefused:
    @pytest.mark.parametrize(
        "lifecycle",
        [
            SUPERSEDED,
            {"state": "archived"},
            {"state": "demoted"},
            {"state": "current", "superseded_by": "doc-fresh"},
            {"state": "current", "valid_until": "2020-01-01T00:00:00+00:00"},
            {"state": "current", "unknown_key": 1},
            {},
            "current",
            ["current"],
            None,
        ],
        ids=[
            "superseded",
            "archived",
            "demoted",
            "current-with-superseded-by",
            "current-with-valid-until",
            "current-with-unknown-key",
            "empty-record",
            "string",
            "list",
            "null",
        ],
    )
    @pytest.mark.parametrize("doc_id", ["doc-fresh", "doc-old"])
    def test_refused_and_nothing_is_written(
        self, temp_registry: StoreRegistry, lifecycle: Any, doc_id: str
    ) -> None:
        before = _footprint(temp_registry, "doc-fresh", "doc-old")
        err = _refused(_ingest({LIFECYCLE_KEY: lifecycle}, doc_id=doc_id))
        message = err.error.message
        assert "metadata.lifecycle may not be set" in message
        assert "save_memory(supersedes=" in message
        assert "retention.restore" in message
        assert '{"state": "current"}' in message
        assert err.error.data == {
            "field": "args.evidence.metadata.lifecycle",
            "operation": "evidence.ingest",
        }
        assert _footprint(temp_registry, "doc-fresh", "doc-old") == before

    def test_stdio_is_held_to_the_same_rule(self, temp_registry: StoreRegistry) -> None:
        before = _footprint(temp_registry, "doc-old")
        with as_stdio(), pytest.raises(McpError) as err:
            execute_mutation(
                operation="evidence.ingest",
                args=_ingest({LIFECYCLE_KEY: SUPERSEDED}, doc_id="doc-old"),
            )
        assert "metadata.lifecycle may not be set" in err.value.error.message
        assert _footprint(temp_registry, "doc-old") == before


class TestAnEvidenceRefThatIsNewOrChangedIsRefused:
    def test_a_new_evidence_ref_is_refused(self, temp_registry: StoreRegistry) -> None:
        before = _footprint(temp_registry, "doc-fresh")
        err = _refused(_ingest({"title": "x", "evidence_ref": REF}))
        assert "metadata.evidence_ref may not be set" in err.error.message
        assert "save_knowledge(evidence_ref=" in err.error.message
        assert err.error.data == {
            "field": "args.evidence.metadata.evidence_ref",
            "operation": "evidence.ingest",
        }
        assert _footprint(temp_registry, "doc-fresh") == before

    def test_a_new_evidence_ref_on_a_document_that_holds_none_is_refused(
        self, temp_registry: StoreRegistry
    ) -> None:
        before = _footprint(temp_registry, "doc-old")
        _refused(_ingest({"evidence_ref": REF}, doc_id="doc-old"))
        assert _footprint(temp_registry, "doc-old") == before

    def test_a_changed_evidence_ref_is_refused(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put(
            "doc-linked", OLD, {"title": "L", "evidence_ref": REF}
        )
        before = _footprint(temp_registry, "doc-linked")
        err = _refused(_ingest({"evidence_ref": "doc-elsewhere"}, doc_id="doc-linked"))
        assert "metadata.evidence_ref may not be set" in err.error.message
        assert _footprint(temp_registry, "doc-linked") == before

    @pytest.mark.parametrize("value", [None, "", 7, ["doc-old"]])
    def test_a_non_matching_value_is_refused(
        self, temp_registry: StoreRegistry, value: Any
    ) -> None:
        temp_registry.knowledge.document_store.put(
            "doc-linked", OLD, {"evidence_ref": REF}
        )
        _refused(_ingest({"evidence_ref": value}, doc_id="doc-linked"))
        stored = temp_registry.knowledge.document_store.get("doc-linked")
        assert stored is not None
        assert stored["metadata"] == {"evidence_ref": REF}

    def test_a_stored_value_above_the_bearers_clearance_reads_as_absent(
        self, temp_registry: StoreRegistry
    ) -> None:
        """No oracle: a bearer cannot learn a restricted document's ref."""
        temp_registry.knowledge.document_store.put(
            "doc-secret", OLD, {"evidence_ref": REF, **label("restricted")}
        )
        err = _refused(_ingest({"evidence_ref": REF}, doc_id="doc-secret"))
        assert "metadata.evidence_ref may not be set" in err.error.message


class TestWhatStaysAccepted:
    def test_a_round_tripped_evidence_ref_is_accepted(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put(
            "doc-linked", OLD, {"title": "L", "evidence_ref": REF}
        )
        with as_http(MUTATE):
            out = _run(
                _ingest(
                    {"title": "L2", "evidence_ref": REF},
                    doc_id="doc-linked",
                    content=NEW,
                )
            )
        assert out["status"] == "success", out
        stored = temp_registry.knowledge.document_store.get("doc-linked")
        assert stored is not None
        assert stored["content"] == NEW
        assert stored["metadata"]["evidence_ref"] == REF

    def test_a_current_lifecycle_is_accepted(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(MUTATE):
            out = _run(_ingest({LIFECYCLE_KEY: {"state": "current"}}, doc_id="doc-old"))
        assert out["status"] == "success", out

    def test_metadata_without_a_pointer_key_is_untouched(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(MUTATE):
            out = _run(_ingest({"title": "plain", "tags": ["a"]}))
        assert out["status"] == "success", out
        stored = temp_registry.knowledge.document_store.get("doc-fresh")
        assert stored is not None
        assert stored["metadata"]["title"] == "plain"

    def test_other_operations_are_not_inspected(
        self, temp_registry: StoreRegistry
    ) -> None:
        graph = temp_registry.knowledge.graph_store
        a = graph.upsert_node(node_id=None, node_type="concept", properties={})
        b = graph.upsert_node(node_id=None, node_type="concept", properties={})
        with as_http(MUTATE):
            out = json.loads(
                execute_mutation(
                    operation="link.create",
                    args={
                        "source_id": a,
                        "target_id": b,
                        "edge_kind": "entity_related_to",
                        "properties": {LIFECYCLE_KEY: SUPERSEDED, "evidence_ref": REF},
                    },
                )
            )
        assert out["status"] == "success", out

    def test_the_operator_restore_recipe_works_end_to_end(
        self, temp_registry: StoreRegistry
    ) -> None:
        """Stamp with ``save_memory(supersedes=)``, then undo it here."""
        with as_http(["ingest"]):
            reply = save_memory(content=NEW, supersedes="doc-old")
        assert "supersedes doc-old" in reply
        stamped = temp_registry.knowledge.document_store.get("doc-old")
        assert stamped is not None
        assert stamped["metadata"][LIFECYCLE_KEY]["state"] == "superseded"

        # The recipe: send the stamped bag back with the lifecycle set current.
        bag = {**stamped["metadata"], LIFECYCLE_KEY: {"state": "current"}}
        with as_http(MUTATE):
            out = _run(_ingest(bag, doc_id="doc-old", content=stamped["content"]))
        assert out["status"] == "success", out
        restored = temp_registry.knowledge.document_store.get("doc-old")
        assert restored is not None
        assert restored["metadata"][LIFECYCLE_KEY]["state"] == "current"
        assert restored["metadata"][LIFECYCLE_KEY].get("superseded_by") is None
