"""The write-health rate, driven end to end through the real write tools.

``summarize_write_health`` rates a tool's rejections against its attempts. What
counts as an attempt differs by tool, so these drive the tools themselves
(accepted calls, boundary rejections, policy-refused calls) rather than seeding
the event rows the report reads:

* ``save_experience`` fans out: one call is one ``trace.ingest`` command plus a
  command per extracted entity and edge. Its rate is rated on the
  ``trace.ingest`` operation alone, so a refused extraction command is not
  counted as a refused call.
* Every other recorded tool must leave an accepted event for each call that
  returns, or its rate overstates. The sweep at the bottom pins which event.
"""

from __future__ import annotations

import json

import pytest
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.mutate.policy_source import POLICY_FILENAME
from trellis.ops.write_health import WriteHealthReport, summarize_write_health
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

save_experience = unwrap_tool(server_mod.save_experience)
save_knowledge = unwrap_tool(server_mod.save_knowledge)
record_feedback = unwrap_tool(server_mod.record_feedback)

_SURFACE = "mcp:save_experience"
_BAD_TRACE = (
    '{"source": "agent", "intent": "x",'
    ' "outcome": {"status": "success", "artifacts": ["a"]},'
    ' "context": {"domain": "d"}}'
)


def _trace(n: int) -> str:
    return json.dumps(
        {
            "source": "agent",
            "intent": f"works {n}",
            "outcome": {"status": "success", "summary": "ok"},
            "context": {"domain": "d"},
        }
    )


def _deny(reg: StoreRegistry, operation: str) -> None:
    assert reg.stores_dir is not None
    policy = Policy(
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level="global", value=None),
        rules=[PolicyRule(operation=operation, action="deny")],
        enforcement=Enforcement.ENFORCE,
    )
    (reg.stores_dir / POLICY_FILENAME).write_text(
        json.dumps({"policies": [policy.model_dump(mode="json")]}),
        encoding="utf-8",
    )


def _report(reg: StoreRegistry) -> WriteHealthReport:
    return summarize_write_health(reg.operational.event_log, days=1)


def _artifact_collision(report: WriteHealthReport) -> dict[str, object] | None:
    for row in report.repeated_collisions:
        if row["tool"] == _SURFACE and row["loc"] == "outcome.artifacts":
            return row
    return None


def _executed(reg: StoreRegistry, tool: str) -> int:
    events = reg.operational.event_log.get_events(
        event_type=EventType.MUTATION_EXECUTED, limit=1000
    )
    return sum(1 for e in events if e.payload.get("requested_by") == f"mcp:{tool}")


def _seed_unrelated_rejections(reg: StoreRegistry, count: int) -> None:
    """Rejected extraction commands, booked to ``save_experience`` by its tag."""
    for _ in range(count):
        reg.operational.event_log.emit(
            EventType.MUTATION_REJECTED,
            "mutation_executor",
            payload={
                "operation": "entity.create",
                "requested_by": _SURFACE,
                "reason": "policy_denied",
            },
        )


class TestSaveExperienceIsRatedOnItsTraceIngestCalls:
    def _drive(self, reg: StoreRegistry, *, accepted: int, boundary: int) -> None:
        for n in range(accepted):
            assert "Trace saved" in save_experience(trace_json=_trace(n))
        for _ in range(boundary):
            with pytest.raises(McpError):
                save_experience(trace_json=_BAD_TRACE)

    def test_a_call_the_policy_refuses_is_one_rejected_call(
        self, temp_registry: StoreRegistry
    ) -> None:
        reg = temp_registry
        self._drive(reg, accepted=12, boundary=3)
        _deny(reg, "trace.ingest")
        for n in range(2):
            with pytest.raises(McpError):
                save_experience(trace_json=_trace(100 + n))

        stats = _report(reg).by_tool[_SURFACE]
        assert stats.executor_rejected_by_operation == {"trace.ingest": 2}
        # 12 stored + 3 refused at the boundary + 2 refused by the policy.
        row = _artifact_collision(_report(reg))
        assert row is not None
        assert row["tool_attempts"] == 17
        assert row["rate"] == round(3 / 17, 4)

    def test_refused_extraction_commands_do_not_dilute_the_rate(
        self, temp_registry: StoreRegistry
    ) -> None:
        """60 rejected ``entity.create`` commands are not 60 rejected calls.

        Counted as calls they would put the three artifacts rejections under the
        5% share and hide a tool that is failing one call in six.
        """
        reg = temp_registry
        self._drive(reg, accepted=12, boundary=3)
        _deny(reg, "trace.ingest")
        for n in range(2):
            with pytest.raises(McpError):
                save_experience(trace_json=_trace(100 + n))
        _seed_unrelated_rejections(reg, 60)

        report = _report(reg)
        assert report.by_tool[_SURFACE].executor_rejected == 62
        row = _artifact_collision(report)
        assert row is not None
        assert row["tool_attempts"] == 17

    def test_a_policy_refusal_is_an_attempt_and_not_ignored(
        self, temp_registry: StoreRegistry
    ) -> None:
        """Left out, 15 attempts would stand in for 17 and the rate would read high."""
        reg = temp_registry
        self._drive(reg, accepted=12, boundary=3)
        before = _artifact_collision(_report(reg))
        assert before is not None
        assert before["tool_attempts"] == 15

        _deny(reg, "trace.ingest")
        with pytest.raises(McpError):
            save_experience(trace_json=_trace(100))

        after = _artifact_collision(_report(reg))
        assert after is not None
        assert after["tool_attempts"] == 16


class TestEveryRecordedToolLeavesAnAcceptedEventPerCall:
    """Where each tool's accept count comes from, checked against the tool.

    A tool whose success reply leaves no event reads as failing more than it is.
    """

    def test_save_experience_counts_a_resubmitted_trace(
        self, temp_registry: StoreRegistry
    ) -> None:
        trace = json.loads(_trace(1))
        trace["trace_id"] = "01JAAAAAAAAAAAAAAAAAAAAAAA"
        save_experience(trace_json=json.dumps(trace))
        first = _executed(temp_registry, "save_experience")

        reply = save_experience(trace_json=json.dumps(trace))

        assert "already ingested" in reply
        assert _executed(temp_registry, "save_experience") == first + 1

    def test_save_knowledge_counts_each_call(
        self, temp_registry: StoreRegistry
    ) -> None:
        save_knowledge(name="widget", content="widget facts about the sample rig")
        first = _executed(temp_registry, "save_knowledge")
        assert first >= 1

        save_knowledge(name="widget", content="widget facts about the sample rig")

        assert _executed(temp_registry, "save_knowledge") > first

    def test_record_feedback_counts_each_call_as_its_own_event(
        self, temp_registry: StoreRegistry
    ) -> None:
        record_feedback(pack_id="pack-1", rating=0.5)
        record_feedback(pack_id="pack-1", rating=0.5)

        stats = _report(temp_registry).by_tool["mcp:record_feedback"]
        assert stats.accepted == 2
        # The tool itself writes no executor command, so none is counted.
        assert stats.accepted_by_operation == {}
