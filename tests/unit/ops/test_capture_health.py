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

from trellis.ops import capture_health
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

    def test_detail_fetch_reads_newest_first(self, tmp_path: Path) -> None:
        """The detail cap must drop the OLDEST rejections, not the newest.

        Reversed from the pre-#374 behaviour deliberately. The per-surface
        counts are computed from this slice, so an ascending fetch that
        truncates cannot see a surface whose rejections are all recent —
        i.e. the banner goes silent through a *fresh* outage, which is the
        one thing it exists to catch.
        """
        event_log = MagicMock(spec=EventLog)
        event_log.count.return_value = DEFAULT_THRESHOLD
        event_log.get_events.return_value = []
        check_capture_health(event_log)
        orders = {call.kwargs.get("order") for call in event_log.get_events.mock_calls}
        assert orders == {"desc"}

    def test_truncated_detail_still_sees_a_fresh_outage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression #374 describes, end to end.

        A noisy older surface fills the detail slice; a different surface
        goes dark just now. Under the old ascending fetch the new one was
        invisible and the banner named only the stale problem.
        """
        monkeypatch.setattr(capture_health, "_DETAIL_LIMIT", 4)
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, tool="old_and_noisy", n=4)
        _reject_boundary(event_log, tool="just_went_dark", n=DEFAULT_THRESHOLD)

        warning = check_capture_health(event_log)
        assert warning is not None
        assert "mcp:just_went_dark" in warning.failing_surfaces
        # And the report admits the slice was capped, so `rejected` and
        # `since` are read as bounds rather than as exact.
        assert warning.truncated is True
        assert "at least" in format_capture_warning(warning)

    def test_untruncated_warning_states_exact_counts(self, tmp_path: Path) -> None:
        """The truncation disclosure must be able to read False — otherwise
        it is a constant that always hedges."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, n=DEFAULT_THRESHOLD)
        warning = check_capture_health(event_log)
        assert warning is not None
        assert warning.truncated is False
        rendered = format_capture_warning(warning)
        assert "at least" not in rendered
        assert "since at or before" not in rendered


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


class TestGlobalSurfaceRecovery:
    """A config failure fails every surface, so it clears differently (#425).

    ``config:policy_file`` marks a write that died at gate-build time — the
    deployment's own ``policies.json`` would not load, before a Command
    existed. No ``MUTATION_EXECUTED`` can ever carry that ``requested_by``,
    so under the per-surface accept rule the banner could **never** clear: a
    one-character fix would leave it firing for a full window on a
    deployment that was writing normally again, which is precisely how an
    operator learns to ignore a banner.

    The recovery evidence is an accepted write from any surface *after* the
    last rejection — the thing a loaded gate produces and a broken one
    cannot. "After the last rejection" rather than "anywhere in the window"
    is what keeps it from silencing a live outage on a busy deployment.
    """

    def test_it_fires_while_the_gate_is_broken(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, tool="config:policy_file", n=3)

        warning = check_capture_health(event_log, threshold=3)
        assert warning is not None
        assert warning.failing_surfaces == ["config:policy_file"]

    def test_earlier_accepts_do_not_silence_a_live_outage(self, tmp_path: Path) -> None:
        """The case that breaks a naive "any accept in the window" rule.

        A deployment writing normally all day, then a policy file edited an
        hour ago. Every accept in the window predates every rejection, and
        the banner must still fire.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _accept(event_log, requested_by="mcp:save_experience")
        _accept(event_log, requested_by="mcp:save_memory")
        _reject_boundary(event_log, tool="config:policy_file", n=3)

        warning = check_capture_health(event_log, threshold=3)
        assert warning is not None
        assert warning.failing_surfaces == ["config:policy_file"]

    def test_one_write_landing_after_the_fix_clears_it(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, tool="config:policy_file", n=3)
        assert check_capture_health(event_log, threshold=3) is not None

        _accept(event_log, requested_by="mcp:save_experience")

        assert check_capture_health(event_log, threshold=3) is None

    def test_the_global_rule_does_not_leak_to_ordinary_surfaces(
        self, tmp_path: Path
    ) -> None:
        """An accept elsewhere must not clear a per-surface outage.

        This is #309's motivating incident: a nightly ingest landing rows
        while every ``save_*`` call is rejected. The prefix rule must not
        widen into the global check that incident was built to defeat.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _reject_boundary(event_log, tool="save_experience", n=3)
        _accept(event_log, requested_by="cli:ingest_corpus")

        warning = check_capture_health(event_log, threshold=3)
        assert warning is not None
        assert warning.failing_surfaces == ["mcp:save_experience"]
