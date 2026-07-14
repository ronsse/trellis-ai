"""Tests for reconcile-on-write — the model-judged verdict tier (#263).

The local model is ALWAYS mocked (at the ``_build_llm_client`` boundary); no
test makes a network call. Fixtures are fully synthetic.

Coverage map (Definition of Done):
* every verdict type end-to-end against the temp stores (ADD/UPDATE/
  SUPERSEDE/NOOP);
* lock discipline — the model call runs with ``_save_memory_lock`` released;
* fallbacks — model down / timeout / malformed / transport error → ADD-with-
  marker, no judged event;
* re-verify-under-lock race — candidate changed while the model thought →
  downgrade to ADD-with-marker (stale), no supersede, no judged event;
* emission payload correctness (leak-safe digests, right refs) + no emission
  for deterministic short-circuits / fallbacks;
* deterministic tier unchanged — flag on but no near match → clean ADD, no
  model call, no judged event; exact re-save short-circuits.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.core.hashing import content_hash
from trellis.llm.types import LLMResponse
from trellis.mcp.reconcile import ReconcileDecision, parse_verdict
from trellis.schemas.memory_op import JudgedOpType, MemoryOpJudgedPayload
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

save_memory = unwrap_tool(server_mod.save_memory)

# Synthetic near-duplicate pair: MinHash Jaccard ~0.91 (comfortably above the
# 0.85 near-dup threshold), so the deterministic tier surfaces BASE as the
# candidate to adjudicate when NEAR is written.
_BASE = "The staging gateway listens on port 9450 for inbound sync jobs."
_NEAR = "The staging gateway listens on port 9451 for inbound sync jobs."
#: A second, DISTINCT near-dup of _BASE (Jaccard ~0.91) for racing two
#: different contents against the same candidate.
_NEAR_B = "The staging gateway listens on port 9460 for inbound sync jobs."
_UNRELATED = "Kubernetes pods restart when the liveness probe fails three times."


class FakeLLMClient:
    """Mock ``LLMClient`` — canned verdict, or a controllable failure/latency.

    ``on_call`` fires inside ``generate`` (still outside ``_save_memory_lock``),
    letting a test observe lock state or mutate the store mid-verdict.
    """

    def __init__(
        self,
        *,
        content: str = "",
        model: str = "hermes3:8b",
        raises: Exception | None = None,
        delay: float = 0.0,
        on_call: Any = None,
    ) -> None:
        self._content = content
        self._model = model
        self._raises = raises
        self._delay = delay
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
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return LLMResponse(content=self._content, model=self._model)


def _verdict_json(decision: str, confidence: float = 0.9) -> str:
    return json.dumps({"decision": decision, "confidence": confidence})


def _enable(monkeypatch: pytest.MonkeyPatch, client: Any | None) -> None:
    """Turn the verdict tier on and pin the client boundary to *client*."""
    monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
    monkeypatch.setattr(server_mod, "_build_llm_client", lambda _registry: client)


def _judged_events(registry: StoreRegistry) -> list[Any]:
    return registry.operational.event_log.get_events(
        event_type=EventType.MEMORY_OP_JUDGED, limit=100
    )


def _stored_events(registry: StoreRegistry) -> list[Any]:
    return registry.operational.event_log.get_events(
        event_type=EventType.MEMORY_STORED, limit=100
    )


def _doc_id(result: str) -> str:
    return result.rsplit(":", 1)[-1].strip()


# ---------------------------------------------------------------------------
# parse_verdict — strictness (malformed → None → caller falls back)
# ---------------------------------------------------------------------------


class TestParseVerdict:
    def test_valid(self) -> None:
        assert parse_verdict('{"decision": "add", "confidence": 0.7}') == (
            ReconcileDecision.ADD,
            0.7,
        )

    def test_fenced_code_block(self) -> None:
        raw = '```json\n{"decision": "supersede", "confidence": 0.5}\n```'
        assert parse_verdict(raw) == (ReconcileDecision.SUPERSEDE, 0.5)

    def test_uppercase_decision_normalised(self) -> None:
        assert parse_verdict('{"decision": "NOOP", "confidence": 1}') == (
            ReconcileDecision.NOOP,
            1.0,
        )

    def test_confidence_clamped(self) -> None:
        assert parse_verdict('{"decision": "add", "confidence": 5}') == (
            ReconcileDecision.ADD,
            1.0,
        )

    def test_missing_confidence_defaults(self) -> None:
        assert parse_verdict('{"decision": "update"}') == (
            ReconcileDecision.UPDATE,
            0.5,
        )

    @pytest.mark.parametrize(
        "raw",
        [
            "not json at all",
            "",
            "[]",  # not a dict
            '{"decision": "frobnicate", "confidence": 0.5}',  # unknown verdict
            '{"confidence": 0.5}',  # missing decision
            '{"decision": "add", "confidence": "high"}',  # non-numeric
            '{"decision": "add", "confidence": true}',  # bool is malformed
        ],
    )
    def test_malformed_returns_none(self, raw: str) -> None:
        assert parse_verdict(raw) is None


# ---------------------------------------------------------------------------
# Verdict matrix — every decision, end-to-end against the temp stores
# ---------------------------------------------------------------------------


class TestVerdictMatrix:
    def test_add_stores_second_doc_and_emits(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_id = _doc_id(save_memory(_BASE))
        _enable(monkeypatch, FakeLLMClient(content=_verdict_json("add")))

        result = save_memory(_NEAR)
        new_id = _doc_id(result)

        assert result.startswith("Memory saved:")
        assert new_id != base_id
        assert temp_registry.knowledge.document_store.count() == 2
        doc = temp_registry.knowledge.document_store.get(new_id)
        assert doc["metadata"]["reconciliation"] == "add"

        judged = _judged_events(temp_registry)
        assert len(judged) == 1
        assert judged[0].payload["decision"] == "add"
        # ADD verdict is *about* the new memory.
        assert judged[0].payload["subject_ref"]["ref_id"] == new_id

    def test_noop_stores_nothing_but_emits(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_id = _doc_id(save_memory(_BASE))
        _enable(monkeypatch, FakeLLMClient(content=_verdict_json("noop")))

        result = save_memory(_NEAR)

        assert result == f"Memory already covered by: {base_id}"
        assert temp_registry.knowledge.document_store.count() == 1
        # No second doc → no MEMORY_STORED beyond the seed.
        assert len(_stored_events(temp_registry)) == 1
        judged = _judged_events(temp_registry)
        assert len(judged) == 1
        assert judged[0].payload["decision"] == "noop"
        assert judged[0].payload["subject_ref"]["ref_id"] == base_id

    def test_update_annotates_non_destructively(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_id = _doc_id(save_memory(_BASE))
        _enable(monkeypatch, FakeLLMClient(content=_verdict_json("update")))

        result = save_memory(_NEAR)
        new_id = _doc_id(result)

        assert result == f"Memory saved (update of {base_id}): {new_id}"
        docs = temp_registry.knowledge.document_store
        # Candidate content is untouched — UPDATE is annotate, not rewrite.
        assert docs.get(base_id)["content"] == _BASE
        new_meta = docs.get(new_id)["metadata"]
        assert new_meta["reconciliation"] == "update"
        assert new_meta["updates_doc_id"] == base_id
        assert _judged_events(temp_registry)[0].payload["decision"] == "update"

    def test_supersede_rides_scd2_no_delete(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_id = _doc_id(save_memory(_BASE))
        _enable(monkeypatch, FakeLLMClient(content=_verdict_json("supersede")))

        result = save_memory(_NEAR)
        new_id = _doc_id(result)

        assert result == f"Memory saved (supersedes {base_id}): {new_id}"
        docs = temp_registry.knowledge.document_store
        # Old doc is NOT deleted — it is SCD-2 stale-marked.
        old = docs.get(base_id)
        assert old is not None
        assert old["content"] == _BASE
        lifecycle = old["metadata"]["lifecycle"]
        assert lifecycle["state"] == "superseded"
        assert lifecycle["superseded_by"] == new_id
        new_meta = docs.get(new_id)["metadata"]
        assert new_meta["reconciliation"] == "supersede"
        assert new_meta["supersedes_doc_id"] == base_id
        judged = _judged_events(temp_registry)[0]
        assert judged.payload["decision"] == "supersede"
        # SUPERSEDE verdict is *about* the superseded candidate.
        assert judged.payload["subject_ref"]["ref_id"] == base_id


# ---------------------------------------------------------------------------
# Lock discipline — the model call must not hold _save_memory_lock
# ---------------------------------------------------------------------------


class TestLockDiscipline:
    def test_lock_not_held_during_model_call(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lock must be free while the (slow) verdict runs.

        The verdict runs on the calling thread. ``threading.Lock`` is
        non-reentrant, so if the caller still held ``_save_memory_lock`` a
        same-thread ``acquire(blocking=False)`` would return ``False``. It
        returning ``True`` proves the lock was released before the model call.
        """
        observed: dict[str, bool] = {}

        def probe() -> None:
            observed["locked"] = server_mod._save_memory_lock.locked()
            acquired = server_mod._save_memory_lock.acquire(blocking=False)
            observed["acquirable"] = acquired
            if acquired:
                server_mod._save_memory_lock.release()

        save_memory(_BASE)
        _enable(
            monkeypatch,
            FakeLLMClient(content=_verdict_json("add"), on_call=probe),
        )
        save_memory(_NEAR)

        assert observed["acquirable"] is True
        assert observed["locked"] is False

    def test_concurrent_near_dup_saves_no_deadlock_single_winner(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two concurrent identical saves → one winner, no deadlock/double-store.

        Both threads judge the same base candidate outside the lock; the
        re-verify-under-lock exact-hash re-check lets only one commit.
        """
        import sys
        import threading
        from concurrent.futures import ThreadPoolExecutor

        save_memory(_BASE)
        _enable(monkeypatch, FakeLLMClient(content=_verdict_json("add")))

        n = 8
        barrier = threading.Barrier(n)
        original = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)

        def worker() -> str:
            barrier.wait()
            return save_memory(_NEAR)

        try:
            with ThreadPoolExecutor(max_workers=n) as pool:
                results = list(pool.map(lambda _: worker(), range(n)))
        finally:
            sys.setswitchinterval(original)

        saved = [r for r in results if r.startswith("Memory saved:")]
        already = [r for r in results if r.startswith("Memory already exists:")]
        assert len(saved) == 1, results
        assert len(already) == n - 1
        # base + exactly one near-dup persisted.
        assert temp_registry.knowledge.document_store.count() == 2


# ---------------------------------------------------------------------------
# Fallbacks — the judge is never a hard dependency
# ---------------------------------------------------------------------------


class TestFallbacks:
    def _assert_skipped_add(
        self, registry: StoreRegistry, base_id: str, result: str
    ) -> None:
        new_id = _doc_id(result)
        assert result.startswith("Memory saved:")
        assert new_id != base_id
        assert registry.knowledge.document_store.count() == 2
        meta = registry.knowledge.document_store.get(new_id)["metadata"]
        assert meta["reconciliation"] == "skipped"
        # A fallback judged nothing — no training pair emitted.
        assert _judged_events(registry) == []

    def test_model_unavailable_adds_with_marker(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_id = _doc_id(save_memory(_BASE))
        _enable(monkeypatch, None)  # _build_llm_client → None
        self._assert_skipped_add(temp_registry, base_id, save_memory(_NEAR))

    def test_client_build_raises_adds_with_marker(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_id = _doc_id(save_memory(_BASE))
        monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")

        def _boom(_registry: Any) -> Any:
            msg = "provider misconfigured"
            raise RuntimeError(msg)

        monkeypatch.setattr(server_mod, "_build_llm_client", _boom)
        self._assert_skipped_add(temp_registry, base_id, save_memory(_NEAR))

    def test_timeout_adds_with_marker(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_id = _doc_id(save_memory(_BASE))
        monkeypatch.setenv("TRELLIS_RECONCILE_TIMEOUT_S", "0.05")
        _enable(
            monkeypatch,
            FakeLLMClient(content=_verdict_json("supersede"), delay=0.5),
        )
        self._assert_skipped_add(temp_registry, base_id, save_memory(_NEAR))

    def test_malformed_response_adds_with_marker(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_id = _doc_id(save_memory(_BASE))
        _enable(monkeypatch, FakeLLMClient(content="I think you should keep both!"))
        self._assert_skipped_add(temp_registry, base_id, save_memory(_NEAR))

    def test_transport_error_adds_with_marker(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_id = _doc_id(save_memory(_BASE))
        _enable(
            monkeypatch,
            FakeLLMClient(raises=ConnectionError("connection refused")),
        )
        self._assert_skipped_add(temp_registry, base_id, save_memory(_NEAR))


# ---------------------------------------------------------------------------
# Re-verify-under-lock — the world changed while the model thought
# ---------------------------------------------------------------------------


class TestReverifyUnderLock:
    def test_candidate_changed_downgrades_to_add(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_id = _doc_id(save_memory(_BASE))
        docs = temp_registry.knowledge.document_store

        def mutate_candidate() -> None:
            # Another writer edits the candidate mid-verdict (phase B), so the
            # SUPERSEDE the model is about to return is now stale.
            docs.put(base_id, "The candidate content changed underneath us.")

        _enable(
            monkeypatch,
            FakeLLMClient(
                content=_verdict_json("supersede"), on_call=mutate_candidate
            ),
        )

        result = save_memory(_NEAR)
        new_id = _doc_id(result)

        # Downgraded to a plain ADD marked for a later sweep — data preserved.
        assert result.startswith("Memory saved:")
        assert docs.get(new_id)["metadata"]["reconciliation"] == "stale_recheck"
        # The (now-changed) candidate was NOT superseded.
        assert "lifecycle" not in (docs.get(base_id)["metadata"] or {})
        # No judged event — the verdict was never applied.
        assert _judged_events(temp_registry) == []

    def test_candidate_superseded_midflight_downgrades_to_add(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lifecycle axis of re-verify: content preserved, but stale-marked.

        ``mark_document_superseded`` keeps the candidate's content byte-
        identical (SCD-2), so a content-hash-only re-check would pass and a
        second SUPERSEDE would fork the supersession chain. A non-``current``
        lifecycle state must fail re-verify on its own.
        """
        from trellis.mcp.reconcile import mark_document_superseded

        base_id = _doc_id(save_memory(_BASE))
        docs = temp_registry.knowledge.document_store

        def supersede_candidate() -> None:
            # A concurrent writer wins its own SUPERSEDE against the
            # candidate while our model is thinking — content unchanged.
            mark_document_superseded(
                docs, old_doc_id=base_id, new_doc_id="someone-elses-successor"
            )

        _enable(
            monkeypatch,
            FakeLLMClient(
                content=_verdict_json("supersede"), on_call=supersede_candidate
            ),
        )

        result = save_memory(_NEAR)
        new_id = _doc_id(result)

        assert result.startswith("Memory saved:")
        assert docs.get(new_id)["metadata"]["reconciliation"] == "stale_recheck"
        # The candidate's back-pointer still names the FIRST successor only.
        lifecycle = docs.get(base_id)["metadata"]["lifecycle"]
        assert lifecycle["superseded_by"] == "someone-elses-successor"
        assert _judged_events(temp_registry) == []

    def test_double_supersede_race_single_successor(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two DISTINCT near-dups racing SUPERSEDE against the same candidate.

        Both writers judge SUPERSEDE outside the lock (a barrier inside the
        mocked model call guarantees both are mid-verdict simultaneously,
        so neither has committed when the other gathered). Exactly one may
        supersede; the loser must land as a ``stale_recheck``-marked ADD and
        the candidate's ``superseded_by`` must name exactly one successor.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor

        base_id = _doc_id(save_memory(_BASE))
        docs = temp_registry.knowledge.document_store

        barrier = threading.Barrier(2, timeout=10)
        _enable(
            monkeypatch,
            FakeLLMClient(content=_verdict_json("supersede"), on_call=barrier.wait),
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(save_memory, c) for c in (_NEAR, _NEAR_B)]
            results = [f.result(timeout=30) for f in futures]

        winners = [r for r in results if f"(supersedes {base_id})" in r]
        losers = [r for r in results if r.startswith("Memory saved:") and "(" not in r]
        assert len(winners) == 1, results
        assert len(losers) == 1, results
        winner_id = _doc_id(winners[0])
        loser_id = _doc_id(losers[0])

        # The candidate names exactly one successor — the winner.
        lifecycle = docs.get(base_id)["metadata"]["lifecycle"]
        assert lifecycle["state"] == "superseded"
        assert lifecycle["superseded_by"] == winner_id

        # Winner carries the supersede markers; loser is a marked ADD.
        assert docs.get(winner_id)["metadata"]["supersedes_doc_id"] == base_id
        assert docs.get(loser_id)["metadata"]["reconciliation"] == "stale_recheck"
        assert "supersedes_doc_id" not in docs.get(loser_id)["metadata"]

        # Exactly one doc in the store claims to supersede the candidate.
        all_docs = docs.list_documents(limit=50)
        claimants = [
            d
            for d in all_docs
            if (d.get("metadata") or {}).get("supersedes_doc_id") == base_id
        ]
        assert len(claimants) == 1
        assert claimants[0]["doc_id"] == winner_id

        # No data loss: candidate + both racers persisted.
        assert docs.count() == 3
        # Exactly one training pair — the applied verdict.
        judged = _judged_events(temp_registry)
        assert len(judged) == 1
        assert judged[0].payload["decision"] == "supersede"
        assert judged[0].payload["subject_ref"]["ref_id"] == base_id


# ---------------------------------------------------------------------------
# Phase-B belt-and-suspenders guard — capture never fails on a judge failure
# ---------------------------------------------------------------------------


class TestPhaseBGuard:
    def test_unexpected_judge_crash_falls_back_to_marked_add(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception escaping the whole verdict computation (beyond the
        exhaustive handling inside ``judge_reconcile``) still resolves to the
        offline fallback: ADD marked ``skipped``, no judged event."""
        base_id = _doc_id(save_memory(_BASE))
        _enable(monkeypatch, FakeLLMClient(content=_verdict_json("noop")))

        def _crash(*_a: Any, **_k: Any) -> Any:
            msg = "unexpected judge-path crash"
            raise RuntimeError(msg)

        monkeypatch.setattr(server_mod, "_compute_reconcile_outcome", _crash)

        result = save_memory(_NEAR)
        new_id = _doc_id(result)

        assert result.startswith("Memory saved:")
        assert new_id != base_id
        docs = temp_registry.knowledge.document_store
        assert docs.get(new_id)["metadata"]["reconciliation"] == "skipped"
        assert _judged_events(temp_registry) == []


# ---------------------------------------------------------------------------
# Emission payload correctness + leak-safety
# ---------------------------------------------------------------------------


class TestEmissionPayload:
    def test_payload_is_typed_and_leak_safe(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_id = _doc_id(save_memory(_BASE))
        _enable(
            monkeypatch,
            FakeLLMClient(content=_verdict_json("update", 0.83), model="hermes3:8b"),
        )
        new_id = _doc_id(save_memory(_NEAR))

        event = _judged_events(temp_registry)[0]
        # Re-validating proves the emitted dict matches the typed contract.
        payload = MemoryOpJudgedPayload(**event.payload)
        assert payload.op_type == JudgedOpType.RECONCILIATION
        assert payload.model_id == "hermes3:8b"
        assert payload.decision == "update"
        assert payload.confidence == pytest.approx(0.83)
        assert payload.input_digest.hash == content_hash(_NEAR)
        assert payload.input_digest.length == len(_NEAR)
        assert payload.input_digest.source_refs == [base_id]
        assert payload.subject_ref.ref_type == "doc"
        assert payload.subject_ref.ref_id == base_id
        assert event.entity_id in {base_id, new_id}

        # Leak-safety: neither memory's prose appears anywhere in the payload.
        blob = json.dumps(event.payload)
        assert _NEAR not in blob
        assert _BASE not in blob


# ---------------------------------------------------------------------------
# Deterministic tier unchanged under the flag
# ---------------------------------------------------------------------------


class TestDeterministicTierUnderFlag:
    def test_clean_add_skips_the_model(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeLLMClient(content=_verdict_json("noop"))
        _enable(monkeypatch, client)

        # No prior doc → no near candidate → plain ADD, model never consulted.
        result = save_memory(_UNRELATED)
        assert result.startswith("Memory saved:")
        assert client.calls == 0
        assert _judged_events(temp_registry) == []
        # Clean ADD carries no reconciliation marker.
        new_id = _doc_id(result)
        assert "reconciliation" not in (
            temp_registry.knowledge.document_store.get(new_id)["metadata"] or {}
        )

    def test_exact_resave_short_circuits_without_model(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = save_memory(_BASE)
        client = FakeLLMClient(content=_verdict_json("supersede"))
        _enable(monkeypatch, client)

        second = save_memory(_BASE)  # byte-identical
        assert second == f"Memory already exists: {_doc_id(first)}"
        assert client.calls == 0
        assert _judged_events(temp_registry) == []
        assert temp_registry.knowledge.document_store.count() == 1
