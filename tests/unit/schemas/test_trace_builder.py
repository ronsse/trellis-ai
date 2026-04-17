"""Tests for TracePayloadBuilder."""

from __future__ import annotations

import datetime

import pytest

from trellis.schemas.trace_builder import TracePayloadBuilder

# ---------------------------------------------------------------------------
# Minimal build
# ---------------------------------------------------------------------------


def test_minimal_build_returns_required_keys():
    payload = TracePayloadBuilder(source="workflow", intent="run etl").build()
    assert payload["source"] == "workflow"
    assert payload["intent"] == "run etl"
    assert payload["steps"] == []
    assert payload["artifacts_produced"] == []


def test_minimal_build_omits_optional_sections():
    payload = TracePayloadBuilder(source="agent", intent="test").build()
    assert "outcome" not in payload
    assert "context" not in payload
    assert "metadata" not in payload


# ---------------------------------------------------------------------------
# add_step
# ---------------------------------------------------------------------------


def test_add_step_appended_in_order():
    builder = TracePayloadBuilder(source="workflow", intent="test")
    builder.add_step(step_type="sql", name="create_view")
    builder.add_step(step_type="event", name="on_complete")
    payload = builder.build()
    assert len(payload["steps"]) == 2
    assert payload["steps"][0]["name"] == "create_view"
    assert payload["steps"][1]["name"] == "on_complete"


def test_add_step_defaults_args_and_result_to_empty_dicts():
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .add_step(step_type="sql", name="q")
        .build()
    )
    step = payload["steps"][0]
    assert step["args"] == {}
    assert step["result"] == {}


def test_add_step_preserves_args_and_result():
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .add_step(
            step_type="sql",
            name="q",
            args={"warehouse": "w1"},
            result={"rows": 42},
        )
        .build()
    )
    step = payload["steps"][0]
    assert step["args"] == {"warehouse": "w1"}
    assert step["result"] == {"rows": 42}


def test_add_step_started_at_string_passthrough():
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .add_step(step_type="event", name="e", started_at="2024-01-01T00:00:00Z")
        .build()
    )
    assert payload["steps"][0]["started_at"] == "2024-01-01T00:00:00Z"


def test_add_step_started_at_datetime_converted():
    dt = datetime.datetime(2024, 6, 1, 12, 0, 0, tzinfo=datetime.UTC)
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .add_step(step_type="event", name="e", started_at=dt)
        .build()
    )
    assert "2024-06-01" in payload["steps"][0]["started_at"]


def test_add_step_no_started_at_key_when_omitted():
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .add_step(step_type="event", name="e")
        .build()
    )
    assert "started_at" not in payload["steps"][0]


# ---------------------------------------------------------------------------
# add_artifact
# ---------------------------------------------------------------------------


def test_add_artifact_defaults_type_to_file():
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .add_artifact(artifact_id="/var/data/out.sql")
        .build()
    )
    art = payload["artifacts_produced"][0]
    assert art["artifact_id"] == "/var/data/out.sql"
    assert art["artifact_type"] == "file"


def test_add_artifact_custom_type():
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .add_artifact(artifact_id="s3://bucket/key", artifact_type="s3_object")
        .build()
    )
    assert payload["artifacts_produced"][0]["artifact_type"] == "s3_object"


def test_add_multiple_artifacts():
    builder = TracePayloadBuilder(source="workflow", intent="x")
    builder.add_artifact(artifact_id="a1")
    builder.add_artifact(artifact_id="a2")
    assert len(builder.build()["artifacts_produced"]) == 2


# ---------------------------------------------------------------------------
# set_outcome
# ---------------------------------------------------------------------------


def test_set_outcome_success():
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .set_outcome(status="success", summary="all good")
        .build()
    )
    assert payload["outcome"]["status"] == "success"
    assert payload["outcome"]["summary"] == "all good"


def test_set_outcome_with_metrics():
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .set_outcome(status="partial", metrics={"rows": 10})
        .build()
    )
    assert payload["outcome"]["metrics"] == {"rows": 10}


