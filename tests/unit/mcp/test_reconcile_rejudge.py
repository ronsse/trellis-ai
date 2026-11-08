"""``rejudge_withheld``: the revisit of memories stored as ``egress_withheld`` (#1134).

Every state under test is produced by the real ``save_memory``: a non-local
reconcile route and a memory labelled above ``internal`` leave the row marked
``skipped`` with a ``RECONCILE_DEGRADED`` event, which is what the sweep reads.
The judge is a mock ``LLMClient`` at the ``_build_llm_client`` boundary, so a
call that reaches it is counted.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import trellis.mcp.auth as auth_mod
import trellis.mcp.reconcile_rejudge as rejudge_mod
import trellis.mcp.server as server_mod
import trellis.mcp.tools.memory as memory_tools_mod
import trellis.mcp.wiring as wiring_mod
from tests.llm_routes import TierRoutedRegistry, per_consumer_config
from tests.unit.mcp.conftest import unwrap_tool
from trellis.llm.routing import LLMConsumer
from trellis.llm.types import LLMResponse
from trellis.mcp.reconcile import ReconcileFallbackReason
from trellis.mcp.reconcile_rejudge import RejudgeReport, rejudge_withheld
from trellis.schemas.classification import lifecycle_state_of
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

save_memory = unwrap_tool(server_mod.save_memory)

# A synthetic near-duplicate pair (Jaccard ~0.91, above the 0.85 threshold).
_BASE = "The staging gateway listens on port 9450 for inbound sync jobs."
_NEAR = "The staging gateway listens on port 9451 for inbound sync jobs."
# A second, unrelated pair, for the run cap.
_BASE_2 = "The production database listens on port 5432 for inbound replica jobs."
_NEAR_2 = "The production database listens on port 5433 for inbound replica jobs."
_PRECEDENT_ID = "precedent://learning/gateway-port"


class FakeLLMClient:
    """Mock ``LLMClient``: a canned reply or a failure, with a call counter."""

    def __init__(
        self,
        *,
        content: str = "",
        raises: Exception | None = None,
        on_call: Any = None,
    ) -> None:
        self._content = content
        self._raises = raises
        self._on_call = on_call
        self.calls = 0

    async def generate(
        self,
        *,
        messages: Any,
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        self.calls += 1
        if self._on_call is not None:
            self._on_call()
        await asyncio.sleep(0)
        if self._raises is not None:
            raise self._raises
        return LLMResponse(content=self._content, model="m")


def _verdict(decision: str) -> FakeLLMClient:
    return FakeLLMClient(content=json.dumps({"decision": decision, "confidence": 0.9}))


class _Builder:
    """Stands in for ``_build_llm_client`` and records that it was asked."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.built = 0

    def __call__(self, _registry: Any, _consumer: Any) -> Any:
        self.built += 1
        return self.client


@pytest.fixture(autouse=True)
def _trusted_local_caller() -> None:
    """Auth off, so the caller's clearance is the trusted-local ``restricted``."""
    auth_mod.set_auth_enforced(enforced=False)


@pytest.fixture
def non_local(temp_registry: StoreRegistry) -> TierRoutedRegistry:
    """The registry as the write saw it: the reconcile route is not local."""
    return TierRoutedRegistry(
        temp_registry, per_consumer_config(LLMConsumer.RECONCILE, local=False)
    )


@pytest.fixture
def local(temp_registry: StoreRegistry) -> TierRoutedRegistry:
    """The same stores once the reconcile route has been declared local."""
    return TierRoutedRegistry(
        temp_registry, per_consumer_config(LLMConsumer.RECONCILE, local=True)
    )


@pytest.fixture
def judge(monkeypatch: pytest.MonkeyPatch) -> _Builder:
    """Pin the sweep's client boundary; the write path gets a client that fails."""
    builder = _Builder(None)
    monkeypatch.setattr(rejudge_mod, "_build_llm_client", builder)
    return builder


def _doc_id(result: str) -> str:
    """The id on the first line (a labelled save adds a note line after it)."""
    return result.splitlines()[0].rsplit(":", 1)[-1].strip()


