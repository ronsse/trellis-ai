"""Tests for the SDK workflow integration hooks.

Covers, for each of :class:`ContextInjector`, :class:`TraceRecorder`, and
:class:`ResultFeedback`:

* **Happy path** — against the in-process ASGI client
  (:func:`trellis.testing.in_memory_client`) with a real store, so the
  trace / entity / feedback writes are observable end-to-end.
* **Every degradation path** — server unreachable, 4xx validation error,
  version mismatch at handshake, and a mid-call transport drop — each
  asserting *no exception escapes* and the documented sentinel is returned.
* **The ``raise_errors=True`` escape hatch** — the same failures propagate
  the underlying :class:`~trellis_sdk.exceptions.TrellisError`.

Degradation paths use ``httpx.MockTransport`` so we can script the exact
failure mode (connect error, 422, mid-call drop) without a real network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest

from trellis.testing import in_memory_client
from trellis_sdk.client import TrellisClient
from trellis_sdk.exceptions import (
    TrellisClientError,
    TrellisError,
    TrellisTransportError,
    TrellisVersionMismatchError,
)
from trellis_sdk.hooks import ContextInjector, ResultFeedback, TraceRecorder

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def live_client(tmp_path: Path) -> Iterator[TrellisClient]:
    """In-process ASGI client backed by a real tmp_path StoreRegistry."""
    with in_memory_client(tmp_path / "stores") as client:
        yield client


def _unreachable_client() -> TrellisClient:
    """Client whose every request raises a connect error (server down).

    ``verify_version=False`` so the failure surfaces on the hook's real
    call, not the handshake — the hooks must catch it either way, but this
    keeps the transport-drop assertion precise.
    """

    def _refuse(_request: httpx.Request) -> httpx.Response:
        msg = "connection refused"
        raise httpx.ConnectError(msg)

    transport = httpx.MockTransport(_refuse)
    http = httpx.Client(transport=transport, base_url="http://down.invalid")
    return TrellisClient(http=http, verify_version=False)


def _client_returning(status: int, body: dict[str, Any]) -> TrellisClient:
    """Client where every request returns ``status`` with ``body``."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    transport = httpx.MockTransport(_handler)
    http = httpx.Client(transport=transport, base_url="http://testserver")
    return TrellisClient(http=http, verify_version=False)


def _version_mismatch_client() -> TrellisClient:
    """Client that fails the lazy version handshake on first call.

    The server reports an incompatible ``api_major``; ``check_handshake``
    raises :class:`TrellisVersionMismatchError` (a ``TrellisError``
    subclass) out of the first request, which the hook must catch.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(
                200,
                json={
                    "api_major": 99,
                    "api_minor": 0,
                    "api_version": "99.0",
                    "wire_schema": "0.1.0",
                    "sdk_min": "0.1.0",
                    "package_version": "99.0.0",
                },
            )
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(_handler)
    http = httpx.Client(transport=transport, base_url="http://testserver")
    return TrellisClient(http=http, verify_version=True)


def _midcall_drop_client(drop_on_path_contains: str) -> TrellisClient:
    """Client that succeeds until a request whose path contains the marker.

    Models a mid-call transport drop: the first writes land, then the
    connection dies partway through the hook's sequence of calls (e.g. the
    edge write after the entity write).
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        if drop_on_path_contains in request.url.path:
            msg = "connection reset mid-call"
            raise httpx.ReadError(msg)
        # Generic success shape covering create_entity / create_link /
        # ingest_trace / record_feedback responses.
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "node_id": "node:1",
                "edge_id": "edge:1",
                "trace_id": "trace:1",
                "pack_id": "pack:1",
                "feedback_id": "fb:1",
                "feedback": "positive",
                "event_log_in_sync": True,
                "event_log_emitted": True,
                "event_log_skipped_as_duplicate": False,
            },
        )

    transport = httpx.MockTransport(_handler)
    http = httpx.Client(transport=transport, base_url="http://testserver")
    return TrellisClient(http=http, verify_version=False)


# ---------------------------------------------------------------------------
# ContextInjector
# ---------------------------------------------------------------------------


class TestContextInjectorHappyPath:
    def test_for_intent_empty_store_returns_empty(
        self, live_client: TrellisClient
    ) -> None:
        injector = ContextInjector(live_client)
        # No items in the store yet — empty context, not an error.
        assert injector.for_intent("rate limiting", domain="backend") == ""

    def test_for_intent_returns_markdown_when_pack_has_items(self) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "pack_id": "pack:ctx",
                    "intent": "rate limiting",
                    "count": 1,
                    "items": [
                        {
                            "item_type": "document",
                            "item_id": "doc:1",
                            "excerpt": "Use a token bucket.",
                            "relevance_score": 0.9,
                        }
                    ],
                },
            )

        transport = httpx.MockTransport(_handler)
        http = httpx.Client(transport=transport, base_url="http://testserver")
        with TrellisClient(http=http, verify_version=False) as client:
            md = ContextInjector(client).for_intent("rate limiting")
        assert "token bucket" in md
        assert "pack:ctx" in md

    def test_for_entities_falls_back_to_per_entity_lookup(self) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/packs":
                # Empty pack forces the per-entity fallback.
                return httpx.Response(
                    200, json={"pack_id": "p", "count": 0, "items": []}
                )
            # get_entity
            return httpx.Response(
                200,
                json={
                    "entity": {
                        "properties": {
                            "name": "orders-api",
                            "description": "Handles order placement.",
                        }
                    }
                },
            )

        transport = httpx.MockTransport(_handler)
        http = httpx.Client(transport=transport, base_url="http://testserver")
        with TrellisClient(http=http, verify_version=False) as client:
            md = ContextInjector(client).for_entities(["e1"], intent="ship it")
        assert "orders-api" in md
        assert "order placement" in md


