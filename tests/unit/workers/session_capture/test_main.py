"""Tests for the capture-sweep entry point — stdout report and exit codes.

``__main__`` is argparse plus the exit-code contract; the sweep itself is
covered by ``test_sweep.py``. The contract under test: the JSON report owns
stdout, a missing judge exits non-zero with no report (nothing ran), a judge
that vanished mid-sweep is counted and — under the default strict mode — also
exits non-zero, and a typed Trellis error (a refused ``config.yaml``) exits
``5`` with its message and no traceback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog

from tests.structlog_isolation import clear_cached_logger_proxies
from trellis.errors import ConfigError, StoreError, TrellisError
from trellis_cli.exit_codes import EXIT_STORE
from trellis_workers.session_capture import __main__ as capture_main
from trellis_workers.session_capture.models import CaptureReport
from trellis_workers.session_capture.sweep import ENV_ROOT, ENV_WATERMARK

#: The shape #646's own refusal tests load
#: (``tests/unit/stores/test_registry_config_literals.py``): a value that is
#: only a ``${VAR}`` placeholder, which ``StoreRegistry.from_config_dir``
#: refuses before any store is built.
_NEO4J_CONFIG = """\
knowledge:
  graph:
    backend: neo4j
    uri: bolt://example.invalid:7687
    password: ${TRELLIS_NEO4J_PASSWORD}
"""


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

    def test_errored_sessions_fail_a_strict_run_under_their_own_code(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.delenv("TRELLIS_CAPTURE_STRICT", raising=False)
        monkeypatch.setattr(
            capture_main,
            "run_sweep",
            MagicMock(return_value=_report(sessions_seen=3, sessions_errored=2)),
        )

        exit_code = capture_main.main([])

        # Distinct from a judge outage, and from argparse's usage error (2).
        assert exit_code == capture_main.EXIT_SESSIONS_ERRORED
        assert exit_code not in {0, 1, 2}
        captured = capsys.readouterr()
        assert json.loads(captured.out)["sessions_errored"] == 2
        assert "2 session(s) raised mid-sweep" in captured.err

    def test_errored_sessions_under_the_opt_out_report_and_exit_zero(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("TRELLIS_CAPTURE_STRICT", "0")
        monkeypatch.setattr(
            capture_main,
            "run_sweep",
            MagicMock(return_value=_report(sessions_seen=4, sessions_errored=1)),
        )

        assert capture_main.main([]) == capture_main.EXIT_OK

        captured = capsys.readouterr()
        assert json.loads(captured.out)["sessions_errored"] == 1
        assert "1 session(s) raised mid-sweep" in captured.err

    def test_an_outage_and_an_errored_session_both_print(
        self, monkeypatch, capsys
    ) -> None:
        """Both lines print before the exit is decided; the outage code wins."""
        monkeypatch.delenv("TRELLIS_CAPTURE_STRICT", raising=False)
        report = _unjudged_report()
        report.sessions_errored = 1
        monkeypatch.setattr(capture_main, "run_sweep", MagicMock(return_value=report))

        assert capture_main.main([]) == capture_main.EXIT_JUDGE_UNAVAILABLE

        err = capsys.readouterr().err
        assert "2 session(s) left unjudged" in err
        assert "1 session(s) raised mid-sweep" in err

    # StoreError too: the arm catches the typed family, so narrowing it to
    # ConfigError (the one member today's routes raise) must fail here.
    @pytest.mark.parametrize("error_cls", [ConfigError, StoreError])
    def test_typed_trellis_error_exits_five_with_its_message(
        self,
        error_cls: type[TrellisError],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A refused config is an operator error: its message, not a traceback."""
        message = "/srv/trellis/config.yaml: knowledge.graph.uri is not a string"
        monkeypatch.setattr(
            capture_main, "run_sweep", MagicMock(side_effect=error_cls(message))
        )

        exit_code = capture_main.main([])

        assert exit_code == capture_main.EXIT_TRELLIS_ERROR
        # `trellis worker capture-sessions` exits this for the same fault.
        assert exit_code == EXIT_STORE
        captured = capsys.readouterr()
        # The operator's line, not only the structured log event's copy of it.
        assert f"trellis-session-capture: {message}\n" in captured.err
        assert "capture_sweep_refused" in captured.err
        assert "Traceback" not in captured.err
        assert captured.out == ""

    def test_refused_config_exits_five_on_the_unmocked_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """#646's refusal reaches this entry through the real sweep and stops here."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(_NEO4J_CONFIG, encoding="utf-8")
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv(ENV_ROOT, str(tmp_path / "transcripts"))
        monkeypatch.setenv(ENV_WATERMARK, str(tmp_path / "watermark.json"))

        assert capture_main.main([]) == capture_main.EXIT_TRELLIS_ERROR

        captured = capsys.readouterr()
        assert (
            "knowledge.graph.password is the literal text ${TRELLIS_NEO4J_PASSWORD}"
            in captured.err
        )
        assert str(config_dir / "config.yaml") in captured.err
        assert "Traceback" not in captured.err
        assert captured.out == ""

    def test_untyped_exception_is_not_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the typed family is caught: an untyped error keeps its traceback."""
        monkeypatch.setattr(
            capture_main, "run_sweep", MagicMock(side_effect=KeyError("x"))
        )

        with pytest.raises(KeyError):
            capture_main.main([])
