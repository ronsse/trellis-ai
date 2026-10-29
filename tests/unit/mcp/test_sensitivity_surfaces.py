"""Every MCP read surface withholds a document above the caller's clearance (#194).

The ambient state a tool reads (transport, bearer scopes) is installed the
way ``test_execute_mutation.py`` does it; the tools are called unwrapped, so
only the clearance decides, never the per-tool scope gate.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp.server.auth import AccessToken
from fastmcp.server.context import reset_transport, set_transport
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.retrieve.test_sensitivity import label
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.schemas.classification import sensitivity_of

if TYPE_CHECKING:
    from collections.abc import Iterator

    from trellis.stores.registry import StoreRegistry

get_context = unwrap_tool(server_mod.get_context)
get_sectioned_context = unwrap_tool(server_mod.get_sectioned_context)
search = unwrap_tool(server_mod.search)
get_items = unwrap_tool(server_mod.get_items)
get_file_context = unwrap_tool(server_mod.get_file_context)
save_memory = unwrap_tool(server_mod.save_memory)
save_knowledge = unwrap_tool(server_mod.save_knowledge)

INTENT = "failover runbook"
OPEN_TEXT = "failover runbook open: drain the write queue, promote the replica. " * 4
SECRET_TEXT = "failover runbook zebra ledger: rotate the vault seal keys. " * 4


@contextlib.contextmanager
def as_http(scopes: list[str] | None) -> Iterator[None]:
    transport = set_transport("streamable-http")
    auth = None
    if scopes is not None:
        auth = auth_context_var.set(
            AuthenticatedUser(
                AccessToken(
                    token="opaque",  # noqa: S106 -- placeholder, never read
                    client_id="key-id",
                    scopes=scopes,
                    expires_at=None,
                )
            )
        )
    try:
        yield
    finally:
        reset_transport(transport)
        if auth is not None:
            auth_context_var.reset(auth)


@contextlib.contextmanager
def as_stdio() -> Iterator[None]:
    transport = set_transport("stdio")
    try:
        yield
    finally:
        reset_transport(transport)


READ = ["read"]
ADMIN = ["admin"]
RESTRICTED = ["read", "restricted"]


def _seed(
    registry: StoreRegistry, secret_metadata: dict[str, Any] | None = None
) -> None:
    store = registry.knowledge.document_store
    store.put("doc-open", OPEN_TEXT, {"title": "Open", "source_path": "svc/open.py"})
    store.put(
        "doc-secret",
        SECRET_TEXT,
        {
            "title": "Secret",
            "source_path": "svc/secret.py",
            **(label("restricted") if secret_metadata is None else secret_metadata),
        },
    )


@pytest.fixture
def seeded(temp_registry: StoreRegistry) -> StoreRegistry:
    _seed(temp_registry)
    return temp_registry


def _callers() -> list[Any]:
    return [as_http(ADMIN), as_http(RESTRICTED), as_stdio()]


class TestPackSurfaces:
    @pytest.mark.parametrize(
        "call",
        [
            lambda: get_context(INTENT),
            lambda: get_sectioned_context(INTENT, sections=[{"name": "All"}]),
            lambda: search(INTENT),
        ],
        ids=["get_context", "get_sectioned_context", "search"],
    )
    def test_a_read_token_never_sees_the_restricted_document(
        self, seeded: StoreRegistry, call: Any
    ) -> None:
        with as_http(READ):
            result = call()
        assert "zebra ledger" not in result
        assert "doc-secret" not in result
        assert "open" in result, "the unlabelled document must still be served"

    @pytest.mark.parametrize(
        "call",
        [
            lambda: get_context(INTENT),
            lambda: get_sectioned_context(INTENT, sections=[{"name": "All"}]),
            lambda: search(INTENT),
        ],
        ids=["get_context", "get_sectioned_context", "search"],
    )
    def test_admin_restricted_and_stdio_callers_do(
        self, seeded: StoreRegistry, call: Any
    ) -> None:
        for ctx in _callers():
            with ctx:
                assert "zebra ledger" in call()

    def test_the_note_counts_withheld_items_without_naming_them(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http(READ):
            result = get_context(INTENT)
        assert "sensitivity 1" in result
        assert "doc-secret" not in result

    def test_a_malformed_label_reads_as_restricted(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry, {"data_classification": "oops"})
        with as_http(READ):
            assert "zebra ledger" not in get_context(INTENT)
        with as_stdio():
            assert "zebra ledger" in get_context(INTENT)

    def test_an_unlabelled_store_is_identical_for_a_read_token(
        self, temp_registry: StoreRegistry
    ) -> None:
        """Transparency: no label anywhere, no caller sees a difference."""
        store = temp_registry.knowledge.document_store
        store.put("doc-a", OPEN_TEXT, {"title": "A"})
        store.put("doc-b", SECRET_TEXT, {"title": "B"})
        with as_http(READ):
            read = get_context(INTENT)
        with as_http(ADMIN):
            admin = get_context(INTENT)
        for result in (read, admin):
            assert "zebra ledger" in result
            assert "Withheld" not in result


class TestFetchSurfaces:
    def test_get_items_answers_a_withheld_id_exactly_as_not_found(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http(READ):
            withheld = get_items(["doc-secret"])
            missing = get_items(["doc-ghost"])
        assert "zebra ledger" not in withheld
        assert withheld.replace("doc-secret", "X") == missing.replace("doc-ghost", "X")

    def test_get_items_serves_it_to_a_cleared_caller(
        self, seeded: StoreRegistry
    ) -> None:
        for ctx in _callers():
            with ctx:
                assert "zebra ledger" in get_items(["doc-secret"])

    def test_get_file_context_leaves_the_document_out(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http(READ):
            result = get_file_context(["svc/secret.py"])
        assert "zebra ledger" not in result
        assert "doc-secret" not in result
        with as_http(ADMIN):
            assert "doc-secret" in get_file_context(["svc/secret.py"])

    def test_get_file_context_still_serves_the_unlabelled_one(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http(READ):
            assert "doc-open" in get_file_context(["svc/open.py"])


class TestProducer:
    def test_a_read_ingest_key_cannot_label_above_its_clearance(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(["read", "ingest"]), pytest.raises(McpError, match="clearance"):
            save_memory("quarterly vault seal rotation notes", sensitivity="restricted")
        assert temp_registry.knowledge.document_store.count(include_chunks=True) == 0, (
            "a refused save must write nothing"
        )

    def test_an_admin_save_succeeds_and_stamps_the_label(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(ADMIN):
            reply = save_memory(
                "quarterly vault seal rotation notes", sensitivity="restricted"
            )
        doc_id = reply.split("Memory saved: ")[1].split()[0]
        doc = temp_registry.knowledge.document_store.get(doc_id)
        assert doc is not None
        assert sensitivity_of(doc["metadata"]) == "restricted"

    def test_a_confidential_key_can_label_confidential_but_not_restricted(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(["ingest", "confidential"]):
            assert "Memory saved" in save_memory("one doc", sensitivity="confidential")
            with pytest.raises(McpError, match="clearance"):
                save_memory("another doc", sensitivity="restricted")

    def test_an_unknown_label_is_refused(self, temp_registry: StoreRegistry) -> None:
        with as_http(ADMIN), pytest.raises(McpError, match="sensitivity"):
            save_memory("some content", sensitivity="top-secret")

    def test_a_raw_label_in_metadata_is_refused(
        self, temp_registry: StoreRegistry
    ) -> None:
        """Otherwise the clearance check has a side door."""
        with as_http(["read", "ingest"]), pytest.raises(McpError, match="sensitivity"):
            save_memory("some content", metadata=label("restricted"))

    def test_no_label_leaves_the_stored_metadata_untouched(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(READ):
            reply = save_memory("plain note", metadata={"title": "t"})
        doc = temp_registry.knowledge.document_store.get(
            reply.split("Memory saved: ")[1].split()[0]
        )
        assert doc is not None
        assert "data_classification" not in doc["metadata"]

    def test_a_labelled_save_does_not_trigger_extraction(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(server_mod, "_get_memory_extractor", lambda _r: object())
        monkeypatch.setattr(
            server_mod, "run_memory_extraction", lambda *a, **k: calls.append("ran")
        )
        with as_http(ADMIN):
            plain = save_memory("an ordinary note about queues")
            labelled = save_memory("a vault note", sensitivity="confidential")
        assert calls == ["ran"], (
            "the unlabelled save must extract, or this proves nothing"
        )
        assert "extraction skipped" in labelled
        assert "extraction skipped" not in plain

    def test_a_duplicate_of_an_unreadable_document_does_not_echo_its_id(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http(READ):
            reply = save_memory(SECRET_TEXT)
        assert "doc-secret" not in reply
        assert reply.startswith("Memory saved")

    def test_a_labelled_save_is_refused_over_a_lower_labelled_duplicate(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(ADMIN):
            save_memory("shared sentence about queues and replicas")
            with pytest.raises(McpError, match="lower"):
                save_memory(
                    "shared sentence about queues and replicas",
                    sensitivity="restricted",
                )

    def test_save_knowledge_labels_the_evidence_document(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(ADMIN):
            reply = save_knowledge(
                "VaultSeal", content="seal rotation procedure", sensitivity="restricted"
            )
        doc_id = reply.split("Evidence document: ")[1].split()[0]
        doc = temp_registry.knowledge.document_store.get(doc_id)
        assert doc is not None
        assert sensitivity_of(doc["metadata"]) == "restricted"

    def test_save_knowledge_label_above_clearance_is_refused(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(["ingest"]), pytest.raises(McpError, match="clearance"):
            save_knowledge(
                "VaultSeal", content="seal rotation", sensitivity="restricted"
            )

    def test_save_knowledge_refuses_a_label_without_content(
        self, temp_registry: StoreRegistry
    ) -> None:
        with as_http(ADMIN), pytest.raises(McpError, match="sensitivity"):
            save_knowledge("VaultSeal", sensitivity="restricted")

    def test_save_knowledge_cannot_point_at_an_unreadable_document(
        self, seeded: StoreRegistry
    ) -> None:
        """Linking to it must be indistinguishable from a missing id."""
        with as_http(READ):
            with pytest.raises(McpError) as hidden:
                save_knowledge("X", evidence_ref="doc-secret")
            with pytest.raises(McpError) as missing:
                save_knowledge("X", evidence_ref="doc-ghost")
        assert str(hidden.value).replace("doc-secret", "I") == str(
            missing.value
        ).replace("doc-ghost", "I")


WRITER = ["read", "ingest"]
REPLACEMENT = "unrelated replacement text about queue depth dashboards"


class TestOverwriteOfALabelledDocument:
    """A caller-chosen ``doc_id`` cannot replace a row the caller may not read (#194).

    The put replaces the whole row, metadata included, so an unguarded re-put
    would reset the label to the default and publish the document's chunks.
    """

    def _stored(self, registry: StoreRegistry) -> dict[str, Any]:
        doc = registry.knowledge.document_store.get("doc-secret")
        assert doc is not None
        return doc

    def test_a_key_below_the_label_cannot_replace_the_document(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http(WRITER), pytest.raises(McpError, match="clearance") as refused:
            save_memory(REPLACEMENT, doc_id="doc-secret")
        stored = self._stored(seeded)
        assert stored["content"] == SECRET_TEXT
        assert sensitivity_of(stored["metadata"]) == "restricted"
        assert "zebra ledger" not in str(refused.value), "the refusal must not echo"
        assert "restricted" not in str(refused.value), "nor name the label"

    def test_the_chunks_stay_withheld_after_the_refused_save(
        self, seeded: StoreRegistry
    ) -> None:
        chunk_id = chunk_doc_id("doc-secret", 0)
        seeded.knowledge.document_store.put(chunk_id, SECRET_TEXT, {})
        with as_http(WRITER), pytest.raises(McpError):
            save_memory(REPLACEMENT, doc_id="doc-secret")
        with as_http(READ):
            withheld = get_items([chunk_id])
            missing = get_items(["doc-ghost#chunk-0"])
        assert "zebra ledger" not in withheld
        assert withheld.replace(chunk_id, "X") == missing.replace(
            "doc-ghost#chunk-0", "X"
        )

    def test_a_cleared_key_may_not_lower_the_label(self, seeded: StoreRegistry) -> None:
        with as_http([*RESTRICTED, "ingest"]), pytest.raises(McpError, match="lower"):
            save_memory(REPLACEMENT, doc_id="doc-secret")
        stored = self._stored(seeded)
        assert stored["content"] == SECRET_TEXT
        assert sensitivity_of(stored["metadata"]) == "restricted"

    def test_a_cleared_key_may_not_lower_it_to_a_lower_named_label(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http(ADMIN), pytest.raises(McpError, match="lower"):
            save_memory(REPLACEMENT, doc_id="doc-secret", sensitivity="confidential")
        assert sensitivity_of(self._stored(seeded)["metadata"]) == "restricted"

    def test_a_cleared_key_may_replace_at_the_same_label(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http(ADMIN):
            reply = save_memory(
                REPLACEMENT, doc_id="doc-secret", sensitivity="restricted"
            )
        assert "Memory saved" in reply
        stored = self._stored(seeded)
        assert stored["content"] == REPLACEMENT
        assert sensitivity_of(stored["metadata"]) == "restricted"

    def test_a_cleared_key_may_raise_the_label(
        self, temp_registry: StoreRegistry
    ) -> None:
        store = temp_registry.knowledge.document_store
        store.put("doc-note", "an internal note about queues", {})
        with as_http(ADMIN):
            save_memory(REPLACEMENT, doc_id="doc-note", sensitivity="confidential")
        doc = store.get("doc-note")
        assert doc is not None
        assert sensitivity_of(doc["metadata"]) == "confidential"

    def test_a_stdio_caller_is_never_refused(self, seeded: StoreRegistry) -> None:
        with as_stdio():
            save_memory(REPLACEMENT, doc_id="doc-secret", sensitivity="restricted")
        assert self._stored(seeded)["content"] == REPLACEMENT

    def test_an_unlabelled_document_is_still_replaceable_by_a_read_key(
        self, seeded: StoreRegistry
    ) -> None:
        """The guard is about labels: an ``internal`` row is the default posture."""
        with as_http(WRITER):
            assert "Memory saved" in save_memory(REPLACEMENT, doc_id="doc-open")
        doc = seeded.knowledge.document_store.get("doc-open")
        assert doc is not None
        assert doc["content"] == REPLACEMENT

    def test_a_new_id_is_unaffected(self, temp_registry: StoreRegistry) -> None:
        with as_http(WRITER):
            assert "Memory saved" in save_memory(REPLACEMENT, doc_id="doc-fresh")