class TestContextInjectorDegradation:
    def test_unreachable_returns_empty(self) -> None:
        injector = ContextInjector(_unreachable_client())
        assert injector.for_intent("anything") == ""

    def test_client_error_returns_empty(self) -> None:
        client = _client_returning(422, {"detail": "bad intent"})
        injector = ContextInjector(client)
        assert injector.for_intent("anything") == ""

    def test_version_mismatch_returns_empty(self) -> None:
        injector = ContextInjector(_version_mismatch_client())
        assert injector.for_intent("anything") == ""

    def test_midcall_drop_in_entity_fallback_returns_empty(self) -> None:
        # Pack route 422s -> fallback; get_entity drops -> empty.
        def _handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/packs":
                return httpx.Response(
                    200, json={"pack_id": "p", "count": 0, "items": []}
                )
            msg = "drop"
            raise httpx.ReadError(msg)

        transport = httpx.MockTransport(_handler)
        http = httpx.Client(transport=transport, base_url="http://testserver")
        with TrellisClient(http=http, verify_version=False) as client:
            assert ContextInjector(client).for_entities(["e1"]) == ""

    def test_raise_errors_propagates_transport(self) -> None:
        injector = ContextInjector(_unreachable_client(), raise_errors=True)
        with pytest.raises(TrellisTransportError):
            injector.for_intent("anything")

    def test_raise_errors_propagates_version_mismatch(self) -> None:
        injector = ContextInjector(_version_mismatch_client(), raise_errors=True)
        with pytest.raises(TrellisVersionMismatchError):
            injector.for_intent("anything")


# ---------------------------------------------------------------------------
# TraceRecorder
# ---------------------------------------------------------------------------


class TestTraceRecorderHappyPath:
    def test_records_success_trace(self, live_client: TrellisClient) -> None:
        recorder = TraceRecorder(
            live_client, workflow_id="wf-1", agent_id="tester", domain="backend"
        )
        trace_id = recorder.record(
            "plan", "success", 1200, summary="planned", entity_ids=["e1"]
        )
        assert trace_id is not None
        stored = live_client.get_trace(trace_id)
        assert stored is not None
        assert stored["context"]["workflow_id"] == "wf-1"
        assert stored["outcome"]["status"] == "success"

    def test_records_failure_trace(self, live_client: TrellisClient) -> None:
        recorder = TraceRecorder(live_client, workflow_id="wf-2")
        trace_id = recorder.record(
            "build", "failure", 50, error="compiler exploded"
        )
        assert trace_id is not None
        stored = live_client.get_trace(trace_id)
        assert stored is not None
        assert stored["outcome"]["status"] == "failure"

    def test_invalid_status_coerced_to_unknown(
        self, live_client: TrellisClient
    ) -> None:
        recorder = TraceRecorder(live_client, workflow_id="wf-3")
        trace_id = recorder.record("step", "not-a-status", 10)
        assert trace_id is not None
        stored = live_client.get_trace(trace_id)
        assert stored is not None
        assert stored["outcome"]["status"] == "unknown"


class TestTraceRecorderDegradation:
    def test_unreachable_returns_none(self) -> None:
        recorder = TraceRecorder(_unreachable_client(), workflow_id="wf")
        assert recorder.record("step", "success", 1) is None

    def test_client_error_returns_none(self) -> None:
        recorder = TraceRecorder(
            _client_returning(422, {"detail": "bad trace"}), workflow_id="wf"
        )
        assert recorder.record("step", "success", 1) is None

    def test_version_mismatch_returns_none(self) -> None:
        recorder = TraceRecorder(_version_mismatch_client(), workflow_id="wf")
        assert recorder.record("step", "success", 1) is None

    def test_midcall_drop_returns_none(self) -> None:
        recorder = TraceRecorder(
            _midcall_drop_client("traces"), workflow_id="wf"
        )
        assert recorder.record("step", "success", 1) is None

    def test_raise_errors_propagates(self) -> None:
        recorder = TraceRecorder(
            _unreachable_client(), workflow_id="wf", raise_errors=True
        )
        with pytest.raises(TrellisTransportError):
            recorder.record("step", "success", 1)

    def test_raise_errors_propagates_client_error(self) -> None:
        recorder = TraceRecorder(
            _client_returning(422, {"detail": "bad"}),
            workflow_id="wf",
            raise_errors=True,
        )
        with pytest.raises(TrellisClientError):
            recorder.record("step", "success", 1)


