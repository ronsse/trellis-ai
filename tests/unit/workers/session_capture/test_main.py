"""Tests for the capture-sweep entry point — stdout report and exit codes.

``__main__`` is argparse plus the exit-code contract; the sweep itself is
covered by ``test_sweep.py``. The contract under test: the JSON report owns
stdout, a missing judge exits non-zero with no report (nothing ran), and a
judge that vanished mid-sweep is counted and — under the default strict mode —
also exits non-zero.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog

from tests.structlog_isolation import clear_cached_logger_proxies
from trellis_workers.session_capture import __main__ as capture_main
from trellis_workers.session_capture.models import CaptureReport


@pytest.fixture(autouse=True)
def _isolate_structlog():
    """Undo the structlog reconfiguration ``main()`` performs.

    ``main`` pins structlog to *the current* ``sys.stderr`` so the JSON report
    keeps stdout to itself. Under pytest that handle is capsys' replacement,
    which is closed at teardown — leaving a global config (and cached binds)
    holding a dead file that breaks every later test that logs.
    """
    yield
    clear_cached_logger_proxies()
    structlog.reset_defaults()


def _report(**overrides: Any) -> CaptureReport:
    report = CaptureReport(transcripts_root="transcripts-root")
    for key, value in overrides.items():
        setattr(report, key, value)
    return report


def _unjudged_report() -> CaptureReport:
    return _report(
        sessions_seen=3,
        warnings=[
            {"kind": "distill_unavailable", "session_id": "a"},
            {"kind": "distill_unavailable", "session_id": "b"},
        ],
    )


class TestMain:
    def test_clean_sweep_prints_json_and_exits_zero(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            capture_main, "run_sweep", MagicMock(return_value=_report(sessions_seen=2))
        )

        assert capture_main.main([]) == capture_main.EXIT_OK

        payload = json.loads(capsys.readouterr().out)
        assert payload["sessions_seen"] == 2
        assert payload["sessions_judge_unavailable"] == 0

    def test_dry_run_flag_is_forwarded(self, monkeypatch) -> None:
        spy = MagicMock(return_value=_report())
        monkeypatch.setattr(capture_main, "run_sweep", spy)

        capture_main.main(["--dry-run"])

        spy.assert_called_once_with(dry_run=True)

    def test_unconfigured_judge_exits_nonzero_and_says_why(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(
            capture_main,
            "run_sweep",
            MagicMock(
                side_effect=capture_main.CaptureJudgeUnavailableError(
                    "no distillation judge is configured. Configure an 'llm:' block"
                )
            ),
        )

        exit_code = capture_main.main([])

        assert exit_code == capture_main.EXIT_JUDGE_UNAVAILABLE
        captured = capsys.readouterr()
        assert "no distillation judge is configured" in captured.err
        # No report on stdout: there was no sweep to report.
        assert captured.out == ""

    def test_no_judge_at_all_fails_even_when_not_strict(
        self, monkeypatch, capsys
    ) -> None:
        """The opt-out covers *partial* outages only — a total no-op still fails."""
        monkeypatch.setenv("TRELLIS_CAPTURE_STRICT", "0")
        monkeypatch.setattr(
            capture_main,
            "run_sweep",
            MagicMock(
                side_effect=capture_main.CaptureJudgeUnavailableError("no judge")
            ),
        )

        assert capture_main.main([]) == capture_main.EXIT_JUDGE_UNAVAILABLE
        capsys.readouterr()

    def test_unjudged_sessions_are_counted_and_exit_nonzero(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.delenv("TRELLIS_CAPTURE_STRICT", raising=False)
        monkeypatch.setattr(
            capture_main, "run_sweep", MagicMock(return_value=_unjudged_report())
        )

        exit_code = capture_main.main([])

        assert exit_code == capture_main.EXIT_JUDGE_UNAVAILABLE
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["sessions_judge_unavailable"] == 2
        assert "2 session(s) left unjudged" in captured.err

    def test_strict_opt_out_reports_but_exits_zero(self, monkeypatch, capsys) -> None:
        """``TRELLIS_CAPTURE_STRICT=0`` keeps the count, drops the failed unit.

        Those sessions stay un-watermarked and are retried next sweep, so an
        operator can treat a transient model timeout as self-healing rather
        than a failed systemd unit — without losing the signal.
        """
        monkeypatch.setenv("TRELLIS_CAPTURE_STRICT", "0")
        monkeypatch.setattr(
            capture_main, "run_sweep", MagicMock(return_value=_unjudged_report())
        )

        assert capture_main.main([]) == capture_main.EXIT_OK

        captured = capsys.readouterr()
        assert json.loads(captured.out)["sessions_judge_unavailable"] == 2
        assert "2 session(s) left unjudged" in captured.err
