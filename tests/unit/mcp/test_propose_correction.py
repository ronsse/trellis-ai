"""MCP ``propose_correction``: a proposal is a record, and changes nothing served.

These drive the real tool and read the pack an agent would be served before and
after. The proposed text lives only in the ``CORRECTION_PROPOSED`` event, so the
servable-state tests compare every content store and the pack itself.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.shared.exceptions import MCPError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_external_key import _auth_context, _external_scopes
from tests.unit.mcp.test_sensitivity_surfaces import as_http, as_stdio
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import SCOPE_INGEST, SCOPE_READ
from trellis.feedback.corrections import (
    PROPOSAL_ID_PREFIX,
    REFUSAL_REASONS,
    find_proposal,
    list_proposals,
)
from trellis.mcp.auth import set_auth_enforced, trellis_scope
from trellis.mcp.server import mcp
from trellis.schemas.classification import LIFECYCLE_KEY, Lifecycle
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

propose_correction = unwrap_tool(server_mod.propose_correction)
get_context = unwrap_tool(server_mod.get_context)

TARGET = "doc-target"
T_TEXT = (
    "zebrafish calibration quillwort: settle window is forty milliseconds per "
    "the old bench notes for the tank rig"
)
#: A sentinel word only the corrected text carries.
X_TEXT = (
    "zebrafish calibration revised marigoldsentinel: the settle window is "
    "twenty-five milliseconds on the new rig, per the vendor sheet"
)
REASON = "the rig was replaced and the vendor sheet gives a shorter window"
INTENT = "zebrafish calibration settle window"

#: One clause changed: a near-duplicate of its original by construction.
LONG_OLD = (
    "zebrafish tank calibration procedure: warm the rig for ten minutes, zero "
    "the sensor array against the reference cell, record three baseline "
    "sweeps, then set the settle window to forty milliseconds before the "
    "first capture run begins"
)
LONG_NEW = LONG_OLD.replace("ten minutes", "eleven minutes")


def _seed(reg: StoreRegistry, **extra: Any) -> None:
    reg.knowledge.document_store.put(TARGET, T_TEXT, {"title": "Calibration", **extra})


def _pid(reply: str) -> str:
    start = reply.index("`") + 1
    return reply[start : reply.index("`", start)]


def _proposals(reg: StoreRegistry) -> list[Any]:
    return reg.operational.event_log.get_events(
        event_type=EventType.CORRECTION_PROPOSED, limit=100
    )


def _pack_ids(reg: StoreRegistry) -> list[str]:
    events = reg.operational.event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, order="desc", limit=1
    )
    assert events
    return [row["item_id"] for row in events[0].payload["injected_items"]]


def _content_state(reg: StoreRegistry) -> dict[str, Any]:
    """Every knowledge-plane fact a pack could be built from."""
    docs = reg.knowledge.document_store
    return {
        "doc_count": docs.count(),
        "target": docs.get(TARGET),
        "doc_ids": sorted(d["doc_id"] for d in docs.list_documents(limit=1000)),
        "vectors": reg.knowledge.vector_store.count(),
        "nodes": reg.knowledge.graph_store.count_nodes(),
        "edges": reg.knowledge.graph_store.count_edges(),
    }


def _refusal(**kwargs: Any) -> MCPError:
    with as_stdio(), pytest.raises(MCPError) as err:
        propose_correction(**kwargs)
    return err.value


class TestAnAcceptedProposalChangesNothingServed:
    def test_target_and_pack_are_the_same_before_and_after(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry)
        with as_stdio():
            before_ctx = get_context(INTENT)
            before_pack = _pack_ids(temp_registry)
            before = _content_state(temp_registry)
            reply = propose_correction(TARGET, X_TEXT, REASON, pack_id="pack-1")
            after_ctx = get_context(INTENT)
            after_pack = _pack_ids(temp_registry)
            sentinel_ctx = get_context("marigoldsentinel")
            sentinel_pack = _pack_ids(temp_registry)
        assert f"`{TARGET}`" in before_ctx
        assert before_pack == after_pack
        assert TARGET in after_pack
        assert _content_state(temp_registry) == before
        assert "marigoldsentinel" not in after_ctx
        # The header echoes the intent, so the served body is what is checked.
        assert "twenty-five milliseconds" not in sentinel_ctx
        assert sentinel_pack == []
        assert "pending review" in reply
        assert "unchanged" in reply

    def test_the_event_carries_the_named_fields_and_the_label(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry, **label("confidential"))
        with as_stdio():
            reply = propose_correction(
                TARGET, X_TEXT, REASON, pack_id="pack-1", evidence_uri="https://x.test"
            )
        pid = _pid(reply)
        assert pid.startswith(PROPOSAL_ID_PREFIX)
        (event,) = _proposals(temp_registry)
        assert event.entity_id == pid
        payload = event.payload
        assert payload["proposal_id"] == pid
        assert payload["target_id"] == TARGET
        assert payload["pack_id"] == "pack-1"
        assert payload["reason"] == REASON
        assert payload["proposed_by"]
        assert payload["content_length"] == len(X_TEXT)
        assert payload["content_digest"]
        assert payload["sensitivity"] == "confidential"
        assert payload["evidence_uri"] == "https://x.test"
        proposal = find_proposal(temp_registry.operational.event_log, pid)
        assert proposal is not None
        assert proposal.status == "pending"

    def test_the_write_is_governed(self, temp_registry: StoreRegistry) -> None:
        _seed(temp_registry)
        with as_stdio():
            pid = _pid(propose_correction(TARGET, X_TEXT, REASON))
        executed = temp_registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_EXECUTED, entity_id=pid, limit=10
        )
        assert [e.payload["operation"] for e in executed] == ["correction.propose"]


class TestEachRefusalIsNamed:
    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            ({"item_id": " ", "corrected_content": X_TEXT, "reason": REASON}, "blank"),
            ({"item_id": TARGET, "corrected_content": "", "reason": REASON}, "blank"),
            ({"item_id": TARGET, "corrected_content": X_TEXT, "reason": ""}, "blank"),
            (
                {"item_id": TARGET, "corrected_content": X_TEXT, "reason": "r" * 2001},
                "reason_too_long",
            ),
            (
                {"item_id": "doc-nope", "corrected_content": X_TEXT, "reason": REASON},
                "target_missing",
            ),
            (
                {"item_id": TARGET, "corrected_content": T_TEXT, "reason": REASON},
                "content_identical",
            ),
            (
                {
                    "item_id": TARGET,
                    "corrected_content": X_TEXT + " api_key=Zq7rT2mX9vL4pW8n",
                    "reason": REASON,
                },
                "secret_detected",
            ),
        ],
    )
    def test_refused_with_nothing_written(
        self, temp_registry: StoreRegistry, kwargs: dict[str, Any], reason: str
    ) -> None:
        _seed(temp_registry)
        before = _content_state(temp_registry)
        err = _refusal(**kwargs)
        expected = "blank_field" if reason == "blank" else reason
        assert err.error.data["reason"] == expected  # type: ignore[index]
        assert "nothing was written" in str(err)
        assert _proposals(temp_registry) == []
        assert _content_state(temp_registry) == before

    def test_the_secret_is_not_echoed(self, temp_registry: StoreRegistry) -> None:
        _seed(temp_registry)
        err = _refusal(
            item_id=TARGET,
            corrected_content=X_TEXT,
            reason="leaked api_key=Zq7rT2mX9vL4pW8n",
        )
        assert err.error.data["field"] == "reason"  # type: ignore[index]
        assert "Zq7rT2mX9vL4pW8n" not in str(err)

    def test_near_identical(self, temp_registry: StoreRegistry) -> None:
        temp_registry.knowledge.document_store.put(TARGET, LONG_OLD, {})
        err = _refusal(item_id=TARGET, corrected_content=LONG_NEW, reason=REASON)
        assert err.error.data["reason"] == "content_near_identical"  # type: ignore[index]

    @pytest.mark.parametrize(
        ("metadata", "reason"),
        [
            ({"chunk_count": 3}, "target_chunk"),
            (
                {LIFECYCLE_KEY: Lifecycle(state="archived").model_dump(mode="json")},
                "target_archived",
            ),
            (
                {
                    LIFECYCLE_KEY: Lifecycle(
                        state="superseded", superseded_by="doc-newer"
                    ).model_dump(mode="json")
                },
                "target_superseded",
            ),
        ],
    )
    def test_ineligible_targets(
        self, temp_registry: StoreRegistry, metadata: dict[str, Any], reason: str
    ) -> None:
        _seed(temp_registry, **metadata)
        err = _refusal(item_id=TARGET, corrected_content=X_TEXT, reason=REASON)
        assert err.error.data["reason"] == reason  # type: ignore[index]
        assert _proposals(temp_registry) == []

    def test_a_trace_summary_id_reads_as_missing(
        self, temp_registry: StoreRegistry
    ) -> None:
        err = _refusal(
            item_id="trace-summary:t1", corrected_content=X_TEXT, reason=REASON
        )
        assert err.error.data["reason"] == "target_missing"  # type: ignore[index]

    def test_every_reason_is_listed(self) -> None:
        assert set(REFUSAL_REASONS) == {
            "blank_field",
            "reason_too_long",
            "target_missing",
            "target_chunk",
            "target_archived",
            "target_superseded",
            "content_identical",
            "content_near_identical",
            "secret_detected",
        }

    def test_a_refusal_is_recorded_as_a_rejected_write(
        self, temp_registry: StoreRegistry
    ) -> None:
        _refusal(item_id="doc-nope", corrected_content=X_TEXT, reason=REASON)
        rejected = temp_registry.operational.event_log.get_events(
            event_type=EventType.WRITE_REJECTED, limit=10
        )
        assert [e.payload.get("tool") for e in rejected] == ["propose_correction"]


class TestClearance:
    def test_an_unreadable_target_answers_like_a_missing_one(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put(
            "doc-secret", T_TEXT, {"title": "S", **label("restricted")}
        )
        with as_http([SCOPE_READ, SCOPE_INGEST]), pytest.raises(MCPError) as hidden:
            propose_correction("doc-secret", X_TEXT, REASON)
        with as_http([SCOPE_READ, SCOPE_INGEST]), pytest.raises(MCPError) as absent:
            propose_correction("doc-absent", X_TEXT, REASON)
        assert hidden.value.error.data == absent.value.error.data
        assert str(hidden.value).replace("doc-secret", "ID") == str(
            absent.value
        ).replace("doc-absent", "ID")
        assert _proposals(temp_registry) == []

    def test_a_cleared_key_can_propose(self, temp_registry: StoreRegistry) -> None:
        temp_registry.knowledge.document_store.put(
            "doc-secret", T_TEXT, {"title": "S", **label("restricted")}
        )
        with as_http([SCOPE_READ, SCOPE_INGEST, "restricted"]):
            reply = propose_correction("doc-secret", X_TEXT, REASON)
        (event,) = _proposals(temp_registry)
        assert event.payload["sensitivity"] == "restricted"
        assert event.payload["proposed_by"] == "key-id"
        assert _pid(reply) == event.entity_id


class TestIdempotency:
    def test_the_same_text_for_the_same_target_is_one_proposal(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry)
        with as_stdio():
            first = propose_correction(TARGET, X_TEXT, REASON)
            second = propose_correction(TARGET, X_TEXT, "a different reason")
        assert _pid(first) == _pid(second)
        assert "already proposed" in second
        assert len(_proposals(temp_registry)) == 1
        assert len(list_proposals(temp_registry.operational.event_log)) == 1

    def test_different_text_is_a_second_proposal(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry)
        with as_stdio():
            first = propose_correction(TARGET, X_TEXT, REASON)
            second = propose_correction(TARGET, X_TEXT + " Confirmed twice.", REASON)
        assert _pid(first) != _pid(second)
        assert len(_proposals(temp_registry)) == 2


class TestScope:
    def test_the_tool_needs_the_ingest_scope(self) -> None:
        from tests.unit.mcp.test_auth import EXPECTED_TOOL_SCOPES

        assert EXPECTED_TOOL_SCOPES["propose_correction"] == SCOPE_INGEST

    async def test_an_external_key_is_refused(
        self, temp_registry: StoreRegistry
    ) -> None:
        ctx = _auth_context(await _external_scopes(temp_registry))
        assert trellis_scope(SCOPE_INGEST)(ctx) is False
        set_auth_enforced(enforced=False)
        try:
            tools = {t.name: t for t in await mcp.list_tools()}
        finally:
            set_auth_enforced(enforced=True)
        assert not tools["propose_correction"].auth(ctx)  # type: ignore[attr-defined]


class TestApplyAndReject:
    def test_apply_withholds_the_target_only_pairwise(
        self, temp_registry: StoreRegistry
    ) -> None:
        from trellis_cli.curate_corrections import apply_correction

        _seed(temp_registry)
        with as_stdio():
            pid = _pid(propose_correction(TARGET, X_TEXT, REASON))
        written = apply_correction(temp_registry, pid, reason="checked")
        successor = written["successor_id"]
        with as_stdio():
            get_context(INTENT)
            both = _pack_ids(temp_registry)
            get_context("quillwort")
            alone = _pack_ids(temp_registry)
        assert successor in both
        assert TARGET not in both
        assert TARGET in alone
        assert successor not in alone

    def test_reject_changes_nothing_served(self, temp_registry: StoreRegistry) -> None:
        from trellis_cli.curate_corrections import reject_correction

        _seed(temp_registry)
        with as_stdio():
            pid = _pid(propose_correction(TARGET, X_TEXT, REASON))
            get_context(INTENT)
            before_pack = _pack_ids(temp_registry)
        before = _content_state(temp_registry)
        reject_correction(temp_registry, pid, reason="wrong")
        with as_stdio():
            get_context(INTENT)
            after_pack = _pack_ids(temp_registry)
            again = propose_correction(TARGET, X_TEXT, REASON)
        assert before_pack == after_pack
        assert _content_state(temp_registry) == before
        assert "has been rejected" in again
