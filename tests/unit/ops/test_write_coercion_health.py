"""``WRITE_COERCED`` rows are counted per rule beside rejections, not as them."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.ops.write_health import (
    record_trace_coercion,
    summarize_write_health,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

_STR = "str_to_summary"
_REPO = "moved_to_metadata"


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores = tmp_path / "stores"
    stores.mkdir()
    return StoreRegistry(stores_dir=stores)


def _coerce(reg: StoreRegistry, tool: str, *rules: str, trace_id: str = "t1") -> None:
    record_trace_coercion(
        reg,
        tool=tool,
        repairs=[{"path": f"p{i}", "rule": rule} for i, rule in enumerate(rules)],
        trace_id=trace_id,
    )


def _accept(reg: StoreRegistry, requested_by: str) -> None:
    reg.operational.event_log.emit(
        EventType.MUTATION_EXECUTED,
        source="mutation_executor",
        payload={
            "requested_by": requested_by,
            "operation": "trace.ingest",
            "command_id": "c",
            "target_id": "t",
        },
    )


def _reject(reg: StoreRegistry, tool: str) -> None:
    reg.operational.event_log.emit(
        EventType.WRITE_REJECTED,
        source=f"mcp:{tool}",
        payload={"tool": tool, "stage": "boundary", "errors": []},
    )


def _report(reg: StoreRegistry):
    return summarize_write_health(reg.operational.event_log, days=1)


class TestCounts:
    def test_counts_per_rule_per_surface_and_overall(
        self, registry: StoreRegistry
    ) -> None:
        for tool, rules in (
            ("save_experience", (_STR, _REPO)),
            ("save_experience", (_STR,)),
            ("api:ingest-trace", (_REPO,)),
        ):
            _accept(registry, "mcp:save_experience" if "api" not in tool else tool)
            _coerce(registry, tool, *rules)

        report = _report(registry)

        mcp = report.by_tool["mcp:save_experience"]
        assert mcp.coerced == 2
        assert mcp.coerced_by_rule == {_STR: 2, _REPO: 1}
        api = report.by_tool["api:ingest-trace"]
        assert api.coerced == 1
        assert api.coerced_by_rule == {_REPO: 1}
        assert report.coerced == 3
        assert report.coerced_by_rule == {_STR: 2, _REPO: 2}

    def test_a_rule_repeated_inside_one_call_counts_the_rule_once(
        self, registry: StoreRegistry
    ) -> None:
        _accept(registry, "mcp:save_experience")
        _coerce(registry, "save_experience", _STR, _STR, _STR)

        stats = _report(registry).by_tool["mcp:save_experience"]

        assert stats.coerced == 1
        assert stats.coerced_by_rule == {_STR: 1}

    def test_the_rate_is_coerced_over_the_surfaces_attempts(
        self, registry: StoreRegistry
    ) -> None:
        for _ in range(4):
            _accept(registry, "mcp:save_experience")
        _coerce(registry, "save_experience", _STR)

        report = _report(registry)

        assert report.by_tool["mcp:save_experience"].coercion_rate == pytest.approx(
            0.25
        )
        assert report.coercion_rate == pytest.approx(0.25)

    def test_no_coercion_leaves_the_rate_absent_and_the_counts_zero(
        self, registry: StoreRegistry
    ) -> None:
        _accept(registry, "mcp:save_experience")

        report = _report(registry)

        assert report.coerced == 0
        assert report.coerced_by_rule == {}
        assert report.coercion_rate == 0.0
        assert report.by_tool["mcp:save_experience"].coercion_rate is None

    def test_a_malformed_row_still_counts_the_call(
        self, registry: StoreRegistry
    ) -> None:
        _accept(registry, "mcp:save_experience")
        registry.operational.event_log.emit(
            EventType.WRITE_COERCED,
            source="mcp:save_experience",
            payload={"tool": "save_experience", "repairs": "garbage"},
        )

        stats = _report(registry).by_tool["mcp:save_experience"]

        assert stats.coerced == 1
        assert stats.coerced_by_rule == {}


class TestBesideRejectionsNotAsThem:
    def test_a_coerced_call_is_not_a_rejection_and_not_a_second_attempt(
        self, registry: StoreRegistry
    ) -> None:
        _accept(registry, "mcp:save_experience")
        _coerce(registry, "save_experience", _STR)
        _reject(registry, "save_experience")

        stats = _report(registry).by_tool["mcp:save_experience"]

        assert stats.attempts == 2
        assert stats.boundary_rejected == 1
        assert stats.coerced == 1

    def test_a_coercion_alone_is_not_an_attempt(self, registry: StoreRegistry) -> None:
        """The accepted write is the attempt; the coercion row only annotates it."""
        _coerce(registry, "save_experience", _STR)

        report = _report(registry)

        assert report.by_tool["mcp:save_experience"].attempts == 0
        assert report.by_tool["mcp:save_experience"].coerced == 1


class TestRecordHelper:
    def test_no_repairs_emits_nothing(self, registry: StoreRegistry) -> None:
        record_trace_coercion(
            registry, tool="save_experience", repairs=[], trace_id="t"
        )

        assert (
            registry.operational.event_log.get_events(
                event_type=EventType.WRITE_COERCED, limit=10
            )
            == []
        )

    def test_the_payload_is_exactly_surface_repairs_and_trace_id(
        self, registry: StoreRegistry
    ) -> None:
        _coerce(registry, "cli:ingest-trace", _STR, trace_id="abc")

        (event,) = registry.operational.event_log.get_events(
            event_type=EventType.WRITE_COERCED, limit=10
        )

        assert event.source == "cli:ingest-trace"
        assert event.payload == {
            "tool": "cli:ingest-trace",
            "repairs": [{"path": "p0", "rule": _STR}],
            "trace_id": "abc",
        }