def _withhold(
    monkeypatch: pytest.MonkeyPatch,
    non_local: TierRoutedRegistry,
    base: str = _BASE,
    near: str = _NEAR,
    *,
    base_label: str | None = "confidential",
    near_label: str | None = "confidential",
) -> tuple[str, str]:
    """Store *base*, then *near*, on a non-local route: the #1129 state.

    A labelled save next to a lower-labelled near-duplicate is refused outright,
    so the pair is labelled alike (or only the older side is).
    """
    monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
    write_client = _verdict("supersede")
    monkeypatch.setattr(
        memory_tools_mod, "_build_llm_client", lambda _r, _c: write_client
    )
    wiring_mod._registry = non_local  # type: ignore[assignment]
    base_id = _doc_id(save_memory(base, sensitivity=base_label))
    new_id = _doc_id(save_memory(near, sensitivity=near_label))
    assert write_client.calls == 0
    docs = non_local.knowledge.document_store
    assert docs.get(new_id)["metadata"]["reconciliation"] == "skipped"
    return base_id, new_id


def _events(registry: Any, event_type: EventType) -> list[Any]:
    return registry.operational.event_log.get_events(event_type=event_type, limit=100)


def _snapshot(registry: Any) -> dict[str, Any]:
    docs = registry.knowledge.document_store
    return {d["doc_id"]: d for d in docs.list_documents(limit=1000)}


class TestSupersedeIsAppliedOnce:
    def test_a_withheld_memory_is_judged_once_the_route_is_local(
        self,
        monkeypatch: pytest.MonkeyPatch,
        non_local: TierRoutedRegistry,
        local: TierRoutedRegistry,
        judge: _Builder,
    ) -> None:
        base_id, new_id = _withhold(monkeypatch, non_local)
        (degraded,) = _events(non_local, EventType.RECONCILE_DEGRADED)
        assert degraded.payload["reason"] == ReconcileFallbackReason.EGRESS_WITHHELD
        assert degraded.payload["subject_ref_id"] == new_id
        client = _verdict("supersede")
        judge.client = client

        report = rejudge_withheld(local, apply=True)

        docs = local.knowledge.document_store
        assert (report.found, report.judged, report.applied) == (1, 1, 1)
        assert report.judged_by_verdict["supersede"] == 1
        assert client.calls == 1
        # The older row is stale-marked, never deleted.
        old = docs.get(base_id)
        assert old["content"] == _BASE
        assert old["metadata"]["lifecycle"]["state"] == "superseded"
        assert old["metadata"]["lifecycle"]["superseded_by"] == new_id
        # The withheld marker is replaced by the judged one, as at write time.
        new_meta = docs.get(new_id)["metadata"]
        assert new_meta["reconciliation"] == "supersede"
        assert new_meta["supersedes_doc_id"] == base_id
        # The leak-safe event, about the superseded row.
        (judged,) = _events(local, EventType.MEMORY_OP_JUDGED)
        assert judged.payload["decision"] == "supersede"
        assert judged.payload["subject_ref"]["ref_id"] == base_id
        assert _BASE not in json.dumps(judged.payload)

        again = rejudge_withheld(local, apply=True)

        assert (again.found, again.no_longer_pending, again.judged) == (1, 1, 0)
        assert client.calls == 1
        assert len(_events(local, EventType.MEMORY_OP_JUDGED)) == 1


