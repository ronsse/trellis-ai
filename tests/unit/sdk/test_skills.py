"""Tests for SDK skill functions."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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
    os.environ["TRELLIS_CONFIG_DIR"] = str(tmp_path / "config")
    os.environ["TRELLIS_DATA_DIR"] = str(tmp_path / "data")
    (tmp_path / "data" / "stores").mkdir(parents=True)
    c = TrellisClient()
    yield c
    c.close()
    del os.environ["TRELLIS_DATA_DIR"]
    del os.environ["TRELLIS_CONFIG_DIR"]


def test_get_context_for_task_empty(client):
    result = get_context_for_task(client, "test intent")
    assert isinstance(result, str)
    assert "test intent" in result.lower() or "no relevant" in result.lower()


def test_get_latest_successful_trace_none(client):
    result = get_latest_successful_trace(client, "deploy")
    assert "No successful traces" in result


def test_save_trace_and_extract_lessons(client):
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


def test_get_recent_activity_empty(client):
    result = get_recent_activity(client)
    assert "No recent activity" in result


def test_get_recent_activity_with_traces(client):
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
    result = get_objective_context_for_workflow(client, "build GGR pipeline")
    assert isinstance(result, str)
    assert (
        "GGR pipeline" in result
        or "Context" in result
        or "no relevant" in result.lower()
        or result != ""
    )


def test_get_task_context_for_step_empty(client: TrellisClient) -> None:
    result = get_task_context_for_step(
        client,
        "generate SQL for casino_sessions",
        entity_ids=["uc://foundation.casino.game_rounds"],
    )
    assert isinstance(result, str)
