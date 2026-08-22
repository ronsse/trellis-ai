"""Capture-health threshold check tests (#309).

The rule under test is *per surface*: warn when one surface holds at
least ``threshold`` rejected writes (boundary + executor) in the trailing
window and no accepted write of its own. An accepted write clears the
surface it belongs to — not the other surfaces, which is the difference
that makes the check fire on the incident it was built for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trellis.ops.capture_health import (
    DEFAULT_THRESHOLD,
    DEFAULT_WINDOW_HOURS,
    CaptureHealthWarning,
    check_capture_health,
    format_capture_warning,
)
from trellis.stores.base.event_log import Event, EventLog, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's shell must not change what the suite asserts."""
    monkeypatch.delenv("TRELLIS_CAPTURE_WARN_THRESHOLD", raising=False)
    monkeypatch.delenv("TRELLIS_CAPTURE_WARN_WINDOW_HOURS", raising=False)


def _reject_boundary(
    event_log: EventLog, tool: str = "save_experience", n: int = 1
) -> None:
    for _ in range(n):
        event_log.emit(
            EventType.WRITE_REJECTED,
            f"mcp:{tool}",
            payload={"tool": tool, "stage": "boundary", "rejections": [], "hints": []},
        )


def _reject_executor(
    event_log: EventLog,
    requested_by: str = "mcp:save_knowledge",
    reason: str = "policy_violation",
    n: int = 1,
) -> None:
    for _ in range(n):
        event_log.emit(
            EventType.MUTATION_REJECTED,
            "mutation_executor",
            payload={"requested_by": requested_by, "reason": reason},
        )


def _accept(event_log: EventLog, requested_by: str = "mcp:save_experience") -> None:
    event_log.emit(
        EventType.MUTATION_EXECUTED,
        "mutation_executor",
        payload={"requested_by": requested_by, "status": "success"},
    )