class TestEgressIsAskedAgain:
    def test_a_pair_the_gate_still_refuses_is_counted_and_not_sent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        non_local: TierRoutedRegistry,
        judge: _Builder,
    ) -> None:
        _base_id, new_id = _withhold(monkeypatch, non_local)
        judge.client = client = _verdict("supersede")
        before = _snapshot(non_local)

        report = rejudge_withheld(non_local, apply=True)

        assert (report.found, report.still_withheld, report.judged) == (1, 1, 0)
        assert report.rows[0].doc_id == new_id
        assert report.rows[0].outcome == "still_withheld"
        # Nothing was sent, and no client was even built.
        assert client.calls == 0
        assert judge.built == 0
        assert _snapshot(non_local) == before
        assert _events(non_local, EventType.MEMORY_OP_JUDGED) == []

    def test_a_label_on_the_older_side_alone_also_withholds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        non_local: TierRoutedRegistry,
        judge: _Builder,
    ) -> None:
        """Both texts are shown to the judge, so either label withholds the pair."""
        _base_id, new_id = _withhold(monkeypatch, non_local, near_label=None)
        judge.client = client = _verdict("supersede")

        report = rejudge_withheld(non_local, apply=True)

        assert (report.found, report.still_withheld) == (1, 1)
        assert report.rows[0].doc_id == new_id
        assert client.calls == 0
        assert judge.built == 0


class TestReportOnlyWritesNothing:
    def test_the_default_calls_the_judge_and_writes_no_row_or_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        non_local: TierRoutedRegistry,
        local: TierRoutedRegistry,
        judge: _Builder,
    ) -> None:
        base_id, new_id = _withhold(monkeypatch, non_local)
        judge.client = client = _verdict("supersede")
        before = _snapshot(local)
        events_before = len(local.operational.event_log.get_events(limit=1000))

        report = rejudge_withheld(local)

        assert report.apply is False
        assert (report.judged, report.applied) == (1, 0)
        assert report.judged_by_verdict["supersede"] == 1
        assert report.rows[0].outcome == "would_supersede"
        assert client.calls == 1
        assert _snapshot(local) == before
        assert len(local.operational.event_log.get_events(limit=1000)) == events_before
        assert (
            lifecycle_state_of(local.knowledge.document_store.get(base_id)["metadata"])
            == "current"
        )
        # And the verdict it counted is still there to apply.
        applied = rejudge_withheld(local, apply=True)
        assert applied.applied == 1
        assert new_id == applied.rows[0].doc_id


class TestJudgeFailureIsFailOpen:
    @pytest.mark.parametrize(
        "client",
        [
            pytest.param(FakeLLMClient(raises=ConnectionError("down")), id="raises"),
            pytest.param(FakeLLMClient(content="keep both!"), id="malformed"),
            pytest.param(None, id="no-client"),
        ],
    )
    def test_the_row_stays_pending_and_a_later_run_retries(
        self,
        monkeypatch: pytest.MonkeyPatch,
        non_local: TierRoutedRegistry,
        local: TierRoutedRegistry,
        judge: _Builder,
        client: FakeLLMClient | None,
    ) -> None:
        base_id, new_id = _withhold(monkeypatch, non_local)
        judge.client = client
        before = _snapshot(local)

        report = rejudge_withheld(local, apply=True)

        assert (report.judge_failures, report.judged, report.applied) == (1, 0, 0)
        assert report.apply_failures == 0
        assert _snapshot(local) == before
        assert (
            local.knowledge.document_store.get(new_id)["metadata"]["reconciliation"]
            == "skipped"
        )

        judge.client = _verdict("supersede")
        retry = rejudge_withheld(local, apply=True)

        assert (retry.judge_failures, retry.applied) == (0, 1)
        assert (
            lifecycle_state_of(local.knowledge.document_store.get(base_id)["metadata"])
            == "superseded"
        )


class TestVerdictsNeverDelete:
    @pytest.mark.parametrize("decision", ["update", "add", "noop"])
    def test_the_other_verdicts_record_the_marker_and_leave_both_rows_current(
        self,
        monkeypatch: pytest.MonkeyPatch,
        non_local: TierRoutedRegistry,
        local: TierRoutedRegistry,
        judge: _Builder,
        decision: str,
    ) -> None:
        base_id, new_id = _withhold(monkeypatch, non_local)
        judge.client = _verdict(decision)

        report = rejudge_withheld(local, apply=True)

        docs = local.knowledge.document_store
        assert (report.applied, report.judged_by_verdict[decision]) == (1, 1)
        meta = docs.get(new_id)["metadata"]
        assert meta["reconciliation"] == decision
        assert meta.get("updates_doc_id") == (base_id if decision == "update" else None)
        assert "supersedes_doc_id" not in meta
        # NOOP is a recorded verdict, not a stale-mark: both rows stay current.
        for doc_id in (base_id, new_id):
            assert lifecycle_state_of(docs.get(doc_id)["metadata"]) == "current"
        (judged,) = _events(local, EventType.MEMORY_OP_JUDGED)
        assert judged.payload["decision"] == decision
        expected = new_id if decision == "add" else base_id
        assert judged.payload["subject_ref"]["ref_id"] == expected
        assert rejudge_withheld(local, apply=True).judged == 0


