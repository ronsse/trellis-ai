"""Tests for the capture-sweep entry point — env wiring and loud failure.

``__main__`` is the file that reads every ``TRELLIS_CAPTURE_*`` env var and
builds the judge client, and it is the code path behind all three front doors
(``python -m``, the ``trellis-session-capture`` console script, and
``trellis worker capture-sessions``). The failure mode it guards is specific:
distillation fail-closes on a missing client, so a misconfigured ``llm:`` block
used to produce a sweep that judged nothing, wrote nothing, and exited 0.
"""

from __future__ import annotations

import gc
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog
from structlog._config import BoundLoggerLazyProxy

from trellis.errors import BackendNotInstalledError
from trellis_workers.session_capture import __main__ as capture_main
from trellis_workers.session_capture.models import CaptureReport

#: Attributes a freshly-built ``BoundLoggerLazyProxy`` carries; anything else
#: is a memoised bind stuck on by ``cache_logger_on_first_use``. Mirrors the
#: eviction in ``tests/unit/cli/conftest.py`` — see that file for the full
#: rationale.
_PROXY_BASELINE_ATTRS = frozenset(
    {
        "_logger",
        "_wrapper_class",
        "_processors",
        "_context_class",
        "_cache_logger_on_first_use",
        "_initial_values",
        "_logger_factory_args",
    }
)


@pytest.fixture(autouse=True)
def _isolate_structlog():
    """Undo the structlog reconfiguration ``main()`` performs.

    ``main`` pins structlog to *the current* ``sys.stderr`` so the JSON report
    keeps stdout to itself. Under pytest that handle is capsys' replacement,
    which is closed at teardown — leaving a global config (and cached binds)
    holding a dead file that breaks every later test that logs.
    """
    yield
    for obj in gc.get_objects():
        if isinstance(obj, BoundLoggerLazyProxy):
            for attr in [k for k in obj.__dict__ if k not in _PROXY_BASELINE_ATTRS]:
                delattr(obj, attr)
    structlog.reset_defaults()


def _report(**overrides: Any) -> CaptureReport:
    report = CaptureReport(transcripts_root="transcripts-root")
    for key, value in overrides.items():
        setattr(report, key, value)
    return report


@pytest.fixture
def registry() -> MagicMock:
    """A registry whose ``build_llm_client`` yields a usable judge."""
    reg = MagicMock()
    reg.build_llm_client.return_value = MagicMock(name="llm-client")
    return reg


class TestSampleDenominator:
    def test_unset_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.delenv("TRELLIS_CAPTURE_SAMPLE_DENOMINATOR", raising=False)
        assert capture_main._sample_denominator() == 5

    def test_valid_value_is_honoured(self, monkeypatch) -> None:
        monkeypatch.setenv("TRELLIS_CAPTURE_SAMPLE_DENOMINATOR", "3")
        assert capture_main._sample_denominator() == 3

    @pytest.mark.parametrize("raw", ["not-a-number", "0", "-4", "  "])
    def test_unusable_values_fall_back_to_default(self, monkeypatch, raw) -> None:
        monkeypatch.setenv("TRELLIS_CAPTURE_SAMPLE_DENOMINATOR", raw)
        assert capture_main._sample_denominator() == 5


class TestBuildLlmClient:
    def test_returns_the_configured_client(self, registry) -> None:
        assert capture_main._build_llm_client(registry) is (
            registry.build_llm_client.return_value
        )

    def test_unconfigured_llm_block_raises(self, registry) -> None:
        registry.build_llm_client.return_value = None
        with pytest.raises(capture_main.CaptureJudgeUnavailableError) as exc:
            capture_main._build_llm_client(registry)
        assert "no distillation judge is configured" in str(exc.value)
        assert "llm:" in str(exc.value)

    def test_missing_sdk_extra_raises_with_the_cause(self, registry) -> None:
        registry.build_llm_client.side_effect = BackendNotInstalledError(
            backend_name="openai", extra="llm-openai"
        )
        with pytest.raises(capture_main.CaptureJudgeUnavailableError) as exc:
            capture_main._build_llm_client(registry)
        assert "openai" in str(exc.value)
        assert "llm-openai" in str(exc.value)


