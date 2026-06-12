"""Tests for SDK ``record_feedback`` parity (sync + async).

Covers three contracts:

* Happy path, both modes — the call returns ``event_log_in_sync`` and
  the authoritative ``FEEDBACK_RECORDED`` event lands in the operational
  EventLog while a row lands in ``pack_feedback.jsonl``.
* Soft-failure surfacing — when the EventLog emission fails, the result
  reports ``event_log_in_sync=False`` rather than swallowing it.
* Payload shape — the SDK posts the exact ``PackFeedbackRequest`` body
  to ``POST /api/v1/packs/{pack_id}/feedback``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI
from starlette.testclient import TestClient as StarletteTestClient

import trellis_api.app as api_app_module
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import curate, version
from trellis_sdk.async_client import AsyncTrellisClient
from trellis_sdk.client import TrellisClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    import pytest


def _build_app(registry: StoreRegistry) -> FastAPI:
    """Minimal app exposing the curate router against ``registry``.

    Mirrors :func:`trellis.testing.inmemory._build_app` but keeps the
    registry reachable so a test can read both feedback sinks back.
    """
    app = FastAPI()
    app.include_router(version.router)
    app.include_router(curate.router, prefix="/api/v1", tags=["curate"])
    api_app_module._registry = registry
    return app


@contextmanager
def _sync_client(stores_dir: Path) -> Iterator[tuple[TrellisClient, StoreRegistry]]:
    stores_dir.mkdir(parents=True, exist_ok=True)
    registry = StoreRegistry(stores_dir=stores_dir)
    app = _build_app(registry)
    http = StarletteTestClient(app, base_url="http://testserver")
    http.__enter__()
    client = TrellisClient(http=http, verify_version=False)
    try:
        yield client, registry
    finally:
        client.close()
        http.__exit__(None, None, None)
        registry.close()
        api_app_module._registry = None


@asynccontextmanager
async def _async_client(
    stores_dir: Path,
) -> AsyncIterator[tuple[AsyncTrellisClient, StoreRegistry]]:
    stores_dir.mkdir(parents=True, exist_ok=True)
    registry = StoreRegistry(stores_dir=stores_dir)
    app = _build_app(registry)
    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    client = AsyncTrellisClient(http=http, verify_version=False)
    try:
        yield client, registry
    finally:
        await client.close()
        await http.aclose()
        registry.close()
        api_app_module._registry = None


def _feedback_events(registry: StoreRegistry) -> list[Any]:
    return registry.operational.event_log.get_events(
        event_type=EventType.FEEDBACK_RECORDED, limit=10
    )


def _jsonl_rows(stores_dir: Path) -> list[dict[str, Any]]:
    log_path = stores_dir / "feedback" / "pack_feedback.jsonl"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestLocalModeHappyPath:
    """In-process ASGI mode — both feedback sinks observed directly."""

    def test_sync_records_event_and_jsonl(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        with _sync_client(stores_dir) as (client, registry):
            result = client.record_feedback(
                "pack:abc",
                success=True,
                helpful_item_ids=["doc:1", "doc:2"],
                unhelpful_item_ids=["doc:3"],
                followed_advisory_ids=["adv:1"],
            )

            assert result.pack_id == "pack:abc"
            assert result.feedback == "positive"
            assert result.event_log_in_sync is True
            assert result.event_log_emitted is True

            events = _feedback_events(registry)
            assert len(events) == 1
            payload = events[0].payload
            assert payload["pack_id"] == "pack:abc"
            assert payload["success"] is True
            # helpful items become the positive helpful_item_ids signal.
            assert payload["helpful_item_ids"] == ["doc:1", "doc:2"]
            # The stronger signals ride along in metadata.
            assert payload["metadata"]["unhelpful_item_ids"] == ["doc:3"]
            assert payload["metadata"]["followed_advisory_ids"] == ["adv:1"]

        rows = _jsonl_rows(stores_dir)
        assert len(rows) == 1
        assert rows[0]["feedback_id"] == result.feedback_id
        assert rows[0]["outcome"] == "success"
        assert rows[0]["items_referenced"] == ["doc:1", "doc:2"]

    def test_sync_failure_outcome(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        with _sync_client(stores_dir) as (client, registry):
            result = client.record_feedback("pack:neg", success=False)
            assert result.feedback == "negative"
            assert result.event_log_in_sync is True
            assert _feedback_events(registry)[0].payload["success"] is False

    async def test_async_records_event_and_jsonl(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        async with _async_client(stores_dir) as (client, registry):
            result = await client.record_feedback(
                "pack:async",
                success=True,
                helpful_item_ids=["doc:9"],
            )
            assert result.event_log_in_sync is True
            events = _feedback_events(registry)
            assert len(events) == 1
            assert events[0].payload["pack_id"] == "pack:async"

        rows = _jsonl_rows(stores_dir)
        assert len(rows) == 1
        assert rows[0]["items_referenced"] == ["doc:9"]


class TestSoftFailureSurfaced:
    """A failed EventLog emission must surface as ``event_log_in_sync``
    False, not be swallowed.  The durable JSONL row is still written."""

    def test_emit_failure_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = tmp_path / "stores"
        with _sync_client(stores_dir) as (client, registry):
            event_log = registry.operational.event_log

            def _boom(*_args: Any, **_kwargs: Any) -> None:
                msg = "event log offline"
                raise RuntimeError(msg)

            # Patch emit so the recording.record_feedback bridge catches
            # the failure and reports it on the result. The duplicate
            # pre-check uses get_events, which must still return [] so
            # the code reaches the emit attempt.
            monkeypatch.setattr(event_log, "emit", _boom)

            result = client.record_feedback("pack:soft", success=True)

            assert result.event_log_emitted is False
            assert result.event_log_skipped_as_duplicate is False
            assert result.event_log_in_sync is False

        # Durable audit row still landed despite the emit failure.
        rows = _jsonl_rows(stores_dir)
        assert len(rows) == 1


class TestPayloadShape:
    """The SDK posts the exact wire contract to the right path."""

    def test_sync_request_body_and_path(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "pack_id": "pack:xyz",
                    "feedback_id": "fb:1",
                    "feedback": "positive",
                    "event_log_in_sync": True,
                    "event_log_emitted": True,
                    "event_log_skipped_as_duplicate": False,
                },
            )

        transport = httpx.MockTransport(handler)
        http = httpx.Client(transport=transport, base_url="http://testserver")
        with TrellisClient(http=http, verify_version=False) as client:
            result = client.record_feedback(
                "pack:xyz",
                success=True,
                helpful_item_ids=["a"],
                unhelpful_item_ids=["b"],
                followed_advisory_ids=["adv"],
                target_id="trace:7",
                rating=0.8,
                comment="notes",
            )

        assert captured["method"] == "POST"
        assert captured["path"] == "/api/v1/packs/pack:xyz/feedback"
        # Body matches PackFeedbackRequest exactly — no extra fields.
        assert captured["body"] == {
            "success": True,
            "helpful_item_ids": ["a"],
            "unhelpful_item_ids": ["b"],
            "followed_advisory_ids": ["adv"],
            "target_id": "trace:7",
            "rating": 0.8,
            "comment": "notes",
        }
        assert result.feedback_id == "fb:1"
        assert result.event_log_in_sync is True
