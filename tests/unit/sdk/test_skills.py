"""Tests for SDK skill functions."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from trellis.testing import in_memory_client
from trellis_sdk.client import TrellisClient
from trellis_sdk.skills import (
    get_context_for_task,
    get_latest_successful_trace,
    get_objective_context_for_workflow,
    get_recent_activity,
    get_task_context_for_step,
    save_trace_and_extract_lessons,
)


@pytest.fixture
def client(tmp_path: Path):
    with in_memory_client(tmp_path / "stores") as c:
        yield c


def test_get_context_for_task_empty(client: TrellisClient):
    result = get_context_for_task(client, "test intent")
    assert isinstance(result, str)
    assert "test intent" in result.lower() or "no relevant" in result.lower()


def test_get_context_for_task_empty_pack_includes_withholding_note() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "pack_id": "pack:empty",
                "count": 0,
                "items": [],
                "withholding": {
                    "total": 2,
                    "by_reason": {"archived": 1, "noise": 1},
                    "withheld_item_ids": ["archive-id", "noise-id"],
                    "non_absence_reasons": [],
                    "section_filtered": 0,
                    "served_count": 0,
                },
            },
        )

    http = httpx.Client(
        transport=httpx.MockTransport(_handler), base_url="http://testserver"
    )
    with TrellisClient(http=http, verify_version=False) as client:
        result = get_context_for_task(client, "test intent")

    assert "No relevant context found for: test intent" in result
    assert "**Withheld:** 2 items" in result
    assert "archive-id" not in result
    assert "noise-id" not in result


def test_get_context_for_task_places_withholding_before_served_items() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "pack_id": "pack:mixed",
                "count": 1,
                "items": [
                    {
                        "item_type": "document",
                        "item_id": "served-id",
                        "excerpt": "Use a token bucket.",
                        "relevance_score": 0.9,
                    }
                ],
                "withholding": {
                    "total": 1,
                    "by_reason": {"noise": 1},
                    "withheld_item_ids": ["noise-id"],
                    "non_absence_reasons": [],
                    "section_filtered": 0,
                    "served_count": 1,
                },
            },
        )

    http = httpx.Client(
        transport=httpx.MockTransport(_handler), base_url="http://testserver"
    )
    with TrellisClient(http=http, verify_version=False) as client:
        result = get_context_for_task(client, "test intent")

    assert result.index("**Withheld:**") < result.index("## [document]")
    assert "noise-id" not in result


def test_get_latest_successful_trace_none(client: TrellisClient):
    result = get_latest_successful_trace(client, "deploy")
    assert "No successful traces" in result


def test_save_trace_and_extract_lessons(client: TrellisClient):
    trace = {
        "source": "agent",
        "intent": "deploy service",
        "steps": [],
        "outcome": {"status": "success"},
        "context": {"agent_id": "test", "domain": "test"},
    }
    result = save_trace_and_extract_lessons(client, trace)
    assert "ingested" in result.lower()
    assert "deploy service" in result


def test_get_recent_activity_empty(client: TrellisClient):
    result = get_recent_activity(client)
    assert "No recent activity" in result or "No traces" in result


def test_get_recent_activity_with_traces(client: TrellisClient):
    trace = {
        "source": "agent",
        "intent": "test activity",
        "steps": [],
        "context": {"agent_id": "test", "domain": "test"},
    }
    client.ingest_trace(trace)
    result = get_recent_activity(client)
    assert isinstance(result, str)
    assert "test activity" in result


def test_get_objective_context_for_workflow_empty(client: TrellisClient) -> None:
    result = get_objective_context_for_workflow(client, "build revenue pipeline")
    assert isinstance(result, str)
    # Either the intent is echoed, or a "no results" shape is rendered.
    assert result != ""


def test_get_task_context_for_step_empty(client: TrellisClient) -> None:
    result = get_task_context_for_step(
        client,
        "generate SQL for domain_c_sessions",
        entity_ids=["uc://analytics.domain_c.events"],
    )
    assert isinstance(result, str)