class TestJudgeUnavailableSessions:
    def test_counts_only_distill_unavailable_warnings(self) -> None:
        report = _report(
            warnings=[
                {"kind": "distill_unavailable", "session_id": "a"},
                {"kind": "distill_unavailable", "session_id": "b"},
                {"kind": "something_else", "session_id": "c"},
            ]
        )
        assert capture_main.judge_unavailable_sessions(report) == 2

    def test_clean_report_counts_zero(self) -> None:
        assert capture_main.judge_unavailable_sessions(_report()) == 0


class TestRunSweep:
    def test_env_vars_drive_the_sweep(self, monkeypatch, tmp_path, registry) -> None:
        monkeypatch.setenv(
            "TRELLIS_CAPTURE_TRANSCRIPTS_ROOT", str(tmp_path / "transcripts")
        )
        monkeypatch.setenv("TRELLIS_CAPTURE_WATERMARK", str(tmp_path / "wm.json"))
        monkeypatch.setenv("TRELLIS_CAPTURE_SOURCE_SYSTEM", "claude-code-test")
        monkeypatch.setenv("TRELLIS_CAPTURE_SAMPLE_DENOMINATOR", "7")
        monkeypatch.setenv("TRELLIS_DISTILL_MODEL", "tiny-local:1b")

        spy = MagicMock(return_value=_report())
        monkeypatch.setattr(capture_main, "run_capture", spy)

        capture_main.run_sweep(registry=registry, dry_run=True)

        _, kwargs = spy.call_args
        assert spy.call_args[0][0] is registry
        assert kwargs["transcripts_root"] == tmp_path / "transcripts"
        assert kwargs["watermark_path"] == tmp_path / "wm.json"
        assert kwargs["source_system"] == "claude-code-test"
        assert kwargs["sample_denominator"] == 7
        assert kwargs["distill_model_id"] == "tiny-local:1b"
        assert kwargs["dry_run"] is True
        assert kwargs["llm_client"] is registry.build_llm_client.return_value

    def test_defaults_apply_when_env_is_empty(
        self, monkeypatch, tmp_path, registry
    ) -> None:
        for var in (
            "TRELLIS_CAPTURE_TRANSCRIPTS_ROOT",
            "TRELLIS_CAPTURE_WATERMARK",
            "TRELLIS_CAPTURE_SOURCE_SYSTEM",
            "TRELLIS_CAPTURE_SAMPLE_DENOMINATOR",
            "TRELLIS_DISTILL_MODEL",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "cfg"))
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))

        spy = MagicMock(return_value=_report())
        monkeypatch.setattr(capture_main, "run_capture", spy)

        capture_main.run_sweep(registry=registry)

        kwargs = spy.call_args[1]
        assert kwargs["transcripts_root"] == tmp_path / "home" / ".claude" / "projects"
        assert kwargs["watermark_path"] == tmp_path / "cfg" / "capture-watermark.json"
        assert kwargs["source_system"] == "claude-code"
        assert kwargs["dry_run"] is False

    def test_builds_a_registry_when_none_is_injected(
        self, monkeypatch, registry
    ) -> None:
        from_config_dir = MagicMock(return_value=registry)
        monkeypatch.setattr(
            capture_main.StoreRegistry, "from_config_dir", from_config_dir
        )
        spy = MagicMock(return_value=_report())
        monkeypatch.setattr(capture_main, "run_capture", spy)

        capture_main.run_sweep()

        from_config_dir.assert_called_once_with()
        assert spy.call_args[0][0] is registry

    def test_missing_judge_aborts_before_sweeping(self, monkeypatch, registry) -> None:
        registry.build_llm_client.return_value = None
        spy = MagicMock(return_value=_report())
        monkeypatch.setattr(capture_main, "run_capture", spy)

        with pytest.raises(capture_main.CaptureJudgeUnavailableError):
            capture_main.run_sweep(registry=registry)

        spy.assert_not_called()


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

    def test_unjudged_sessions_are_counted_and_exit_nonzero(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(
            capture_main,
            "run_sweep",
            MagicMock(
                return_value=_report(
                    sessions_seen=3,
                    warnings=[
                        {"kind": "distill_unavailable", "session_id": "a"},
                        {"kind": "distill_unavailable", "session_id": "b"},
                    ],
                )
            ),
        )

        exit_code = capture_main.main([])

        assert exit_code == capture_main.EXIT_JUDGE_UNAVAILABLE
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["sessions_judge_unavailable"] == 2
        assert "2 session(s) left unjudged" in captured.err