class TestCheckCaptureHealth:
    def test_empty_log_is_healthy(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        assert check_capture_health(event_log) is None

    def test_below_threshold_is_healthy(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, n=DEFAULT_THRESHOLD - 1)
        assert check_capture_health(event_log) is None

    def test_threshold_with_zero_accepts_warns(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, n=DEFAULT_THRESHOLD)
        warning = check_capture_health(event_log)
        assert warning is not None
        assert warning.rejected == DEFAULT_THRESHOLD
        assert warning.accepted == 0
        assert warning.window_hours == DEFAULT_WINDOW_HOURS
        assert warning.failing_surfaces == ["mcp:save_experience"]
        # ``since`` dates the outage to the earliest rejection, not now.
        assert warning.since <= datetime.now(tz=UTC)

    def test_spread_across_surfaces_below_threshold_is_healthy(
        self, tmp_path: Path
    ) -> None:
        """The threshold is per surface, so scattered one-offs across
        several tools are not an outage."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        for tool in ("save_experience", "save_knowledge", "save_memory"):
            _reject_boundary(event_log, tool=tool, n=DEFAULT_THRESHOLD - 1)
        assert check_capture_health(event_log) is None

    def test_accepted_write_clears_its_own_surface(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, n=DEFAULT_THRESHOLD)
        _accept(event_log, requested_by="mcp:save_experience")
        assert check_capture_health(event_log) is None

    def test_accepted_write_elsewhere_does_not_clear(self, tmp_path: Path) -> None:
        """The motivating incident: every MCP save is rejected while a
        nightly ingest keeps landing writes. A global 'zero accepted'
        rule would stay silent through exactly this outage."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, n=DEFAULT_THRESHOLD)
        _accept(event_log, requested_by="cli:ingest")
        warning = check_capture_health(event_log)
        assert warning is not None
        assert warning.failing_surfaces == ["mcp:save_experience"]

    def test_boundary_and_executor_rejections_aggregate_per_surface(
        self, tmp_path: Path
    ) -> None:
        """A write dying at the policy gate is exactly as dark as one
        dying at the boundary — both count against the same surface."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, tool="save_experience", n=2)
        _reject_executor(event_log, requested_by="mcp:save_experience")
        warning = check_capture_health(event_log, threshold=3)
        assert warning is not None
        assert warning.rejected == 3
        assert warning.failing_surfaces == ["mcp:save_experience"]

    def test_only_failing_surfaces_are_counted(self, tmp_path: Path) -> None:
        """A surface that is still landing writes contributes neither its
        name nor its rejections to the warning."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, tool="save_experience", n=4)
        _reject_boundary(event_log, tool="save_memory", n=3)
        _accept(event_log, requested_by="mcp:save_memory")
        warning = check_capture_health(event_log)
        assert warning is not None
        assert warning.failing_surfaces == ["mcp:save_experience"]
        assert warning.rejected == 4

    def test_surfaces_ordered_most_rejected_first(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, tool="save_memory", n=DEFAULT_THRESHOLD)
        _reject_boundary(event_log, tool="save_experience", n=DEFAULT_THRESHOLD + 1)
        warning = check_capture_health(event_log)
        assert warning is not None
        assert warning.failing_surfaces == ["mcp:save_experience", "mcp:save_memory"]
        assert warning.rejected == 2 * DEFAULT_THRESHOLD + 1

    def test_idempotency_replay_is_not_a_capture_failure(self, tmp_path: Path) -> None:
        """A replayed command is a duplicate submission of a write that
        already landed, not a write that went dark."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_executor(
            event_log,
            requested_by="mcp:execute_mutation",
            reason="idempotency_replay",
            n=DEFAULT_THRESHOLD,
        )
        assert check_capture_health(event_log) is None

    def test_window_excludes_older_rejections(self, tmp_path: Path) -> None:
        """A backdated outage that has aged out of the window is over."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        stale = datetime.now(tz=UTC) - timedelta(hours=25)
        for _ in range(DEFAULT_THRESHOLD):
            event_log.append(
                Event(
                    event_type=EventType.WRITE_REJECTED,
                    source="mcp:save_experience",
                    occurred_at=stale,
                    payload={"tool": "save_experience", "stage": "boundary"},
                )
            )
        assert check_capture_health(event_log, window_hours=24) is None
        assert check_capture_health(event_log, window_hours=48) is not None

    def test_since_is_the_earliest_rejection_in_the_window(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        oldest = datetime.now(tz=UTC) - timedelta(hours=6)
        event_log.append(
            Event(
                event_type=EventType.WRITE_REJECTED,
                source="mcp:save_experience",
                occurred_at=oldest,
                payload={"tool": "save_experience", "stage": "boundary"},
            )
        )
        _reject_boundary(event_log, n=DEFAULT_THRESHOLD - 1)
        warning = check_capture_health(event_log)
        assert warning is not None
        assert warning.since == oldest

    def test_env_threshold_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, n=1)
        monkeypatch.setenv("TRELLIS_CAPTURE_WARN_THRESHOLD", "1")
        assert check_capture_health(event_log) is not None

    def test_explicit_kwarg_wins_over_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, n=1)
        monkeypatch.setenv("TRELLIS_CAPTURE_WARN_THRESHOLD", "1")
        assert check_capture_health(event_log, threshold=5) is None

    def test_threshold_zero_disables_without_touching_the_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_CAPTURE_WARN_THRESHOLD", "0")
        event_log = MagicMock(spec=EventLog)
        assert check_capture_health(event_log) is None
        event_log.count.assert_not_called()

    def test_malformed_env_falls_back_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, n=DEFAULT_THRESHOLD - 1)
        monkeypatch.setenv("TRELLIS_CAPTURE_WARN_THRESHOLD", "lots")
        assert check_capture_health(event_log) is None
        _reject_boundary(event_log, n=1)
        assert check_capture_health(event_log) is not None

    def test_healthy_path_never_fetches_event_rows(self) -> None:
        """Below threshold the check must stay count-only — it runs on
        every retrieval call."""
        event_log = MagicMock(spec=EventLog)
        event_log.count.return_value = 0
        assert check_capture_health(event_log) is None
        event_log.get_events.assert_not_called()

    def test_detail_fetch_reads_oldest_first(self, tmp_path: Path) -> None:
        """``since`` stays truthful under the detail cap only because the
        fetch is ascending — pin the order the call depends on."""
        event_log = MagicMock(spec=EventLog)
        event_log.count.return_value = DEFAULT_THRESHOLD
        event_log.get_events.return_value = []
        check_capture_health(event_log)
        orders = {call.kwargs.get("order") for call in event_log.get_events.mock_calls}
        assert orders == {"asc"}


class TestFormatCaptureWarning:
    def _warning(
        self, surfaces: list[str], since: datetime | None = None
    ) -> CaptureHealthWarning:
        return CaptureHealthWarning(
            window_hours=24,
            rejected=4,
            failing_surfaces=surfaces,
            since=since or datetime(2026, 8, 21, 4, 12, tzinfo=UTC),
        )

    def test_names_surface_since_and_remedy(self) -> None:
        text = format_capture_warning(self._warning(["mcp:save_experience"]))
        assert text.startswith("> **WARNING: memory capture is failing.**")
        assert "mcp:save_experience" in text
        assert "since 2026-08-21 04:12 UTC" in text
        assert "4 write attempt(s) rejected and 0 accepted" in text
        assert "`trellis analyze health`" in text

    def test_since_is_converted_not_relabelled(self) -> None:
        """Postgres hands back ``timestamptz`` in the session timezone; a
        banner that stamps 'UTC' on a local wall clock lies about the one
        field the operator needs."""
        local = datetime(2026, 8, 21, 4, 12, tzinfo=UTC).astimezone(
            timezone(timedelta(hours=-7))
        )
        text = format_capture_warning(self._warning(["mcp:save_memory"], since=local))
        assert "since 2026-08-21 04:12 UTC" in text

    def test_caps_named_surfaces(self) -> None:
        text = format_capture_warning(self._warning(["a", "b", "c", "d", "e"]))
        assert "a, b, c (+2 more)" in text
        assert "d" not in text.split("(+2 more)")[0].split("from: ")[1]

    def test_no_surfaces_renders_unknown(self) -> None:
        text = format_capture_warning(self._warning([]))
        assert "(unknown)" in text
