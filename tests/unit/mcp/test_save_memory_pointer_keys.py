"""MCP ``save_memory`` and the pointer keys (#1057).

The pack builder withholds items *by id*, so a document whose ``lifecycle``
says ``superseded`` withholds every item under its id (a graph node's
included), and a document's ``evidence_ref`` makes it the successor another
item's ``superseded_by`` resolves to. ``save_memory`` stored its ``metadata``
argument as given, so either key reached an item other than the memory. The
tool now refuses both before anything is read or written, as ``POST
/documents`` does. The governed writers are untouched: ``supersedes=`` and a
reconcile SUPERSEDE verdict stamp the target through their own commands, and
``save_knowledge`` writes ``evidence_ref`` on its own graph node. Real SQLite
stores and the real tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_reconcile import FakeLLMClient, _enable, _verdict_json
from tests.unit.mcp.test_sensitivity_surfaces import as_http, as_stdio
from trellis.schemas.classification import LIFECYCLE_KEY

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

save_memory = unwrap_tool(server_mod.save_memory)
save_knowledge = unwrap_tool(server_mod.save_knowledge)

OLD = "failover runbook old: drain the write queue, promote the replica. " * 4
NEW = "failover runbook new: drain the write queue, then promote. " * 4
OTHER = "capacity notes: the batch window opens at two in the morning. " * 4
MUTATE = ["mutate"]
SUPERSEDED = {"state": "superseded", "superseded_by": "doc-fresh"}

#: A near-duplicate pair, so the reconcile tier surfaces the first as the
#: candidate when the second is written.
BASE = "The staging gateway listens on port 9450 for inbound sync jobs."
NEAR = "The staging gateway listens on port 9451 for inbound sync jobs."


def _refused(**kwargs: Any) -> McpError:
    with as_http(MUTATE), pytest.raises(McpError) as err:
        save_memory(**kwargs)
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


def _doc_id(reply: str) -> str:
    return reply.splitlines()[0].rsplit(": ", 1)[1].strip()


@pytest.fixture(autouse=True)
def _seeded(temp_registry: StoreRegistry) -> None:
    temp_registry.knowledge.document_store.put("doc-old", OLD, {"title": "Old"})
    temp_registry.knowledge.graph_store.upsert_node(
        node_id="entity-1", node_type="service", properties={"name": "svc"}
    )


class TestAPointerKeyIsRefused:
    @pytest.mark.parametrize(
        "lifecycle",
        [
            SUPERSEDED,
            {"state": "archived"},
            {"state": "demoted"},
            {"state": "current"},
            {},
            "current",
            None,
        ],
        ids=[
            "superseded",
            "archived",
            "demoted",
            "current",
            "empty-record",
            "string",
            "null",
        ],
    )
    @pytest.mark.parametrize(
        "doc_id", ["doc-fresh", "doc-old", "entity-1", None], ids=str
    )
    def test_a_lifecycle_is_refused_and_nothing_is_written(
        self, temp_registry: StoreRegistry, lifecycle: Any, doc_id: str | None
    ) -> None:
        before = _footprint(temp_registry, "doc-fresh", "doc-old", "entity-1")
        err = _refused(content=NEW, doc_id=doc_id, metadata={LIFECYCLE_KEY: lifecycle})
        assert "metadata.lifecycle may not be set" in err.error.message
        assert "Nothing was written" in err.error.message
        assert err.error.data == {"field": "metadata.lifecycle", "tool": "save_memory"}
        assert _footprint(temp_registry, "doc-fresh", "doc-old", "entity-1") == before

    @pytest.mark.parametrize("value", ["doc-open", None, "", {"id": "doc-open"}])
    @pytest.mark.parametrize("doc_id", ["doc-fresh", "doc-old", None])
    def test_an_evidence_ref_is_refused_and_nothing_is_written(
        self, temp_registry: StoreRegistry, value: Any, doc_id: str | None
    ) -> None:
        before = _footprint(temp_registry, "doc-fresh", "doc-old")
        err = _refused(
            content=NEW,
            doc_id=doc_id,
            metadata={"title": "t", "evidence_ref": value},
        )
        assert "metadata.evidence_ref may not be set" in err.error.message
        assert "Nothing was written" in err.error.message
        assert err.error.data == {
            "field": "metadata.evidence_ref",
            "tool": "save_memory",
        }
        assert _footprint(temp_registry, "doc-fresh", "doc-old") == before

    def test_the_refusal_names_the_governed_paths(self) -> None:
        message = _refused(content=NEW, metadata={LIFECYCLE_KEY: SUPERSEDED}).error
        assert "save_memory(supersedes=" in message.message
        assert "retention.restore" in message.message

    def test_stdio_is_held_to_the_same_rule(self, temp_registry: StoreRegistry) -> None:
        before = _footprint(temp_registry, "doc-old")
        with as_stdio(), pytest.raises(McpError) as err:
            save_memory(
                content=NEW, doc_id="doc-old", metadata={LIFECYCLE_KEY: SUPERSEDED}
            )
        assert "metadata.lifecycle may not be set" in err.value.error.message
        assert _footprint(temp_registry, "doc-old") == before

    def test_an_exact_content_match_is_not_a_way_past_it(
        self, temp_registry: StoreRegistry
    ) -> None:
        """The refusal comes before the dedup lookups, so it does not depend on
        whether the content happens to match a stored document."""
        before = _footprint(temp_registry, "doc-old")
        _refused(content=OLD, metadata={LIFECYCLE_KEY: SUPERSEDED})
        assert _footprint(temp_registry, "doc-old") == before

    def test_a_supersedes_call_cannot_carry_one_either(
        self, temp_registry: StoreRegistry
    ) -> None:
        before = _footprint(temp_registry, "doc-old")
        _refused(
            content=NEW,
            supersedes="doc-old",
            metadata={LIFECYCLE_KEY: SUPERSEDED},
        )
        assert _footprint(temp_registry, "doc-old") == before

    def test_the_reconcile_judge_is_never_asked(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        judge = FakeLLMClient(content=_verdict_json("add"))
        _enable(monkeypatch, judge)
        before = _footprint(temp_registry, "doc-old")
        _refused(content=NEW, metadata={"evidence_ref": "doc-open"})
        assert judge.calls == 0
        assert _footprint(temp_registry, "doc-old") == before


class TestWhatStaysAccepted:
    def test_ordinary_metadata_is_stored_without_a_pointer_key(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(MUTATE):
            reply = save_memory(
                content=OTHER,
                metadata={"title": "Plain", "source_system": "notes", "tags": ["a"]},
            )
        stored = temp_registry.knowledge.document_store.get(_doc_id(reply))
        assert stored is not None
        assert stored["metadata"]["title"] == "Plain"
        assert LIFECYCLE_KEY not in stored["metadata"]
        assert "evidence_ref" not in stored["metadata"]

    @pytest.mark.parametrize("metadata", [None, {}], ids=["none", "empty"])
    def test_no_metadata_is_accepted(
        self, temp_registry: StoreRegistry, metadata: dict[str, Any] | None
    ) -> None:
        with as_http(MUTATE):
            reply = save_memory(content=OTHER, metadata=metadata)
        assert temp_registry.knowledge.document_store.get(_doc_id(reply)) is not None

    def test_supersedes_still_stamps_its_target(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(MUTATE):
            reply = save_memory(
                content=NEW, supersedes="doc-old", metadata={"title": "New"}
            )
        new_id = _doc_id(reply)
        docs = temp_registry.knowledge.document_store
        stamped = docs.get("doc-old")["metadata"][LIFECYCLE_KEY]
        assert stamped["state"] == "superseded"
        assert stamped["superseded_by"] == new_id
        # The new memory carries no lifecycle of its own.
        assert LIFECYCLE_KEY not in docs.get(new_id)["metadata"]

    def test_a_reconcile_supersede_still_writes_its_keys_beside_caller_metadata(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with as_http(MUTATE):
            base_id = _doc_id(save_memory(content=BASE))
            _enable(monkeypatch, FakeLLMClient(content=_verdict_json("supersede")))
            reply = save_memory(content=NEAR, metadata={"title": "Gateway"})
        new_id = _doc_id(reply)
        docs = temp_registry.knowledge.document_store
        lifecycle = docs.get(base_id)["metadata"][LIFECYCLE_KEY]
        assert lifecycle["state"] == "superseded"
        assert lifecycle["superseded_by"] == new_id
        new_meta = docs.get(new_id)["metadata"]
        assert new_meta["title"] == "Gateway"
        assert new_meta["reconciliation"] == "supersede"
        assert new_meta["supersedes_doc_id"] == base_id

    def test_save_knowledge_still_sets_evidence_ref_on_its_node(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(MUTATE):
            reply = save_knowledge(name="gateway port", content=OTHER)
        node_line = next(
            line for line in reply.splitlines() if line.startswith("Entity created: ")
        )
        node_id = node_line.removeprefix("Entity created: ").split()[0]
        node = temp_registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"]["evidence_ref"]