@pytest.mark.parametrize("status", ["success", "failure", "partial", "unknown"])
def test_set_outcome_all_valid_statuses(status):
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .set_outcome(status=status)
        .build()
    )
    assert payload["outcome"]["status"] == status


def test_set_outcome_invalid_status_raises():
    builder = TracePayloadBuilder(source="workflow", intent="x")
    with pytest.raises(ValueError, match="invalid status"):
        builder.set_outcome(status="DONE")


def test_set_outcome_default_metrics_empty():
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .set_outcome(status="unknown")
        .build()
    )
    assert payload["outcome"]["metrics"] == {}


# ---------------------------------------------------------------------------
# set_context
# ---------------------------------------------------------------------------


def test_set_context_appears_in_payload():
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .set_context(agent_id="my-agent", domain="data_eng", workflow_id="run-001")
        .build()
    )
    ctx = payload["context"]
    assert ctx["agent_id"] == "my-agent"
    assert ctx["domain"] == "data_eng"
    assert ctx["workflow_id"] == "run-001"


def test_set_context_datetime_converted():
    dt = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .set_context(agent_id="a", started_at=dt)
        .build()
    )
    assert "2024-01-01" in payload["context"]["started_at"]


def test_set_context_none_timestamps_remain_none():
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .set_context(agent_id="a")
        .build()
    )
    assert payload["context"]["started_at"] is None
    assert payload["context"]["ended_at"] is None


# ---------------------------------------------------------------------------
# set_metadata
# ---------------------------------------------------------------------------


def test_set_metadata_appears_in_payload():
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .set_metadata(run_id="abc", env="prod")
        .build()
    )
    assert payload["metadata"]["run_id"] == "abc"
    assert payload["metadata"]["env"] == "prod"


def test_set_metadata_merges_across_calls():
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .set_metadata(a=1)
        .set_metadata(b=2)
        .build()
    )
    assert payload["metadata"] == {"a": 1, "b": 2}


def test_set_metadata_later_call_overwrites_key():
    payload = (
        TracePayloadBuilder(source="workflow", intent="x")
        .set_metadata(key="first")
        .set_metadata(key="second")
        .build()
    )
    assert payload["metadata"]["key"] == "second"


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_blank_source_raises():
    with pytest.raises(ValueError, match="source must not be blank"):
        TracePayloadBuilder(source="", intent="x")


def test_whitespace_source_raises():
    with pytest.raises(ValueError, match="source must not be blank"):
        TracePayloadBuilder(source="   ", intent="x")


def test_blank_intent_raises():
    with pytest.raises(ValueError, match="intent must not be blank"):
        TracePayloadBuilder(source="workflow", intent="")


def test_whitespace_intent_raises():
    with pytest.raises(ValueError, match="intent must not be blank"):
        TracePayloadBuilder(source="workflow", intent="  ")


# ---------------------------------------------------------------------------
# Fluent chaining — all methods return self
# ---------------------------------------------------------------------------


def test_full_fluent_chain():
    payload = (
        TracePayloadBuilder(source="workflow", intent="full chain test")
        .add_step(
            step_type="sql", name="step1", args={"q": "select 1"}, result={"ok": True}
        )
        .add_artifact(artifact_id="/out/file.parquet", artifact_type="parquet")
        .set_outcome(status="success", metrics={"rows": 99}, summary="done")
        .set_context(agent_id="pipe-agent", domain="analytics", workflow_id="w-42")
        .set_metadata(run_id="run-xyz", version="2")
        .build()
    )
    assert payload["source"] == "workflow"
    assert payload["intent"] == "full chain test"
    assert len(payload["steps"]) == 1
    assert len(payload["artifacts_produced"]) == 1
    assert payload["outcome"]["status"] == "success"
    assert payload["context"]["agent_id"] == "pipe-agent"
    assert payload["metadata"]["run_id"] == "run-xyz"


def test_build_does_not_mutate_builder():
    builder = TracePayloadBuilder(source="workflow", intent="x")
    builder.add_step(step_type="a", name="first")
    first = builder.build()
    builder.add_step(step_type="b", name="second")
    second = builder.build()
    # first build's steps list should not have been extended
    assert len(first["steps"]) == 1
    assert len(second["steps"]) == 2
