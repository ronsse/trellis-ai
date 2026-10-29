"""Tests for SDK skill functions."""

from __future__ import annotations

import json
import re
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


def _pack_client(payload: dict) -> TrellisClient:
    """A client whose every request answers ``payload`` (the assemble-pack reply)."""
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload)),
        base_url="http://testserver",
    )
    return TrellisClient(http=http, verify_version=False)


def _footer(markdown: str) -> str:
    """The feedback footer: everything from the last ``---`` rule on."""
    return markdown[markdown.rindex("\n---\n") :]


def test_get_context_for_task_empty_pack_carries_pack_id_and_footer() -> None:
    """An empty pack is gradable: it names its ``pack_id`` and how to cite it."""
    with _pack_client({"pack_id": "pack:empty", "count": 0, "items": []}) as client:
        result = get_context_for_task(client, "test intent")

    assert "No relevant context found for: test intent" in result
    assert "**pack_id:** `pack:empty`" in result
    footer = _footer(result)
    assert 'record_feedback(pack_id="pack:empty"' in footer
    assert "rating=" in footer
    assert "helpful_item_ids" in footer
    assert result.endswith(footer)


def test_get_context_for_task_empty_pack_footer_matches_the_served_pack_footer() -> (
    None
):
    """One footer text for both paths, so the two cannot drift apart."""
    item = {
        "item_type": "document",
        "item_id": "served-id",
        "excerpt": "Use a token bucket.",
        "relevance_score": 0.9,
    }
    with _pack_client({"pack_id": "pack:same", "count": 1, "items": [item]}) as client:
        served = get_context_for_task(client, "test intent")
    with _pack_client({"pack_id": "pack:same", "count": 0, "items": []}) as client:
        empty = get_context_for_task(client, "test intent")

    assert _footer(empty) == _footer(served)


def test_get_context_for_task_empty_pack_keeps_the_withholding_note_in_the_header() -> (
    None
):
    payload = {
        "pack_id": "pack:withheld",
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
    }
    with _pack_client(payload) as client:
        result = get_context_for_task(client, "test intent")

    assert "**Withheld:** 2 items" in result
    assert result.index("**pack_id:**") < result.index("**Withheld:**")
    assert result.index("**Withheld:**") < result.index("No relevant context found")
    assert 'record_feedback(pack_id="pack:withheld"' in result


def test_get_context_for_task_empty_pack_without_a_pack_id_has_no_footer() -> None:
    """Nothing to cite, so nothing claims the reply can be graded."""
    with _pack_client({"count": 0, "items": []}) as client:
        result = get_context_for_task(client, "test intent")

    assert result == "No relevant context found for: test intent"


def test_get_context_for_task_empty_real_pack_is_gradable(
    client: TrellisClient,
) -> None:
    """End to end: the id on an empty reply is one the server minted."""
    result = get_context_for_task(client, "test intent")

    match = re.search(r"\*\*pack_id:\*\* `([^`]+)`", result)
    assert match is not None, result
    assert f'record_feedback(pack_id="{match.group(1)}"' in result


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


def test_get_context_for_task_carries_pack_id_and_feedback_footer() -> None:
    """The markdown names the pack, so the caller can grade it with item ids."""
    seen: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "pack_id": "pack:served",
                "count": 1,
                "items": [
                    {
                        "item_type": "document",
                        "item_id": "served-id",
                        "excerpt": "Use a token bucket.",
                        "relevance_score": 0.9,
                    }
                ],
            },
        )

    http = httpx.Client(
        transport=httpx.MockTransport(_handler), base_url="http://testserver"
    )
    with TrellisClient(http=http, verify_version=False) as client:
        result = get_context_for_task(client, "test intent", max_tokens=1234)

    assert "`pack:served`" in result
    footer = result.rstrip().splitlines()[-1]
    assert footer.startswith("*Cite feedback via `record_feedback(pack_id=")
    assert "rating=..." in footer
    assert "helpful_item_ids=[...]" in footer
    assert "`served-id`" in result
    assert seen["body"]["max_tokens"] == 1234  # type: ignore[index]


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
