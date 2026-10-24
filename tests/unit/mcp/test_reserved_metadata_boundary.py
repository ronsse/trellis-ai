"""MCP side of the reserved-metadata guard: there is no live vector to fix.

``execute_mutation`` has no caller-settable ``metadata`` parameter at
all. Its ``Command.metadata`` is always this server's own
``{"requested_by_claim": ...}`` or ``{}`` (see the tool's own docstring),
so ``trellis.mutate.reject_reserved_metadata`` has no call site here the
way REST ``/commands/batch`` needs one for its per-item ``metadata`` body
field -- applying the guard to this function's own server-built dict
would reject its own legitimate ``requested_by_claim`` key.

The one caller-reachable field shaped like "metadata" near this tool is
``args["evidence"]["metadata"]`` on ``operation="evidence.ingest"`` --
but that is the *document's own content metadata*
(``build_evidence_ingest_command``'s ``metadata=`` parameter, stored on
the created document), never ``Command.metadata`` (its disjoint
``command_metadata=`` parameter, always server-built here). This file
proves that boundary holds: a caller planting a reserved-key-shaped
value inside the evidence payload lands only on the document, never on
the audit event -- so there is nothing for the REST guard's MCP
counterpart to refuse, because the forgery this guard exists to stop has
no path to begin with.

Lives in its own module, matching ``test_execute_mutation.py``'s own
stated convention of staying file-disjoint from the broader test surface.
"""

from __future__ import annotations

import json

import pytest
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.core.hashing import content_hash
from trellis.mcp.server import execute_mutation as _execute_mutation
from trellis.mutate import RESERVED_COMMAND_METADATA_KEYS
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

execute_mutation = unwrap_tool(_execute_mutation)
save_memory = unwrap_tool(server_mod.save_memory)


class TestEvidenceMetadataNeverPromotesToCommandMetadata:
    def test_a_reserved_key_shape_in_evidence_metadata_lands_on_the_document_only(
        self, temp_registry: StoreRegistry
    ) -> None:
        for key in sorted(RESERVED_COMMAND_METADATA_KEYS):
            payload = json.loads(
                execute_mutation(
                    operation="evidence.ingest",
                    args={
                        "evidence": {
                            "content": f"probe for {key}",
                            "metadata": {key: "attacker-planted"},
                        }
                    },
                )
            )
            assert payload["status"] == "success", payload

            stored = temp_registry.knowledge.document_store.get(payload["created_id"])
            assert stored["metadata"][key] == "attacker-planted"

            events = temp_registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED, limit=100
            )
            command_id = payload["command_id"]
            matching = [e for e in events if e.payload.get("command_id") == command_id]
            assert len(matching) == 1
            # The planted value is on the document, never surfaced as an
            # authoritative audit field -- requested_by_claim is the only
            # key this payload ever carries, and only when overridden.
            assert matching[0].payload.get(key) != "attacker-planted"
            if key == "requested_by_claim":
                assert "requested_by_claim" not in matching[0].payload


class TestSaveMemoryRefusesTheReservedPrecedentNamespace:
    """``save_memory(doc_id=...)`` is the one agent-facing way to name a document.

    A memory filed under ``precedent://learning/`` would inherit the promoted-
    precedent exemptions (noise pass, retention, classify refresh), so the
    evidence handler refuses the id and this surface must report it, with
    nothing written.

    Defective subject: drop the ``reserved_document_id`` refusal in
    ``EvidenceIngestHandler`` and this call stores the row.
    """

    RESERVED_ID = "precedent://learning/mine"

    def test_a_reserved_doc_id_is_rejected_and_nothing_is_stored(
        self, temp_registry: StoreRegistry
    ) -> None:
        with pytest.raises(McpError) as excinfo:
            save_memory("a generic note about widgets", doc_id=self.RESERVED_ID)

        assert "reserved precedent namespace" in excinfo.value.error.message
        assert temp_registry.knowledge.document_store.get(self.RESERVED_ID) is None
        assert (
            temp_registry.knowledge.document_store.get_by_hash(
                content_hash("a generic note about widgets")
            )
            is None
        )

    def test_an_ordinary_doc_id_is_still_accepted(
        self, temp_registry: StoreRegistry
    ) -> None:
        result = save_memory("a generic note about gadgets", doc_id="note-gadgets")

        assert "note-gadgets" in result
        assert temp_registry.knowledge.document_store.get("note-gadgets") is not None