class TestProtectedTarget:
    def test_a_promoted_precedent_row_is_counted_and_not_stamped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        non_local: TierRoutedRegistry,
        local: TierRoutedRegistry,
        judge: _Builder,
    ) -> None:
        from trellis.core.document_write import put_document

        monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
        wiring_mod._registry = non_local  # type: ignore[assignment]
        docs = non_local.knowledge.document_store
        put_document(
            docs,
            None,
            _PRECEDENT_ID,
            _BASE,
            {"document_form": "precedent", "data_classification": "confidential"},
            preserve_updated_at=False,
        )
        server_mod._get_minhash_index(non_local).add(_PRECEDENT_ID, _BASE)
        new_id = _doc_id(save_memory(_NEAR, sensitivity="confidential"))
        precedent_before = docs.get(_PRECEDENT_ID)
        judge.client = _verdict("supersede")

        report = rejudge_withheld(local, apply=True)

        assert (report.found, report.target_protected, report.applied) == (1, 1, 0)
        assert docs.get(_PRECEDENT_ID) == precedent_before
        meta = docs.get(new_id)["metadata"]
        assert meta["reconciliation"] == "target_protected"
        assert "supersedes_doc_id" not in meta
        assert _events(local, EventType.MEMORY_OP_JUDGED) == []
        degraded = [
            e
            for e in _events(local, EventType.RECONCILE_DEGRADED)
            if e.payload["reason"] == ReconcileFallbackReason.TARGET_PROTECTED
        ]
        assert [e.payload["judge_failure"] for e in degraded] == [False]
        # A protected row is a settled answer: it is not judged again.
        assert rejudge_withheld(local, apply=True).judged == 0


class TestMovedWhileJudging:
    def test_an_edited_candidate_leaves_the_row_pending(
        self,
        monkeypatch: pytest.MonkeyPatch,
        non_local: TierRoutedRegistry,
        local: TierRoutedRegistry,
        judge: _Builder,
    ) -> None:
        base_id, new_id = _withhold(monkeypatch, non_local)
        docs = local.knowledge.document_store
        judge.client = FakeLLMClient(
            content=json.dumps({"decision": "supersede", "confidence": 0.9}),
            on_call=lambda: docs.put(base_id, "The candidate changed underneath us."),
        )

        report = rejudge_withheld(local, apply=True)

        assert (report.candidate_changed, report.applied) == (1, 0)
        assert docs.get(new_id)["metadata"]["reconciliation"] == "skipped"
        assert "lifecycle" not in (docs.get(base_id)["metadata"] or {})


class TestRunCap:
    def test_pending_rows_beyond_max_items_are_deferred_not_examined(
        self,
        monkeypatch: pytest.MonkeyPatch,
        non_local: TierRoutedRegistry,
        local: TierRoutedRegistry,
        judge: _Builder,
    ) -> None:
        _withhold(monkeypatch, non_local)
        save_memory(_BASE_2, sensitivity="confidential")
        save_memory(_NEAR_2, sensitivity="confidential")
        judge.client = client = _verdict("noop")

        report: RejudgeReport = rejudge_withheld(local, apply=True, max_items=1)

        assert (report.found, report.deferred, report.judged) == (2, 1, 1)
        assert client.calls == 1
        rest = rejudge_withheld(local, apply=True, max_items=1)
        assert (rest.found, rest.no_longer_pending, rest.judged) == (2, 1, 1)
