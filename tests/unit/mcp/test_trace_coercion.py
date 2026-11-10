"""``save_experience`` repairs the two commonest trace-shape mistakes, visibly.

A step ``result`` sent as a bare string and a ``context.repo`` key each
rejected the whole trace. Both now repair at the boundary, the stored trace is
canonical, the reply says what was repaired and how to send it next time, and
one ``WRITE_COERCED`` event records it without any content. Anything that is
not one of those two mistakes rejects exactly as before, with the same paths.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.ops.write_health import summarize_write_health
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.registry import StoreRegistry

save_experience = unwrap_tool(server_mod.save_experience)

_NEEDLE = "needle-do-not-log"
_STORE_DOWN = "event store down"
_REPO = "secret-org/secret-repo"


def _trace(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": "agent",
        "intent": "fix the parser",
        "context": {"agent_id": "a", "domain": "bugfix"},
        "steps": [{"step_type": "tool_call", "name": "run_tests", "result": {}}],
    }
    base.update(overrides)
    return base


def _string_result() -> dict[str, Any]:
    return _trace(
        steps=[{"step_type": "tool_call", "name": "t", "result": f"ok {_NEEDLE}"}]
    )


def _context_repo() -> dict[str, Any]:
    return _trace(context={"agent_id": "a", "domain": "bugfix", "repo": _REPO})


def _events(reg: StoreRegistry, event_type: EventType) -> list[Event]:
    return reg.operational.event_log.get_events(event_type=event_type, limit=1000)


def _trace_id(reply: str) -> str:
    return reply.splitlines()[0].split("Trace saved: ", 1)[1].split(" ")[0].strip()


class TestBothMistakesAreAccepted:
    def test_a_string_result_is_stored_as_a_summary_object(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = save_experience(trace_json=json.dumps(_string_result()))

        assert reply.startswith("Trace saved: ")
        stored = temp_registry.operational.trace_store.get(_trace_id(reply))
        assert stored is not None
        assert stored.steps[0].result == {"summary": f"ok {_NEEDLE}"}

    def test_context_repo_is_stored_in_top_level_metadata(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = save_experience(trace_json=json.dumps(_context_repo()))

        stored = temp_registry.operational.trace_store.get(_trace_id(reply))
        assert stored is not None
        assert stored.metadata["repo"] == _REPO
        assert "repo" not in stored.context.model_dump()

    def test_the_reply_says_what_was_repaired_and_how_to_send_it_next_time(
        self, temp_registry: StoreRegistry
    ) -> None:
        both = _context_repo()
        both["steps"] = _string_result()["steps"]

        reply = save_experience(trace_json=json.dumps(both))

        assert "repaired 2 fields" in reply
        assert "steps[].result" in reply
        assert "context.repo" in reply
        assert '{"summary"' in reply
        assert "top-level metadata" in reply
        # One line, and no repaired content in it.
        assert len(reply.splitlines()) == 1
        assert _NEEDLE not in reply
        assert _REPO not in reply

    def test_a_clean_trace_gets_the_unchanged_reply_and_no_event(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = save_experience(trace_json=json.dumps(_trace()))

        assert "repaired" not in reply
        assert _events(temp_registry, EventType.WRITE_COERCED) == []

    def test_a_repaired_duplicate_still_names_the_repair(
        self, temp_registry: StoreRegistry
    ) -> None:
        data = _string_result()
        data["trace_id"] = "01TESTTRACEIDDUPLICATE0000"
        text = json.dumps(data)
        save_experience(trace_json=text)

        second = save_experience(trace_json=text)

        assert second.startswith("Trace already ingested")
        assert "repaired 1 field" in second


class TestTheCoercionEvent:
    def test_one_event_with_the_surface_the_paths_the_rules_and_the_trace_id(
        self, temp_registry: StoreRegistry
    ) -> None:
        both = _context_repo()
        both["steps"] = _string_result()["steps"]

        reply = save_experience(trace_json=json.dumps(both))

        (event,) = _events(temp_registry, EventType.WRITE_COERCED)
        assert event.source == "mcp:save_experience"
        assert event.payload == {
            "tool": "save_experience",
            "repairs": [
                {"path": "steps[0].result", "rule": "str_to_summary"},
                {"path": "context.repo", "rule": "moved_to_metadata"},
            ],
            "trace_id": _trace_id(reply),
        }

    def test_the_event_carries_no_content(self, temp_registry: StoreRegistry) -> None:
        both = _context_repo()
        both["steps"] = _string_result()["steps"]
        both["intent"] = "intent text that must not be echoed"

        save_experience(trace_json=json.dumps(both))

        (event,) = _events(temp_registry, EventType.WRITE_COERCED)
        raw = json.dumps(event.model_dump(mode="json"), default=str)
        assert _NEEDLE not in raw
        assert _REPO not in raw
        assert "must not be echoed" not in raw
        assert set(event.payload) == {"tool", "repairs", "trace_id"}
        for repair in event.payload["repairs"]:
            assert set(repair) == {"path", "rule"}

    def test_a_coerced_accept_is_not_a_rejection_and_the_write_is_one_attempt(
        self, temp_registry: StoreRegistry
    ) -> None:
        save_experience(trace_json=json.dumps(_string_result()))

        report = summarize_write_health(temp_registry.operational.event_log, days=1)

        stats = report.by_tool["mcp:save_experience"]
        assert stats.attempts == 1
        assert stats.boundary_rejected == 0
        assert stats.executor_rejected == 0
        assert stats.coerced == 1

    def test_a_failing_event_log_does_not_fail_the_write(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = temp_registry.operational.event_log
        real = log.emit

        def flaky(event_type: EventType, *args: Any, **kwargs: Any) -> Event:
            if event_type is EventType.WRITE_COERCED:
                raise RuntimeError(_STORE_DOWN)
            return real(event_type, *args, **kwargs)

        monkeypatch.setattr(log, "emit", flaky)

        reply = save_experience(trace_json=json.dumps(_string_result()))

        assert reply.startswith("Trace saved: ")
        assert "repaired 1 field" in reply
        assert temp_registry.operational.trace_store.get(_trace_id(reply)) is not None
        assert _events(temp_registry, EventType.WRITE_COERCED) == []

    def test_an_unreachable_registry_is_swallowed_by_the_helper(self) -> None:
        import trellis.ops.write_health as write_health_mod

        class _Down:
            @property
            def operational(self) -> Any:
                raise RuntimeError(_STORE_DOWN)

        write_health_mod.record_trace_coercion(
            _Down(),  # type: ignore[arg-type]
            tool="save_experience",
            repairs=[{"path": "context.repo", "rule": "moved_to_metadata"}],
            trace_id="t",
        )


class TestWhatStaysRejected:
    @staticmethod
    def _rejection(trace: dict[str, Any]) -> str:
        with pytest.raises(MCPError) as excinfo:
            save_experience(trace_json=json.dumps(trace))
        return str(excinfo.value)

    def test_a_non_string_non_object_result_still_rejects(
        self, temp_registry: StoreRegistry
    ) -> None:
        trace = _trace(steps=[{"step_type": "t", "name": "a", "result": 7}])

        message = self._rejection(trace)

        assert "steps.0.result" in message or "steps[0].result" in message
        assert _events(temp_registry, EventType.WRITE_COERCED) == []
        assert len(_events(temp_registry, EventType.WRITE_REJECTED)) == 1

    def test_a_conflicting_metadata_repo_still_rejects_and_is_not_overwritten(
        self, temp_registry: StoreRegistry
    ) -> None:
        trace = _trace(context={"repo": "other/repo"}, metadata={"repo": "my/repo"})

        message = self._rejection(trace)

        assert "context" in message
        assert "repo" in message
        assert _events(temp_registry, EventType.WRITE_COERCED) == []
        assert temp_registry.operational.trace_store.count() == 0

    def test_a_mixed_trace_rejects_for_the_unrepairable_error_only(
        self, temp_registry: StoreRegistry
    ) -> None:
        trace = _string_result()
        trace["context"] = {"domain": "d", "repo": _REPO, "colour": "blue"}

        message = self._rejection(trace)

        # The rejection is about ``colour`` alone: the two repairable mistakes
        # are not reported, because they were repaired.
        assert "colour" in message
        assert "steps" not in message.replace("steps=", "")
        assert "context.repo" not in message
        assert _events(temp_registry, EventType.WRITE_COERCED) == []
        (rejection,) = _events(temp_registry, EventType.WRITE_REJECTED)
        assert rejection.source == "mcp:save_experience"
        assert temp_registry.operational.trace_store.count() == 0

    def test_a_remaining_error_names_the_path_it_always_did(
        self, temp_registry: StoreRegistry
    ) -> None:
        """The same trace, with and without a repairable mistake alongside."""
        bad_step = {"step_type": "t", "name": "b", "bogus": 1}
        plain = self._rejection(_trace(steps=[bad_step]))
        with_repair = self._rejection(
            _trace(steps=[{"step_type": "t", "name": "a", "result": "done"}, bad_step])
        )

        assert "bogus" in plain
        assert "bogus" in with_repair