# ---------------------------------------------------------------------------
# ResultFeedback
# ---------------------------------------------------------------------------


class TestResultFeedbackHappyPath:
    def test_record_success_creates_evidence_and_feedback(
        self, live_client: TrellisClient
    ) -> None:
        target = live_client.create_entity("orders-api", entity_type="service")
        feedback = ResultFeedback(live_client)
        result = feedback.record_success(
            target,
            "rate-limit-config",
            "added a token bucket",
            full_content="limit = 100/min",
            pack_id="pack:abc",
            helpful_item_ids=["doc:1"],
        )
        assert result.ok is True
        assert result.ids is not None
        assert "document_id" in result.ids
        assert "edge_id" in result.ids
        assert "feedback_id" in result.ids
        # The DOCUMENT entity actually landed.
        doc = live_client.get_entity(result.ids["document_id"])
        assert doc is not None
        assert doc["properties"]["name"] == "rate-limit-config"

    def test_record_success_without_pack_skips_feedback(
        self, live_client: TrellisClient
    ) -> None:
        target = live_client.create_entity("svc", entity_type="service")
        result = ResultFeedback(live_client).record_success(
            target, "doc", "summary"
        )
        assert result.ok is True
        assert result.ids is not None
        assert "feedback_id" not in result.ids

    def test_record_failure_grades_pack_negative(
        self, live_client: TrellisClient
    ) -> None:
        target = live_client.create_entity("svc", entity_type="service")
        result = ResultFeedback(live_client).record_failure(
            target, "it broke", pack_id="pack:neg", unhelpful_item_ids=["doc:9"]
        )
        assert result.ok is True
        assert result.ids is not None
        assert "feedback_id" in result.ids

    def test_record_failure_without_pack_is_noop_ok(
        self, live_client: TrellisClient
    ) -> None:
        result = ResultFeedback(live_client).record_failure("svc", "broke")
        assert result.ok is True
        assert result.ids is None

    def test_record_success_uses_record_feedback_not_handrolled(self) -> None:
        """Pack grading hits POST /packs/{id}/feedback (the WP2 method)."""
        seen_paths: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            seen_paths.append(path)
            if path.endswith("/feedback"):
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "pack_id": "pack:abc",
                        "feedback_id": "fb:1",
                        "feedback": "positive",
                        "event_log_in_sync": True,
                        "event_log_emitted": True,
                        "event_log_skipped_as_duplicate": False,
                    },
                )
            return httpx.Response(
                200,
                json={"status": "ok", "node_id": "doc:1", "edge_id": "edge:1"},
            )

        transport = httpx.MockTransport(_handler)
        http = httpx.Client(transport=transport, base_url="http://testserver")
        with TrellisClient(http=http, verify_version=False) as client:
            ResultFeedback(client).record_success(
                "target", "name", "summary", pack_id="pack:abc"
            )
        assert "/api/v1/packs/pack:abc/feedback" in seen_paths


class TestResultFeedbackDegradation:
    def test_unreachable_returns_not_ok(self) -> None:
        result = ResultFeedback(_unreachable_client()).record_success(
            "t", "n", "s"
        )
        assert result.ok is False

    def test_client_error_returns_not_ok(self) -> None:
        client = _client_returning(422, {"detail": "bad entity"})
        result = ResultFeedback(client).record_success("t", "n", "s")
        assert result.ok is False

    def test_version_mismatch_returns_not_ok(self) -> None:
        result = ResultFeedback(_version_mismatch_client()).record_success(
            "t", "n", "s"
        )
        assert result.ok is False

    def test_midcall_drop_on_edge_returns_not_ok(self) -> None:
        # Entity write succeeds, the DESCRIBED_BY edge write drops.
        result = ResultFeedback(
            _midcall_drop_client("links")
        ).record_success("t", "n", "s")
        assert result.ok is False
        # The entity id that did land is still surfaced for debugging.
        assert result.ids is not None
        assert "document_id" in result.ids

    def test_failure_grade_drop_returns_not_ok(self) -> None:
        result = ResultFeedback(
            _midcall_drop_client("feedback")
        ).record_failure("t", "broke", pack_id="pack:1")
        assert result.ok is False

    def test_raise_errors_propagates_on_evidence(self) -> None:
        feedback = ResultFeedback(_unreachable_client(), raise_errors=True)
        with pytest.raises(TrellisError):
            feedback.record_success("t", "n", "s")

    def test_raise_errors_propagates_on_failure_grade(self) -> None:
        feedback = ResultFeedback(
            _midcall_drop_client("feedback"), raise_errors=True
        )
        with pytest.raises(TrellisError):
            feedback.record_failure("t", "broke", pack_id="pack:1")
