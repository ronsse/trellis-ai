"""A ``save_memory`` call that answers a duplicate is counted as an attempt.

A duplicate reply stores nothing, so it leaves no ``MUTATION_EXECUTED``. The
rejection rate divides by attempts, and a tool that mostly answers duplicates
read as mostly failing until the reply started leaving a ``WRITE_NOOP`` row.

These drive the real tool against temp stores and pin four things the emit must
not do: change the reply, change what is stored, count one call twice (a
duplicate that stamps ``supersedes`` already leaves an accepted event of its
own), or fail the call when the event log is down.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
import trellis.ops.write_health as write_health_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.mutate.policy_source import POLICY_FILENAME
from trellis.ops.write_health import summarize_write_health
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.registry import StoreRegistry

save_memory = unwrap_tool(server_mod.save_memory)

_OLD = (
    "calibration procedure for the sample rig: warm it for ten minutes, zero "
    "the sensor array against the reference cell, record three baseline "
    "sweeps, then set the settle window to forty milliseconds before the "
    "first capture run begins"
)
_NEAR = _OLD.replace("forty milliseconds", "twenty-five milliseconds")
_STORE_DOWN = "event store down"
_OTHER = "an unrelated note about shelf labels and where the spare cables are kept"


def _events(reg: StoreRegistry, event_type: EventType) -> list[Event]:
    return reg.operational.event_log.get_events(event_type=event_type, limit=1000)


def _noops(reg: StoreRegistry) -> list[Event]:
    return _events(reg, EventType.WRITE_NOOP)


def _executed_by_save_memory(reg: StoreRegistry) -> list[Event]:
    return [
        e
        for e in _events(reg, EventType.MUTATION_EXECUTED)
        if e.payload.get("requested_by") == "mcp:save_memory"
    ]


def _doc_id(reply: str) -> str:
    return reply.splitlines()[0].rsplit(": ", 1)[1].strip()


def _attempts(reg: StoreRegistry) -> int:
    report = summarize_write_health(reg.operational.event_log, days=1)
    return report.by_tool["mcp:save_memory"].attempts


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


class TestExactDuplicate:
    def test_it_leaves_one_noop_and_the_reply_it_always_had(
        self, temp_registry: StoreRegistry
    ) -> None:
        first = save_memory(_OLD)
        reply = save_memory(_OLD)

        assert reply == f"Memory already exists: {_doc_id(first)}"
        (noop,) = _noops(temp_registry)
        assert noop.source == "mcp:save_memory"
        assert noop.payload == {"tool": "save_memory", "reason": "exact_duplicate"}

    def test_the_row_names_no_content_and_no_id(
        self, temp_registry: StoreRegistry
    ) -> None:
        first = save_memory(_OLD)
        save_memory(_OLD)

        (noop,) = _noops(temp_registry)
        assert noop.entity_id is None
        assert _doc_id(first) not in json.dumps(noop.model_dump(mode="json"))
        assert "calibration" not in json.dumps(noop.model_dump(mode="json"))

    def test_it_stores_nothing_more(self, temp_registry: StoreRegistry) -> None:
        save_memory(_OLD)
        docs = temp_registry.knowledge.document_store.count()
        executed = len(_executed_by_save_memory(temp_registry))

        save_memory(_OLD)

        assert temp_registry.knowledge.document_store.count() == docs
        assert len(_executed_by_save_memory(temp_registry)) == executed

    def test_a_new_save_is_an_accepted_call_and_not_a_noop(
        self, temp_registry: StoreRegistry
    ) -> None:
        save_memory(_OLD)
        assert _noops(temp_registry) == []
        assert len(_executed_by_save_memory(temp_registry)) == 1

    def test_each_call_counts_once_in_the_health_report(
        self, temp_registry: StoreRegistry
    ) -> None:
        save_memory(_OLD)
        save_memory(_OLD)
        save_memory(_OLD)

        report = summarize_write_health(temp_registry.operational.event_log, days=1)
        stats = report.by_tool["mcp:save_memory"]
        assert (stats.accepted, stats.noop, stats.attempts) == (1, 2, 3)


class TestFuzzyDuplicate:
    def test_it_leaves_one_noop_and_the_reply_it_always_had(
        self, temp_registry: StoreRegistry
    ) -> None:
        first = save_memory(_OLD)
        reply = save_memory(_NEAR)

        assert re.fullmatch(
            rf"Fuzzy duplicate \(similarity \d+%\): {_doc_id(first)}", reply
        ), reply
        (noop,) = _noops(temp_registry)
        assert noop.payload == {"tool": "save_memory", "reason": "fuzzy_duplicate"}

    def test_it_stores_nothing_more(self, temp_registry: StoreRegistry) -> None:
        save_memory(_OLD)
        docs = temp_registry.knowledge.document_store.count()
        executed = len(_executed_by_save_memory(temp_registry))

        save_memory(_NEAR)

        assert temp_registry.knowledge.document_store.count() == docs
        assert len(_executed_by_save_memory(temp_registry)) == executed

    def test_a_refused_supersedes_is_an_error_and_not_a_noop(
        self, temp_registry: StoreRegistry
    ) -> None:
        """The near match is not the document named, so the call raises."""
        save_memory(_OLD)
        other = _doc_id(save_memory(_OTHER))

        with pytest.raises(McpError):
            save_memory(_NEAR, supersedes=other)

        assert _noops(temp_registry) == []


class TestADuplicateThatStampsSupersedes:
    """The stamp is a write of its own, and that write is the accepted call."""

    def test_a_stamp_that_runs_is_one_accepted_call_and_no_noop(
        self, temp_registry: StoreRegistry
    ) -> None:
        reg = temp_registry
        target = _doc_id(save_memory(_OLD))
        existing = _doc_id(save_memory(_OTHER))
        attempts = _attempts(reg)

        reply = save_memory(_OTHER, supersedes=target)

        assert reply == f"Memory already exists (supersedes {target}): {existing}"
        assert _noops(reg) == []
        assert _attempts(reg) == attempts + 1
        stamp = _executed_by_save_memory(reg)[-1]
        assert stamp.payload["operation"] == "evidence.ingest"

    def test_a_stamp_already_in_place_writes_nothing_and_is_one_noop(
        self, temp_registry: StoreRegistry
    ) -> None:
        reg = temp_registry
        target = _doc_id(save_memory(_OLD))
        successor = _doc_id(save_memory(_OTHER, supersedes=target))
        executed = len(_executed_by_save_memory(reg))
        attempts = _attempts(reg)

        reply = save_memory(_OTHER, supersedes=target)

        assert reply == f"Memory already exists (supersedes {target}): {successor}"
        assert len(_executed_by_save_memory(reg)) == executed
        (noop,) = _noops(reg)
        assert noop.payload["reason"] == "exact_duplicate"
        assert _attempts(reg) == attempts + 1

    def test_a_stamp_the_policy_refuses_is_an_error_and_not_a_noop(
        self, temp_registry: StoreRegistry
    ) -> None:
        reg = temp_registry
        target = _doc_id(save_memory(_OLD))
        save_memory(_OTHER)
        _deny(reg, "evidence.ingest")

        with pytest.raises(McpError):
            save_memory(_OTHER, supersedes=target)

        assert _noops(reg) == []


class TestAFailingEventLogDoesNotChangeTheCall:
    @staticmethod
    def _break_noop_emit(reg: StoreRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
        log = reg.operational.event_log
        real = log.emit

        def flaky(event_type: EventType, *args: Any, **kwargs: Any) -> Event:
            if event_type is EventType.WRITE_NOOP:
                raise RuntimeError(_STORE_DOWN)
            return real(event_type, *args, **kwargs)

        monkeypatch.setattr(log, "emit", flaky)

    @pytest.mark.parametrize("second", [_OLD, _NEAR], ids=["exact", "fuzzy"])
    def test_the_reply_and_the_store_are_the_same(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
        second: str,
    ) -> None:
        reg = temp_registry
        save_memory(_OLD)
        healthy = save_memory(second)
        docs = reg.knowledge.document_store.count()
        executed = len(_executed_by_save_memory(reg))
        assert len(_noops(reg)) == 1

        self._break_noop_emit(reg, monkeypatch)
        broken = save_memory(second)

        assert broken == healthy
        assert reg.knowledge.document_store.count() == docs
        assert len(_executed_by_save_memory(reg)) == executed
        # The emit raised, so the second row was never written.
        assert len(_noops(reg)) == 1

    def test_a_registry_that_cannot_be_built_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unavailable() -> StoreRegistry:
            raise RuntimeError(_STORE_DOWN)

        monkeypatch.setattr(server_mod, "_get_registry", unavailable)

        assert server_mod._note_noop_call("save_memory", "exact_duplicate") is None

    def test_a_helper_that_raises_leaves_the_reply_and_the_store_alone(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure the helper's own guard did not catch must not reach the caller."""
        reg = temp_registry
        save_memory(_OLD)
        healthy = save_memory(_OLD)
        docs = reg.knowledge.document_store.count()
        executed = len(_executed_by_save_memory(reg))

        def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError(_STORE_DOWN)

        monkeypatch.setattr(write_health_mod, "record_write_noop", broken)
        reply = save_memory(_OLD)

        assert reply == healthy
        assert reg.knowledge.document_store.count() == docs
        assert len(_executed_by_save_memory(reg)) == executed
