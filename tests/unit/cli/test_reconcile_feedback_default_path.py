"""``admin reconcile-feedback`` finds the audit log, and says when it is not there.

The MCP tool and the REST route write ``pack_feedback.jsonl`` under
``<stores_dir>/feedback``. The command used to demand ``--log-dir`` and read
the data directory the docs named, where no log lives, so it scanned zero rows
and reported ``ok`` and exit 0, which is also what a healthy reconcile with
nothing to do prints. The default is now the one directory the writers use, and
a directory with no log is reported as ``no_log`` in both formats.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import feedback_log_dir, record_feedback
from trellis_cli.exit_codes import EXIT_OK, EXIT_VALIDATION
from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    assert runner.invoke(app, ["admin", "init"]).exit_code == EXIT_OK
    return tmp_path / "data"


def _write_row(data_dir: Path) -> None:
    feedback = PackFeedback(
        run_id="run-1", phase="p", intent="i", outcome="success", items_served=["a"]
    )
    record_feedback(feedback, log_dir=feedback_log_dir(data_dir / "stores"))


def _run(*args: str) -> tuple[int, dict, str]:
    as_json = runner.invoke(
        app, ["admin", "reconcile-feedback", *args, "--format", "json"]
    )
    as_text = runner.invoke(app, ["admin", "reconcile-feedback", *args])
    assert as_json.exit_code == as_text.exit_code, (as_json.output, as_text.output)
    return as_json.exit_code, json.loads(as_json.stdout), as_text.output


def test_default_path_finds_the_log_the_writers_use(data_dir: Path) -> None:
    _write_row(data_dir)

    exit_code, payload, _ = _run("--dry-run")

    assert exit_code == EXIT_OK
    assert payload["status"] == "ok"
    assert payload["scanned"] == 1
    assert payload["would_emit"] == 1
    assert payload["log_path"] == str(
        feedback_log_dir(data_dir / "stores") / "pack_feedback.jsonl"
    )


def test_default_path_reconciles_for_real(data_dir: Path) -> None:
    _write_row(data_dir)

    result = runner.invoke(app, ["admin", "reconcile-feedback", "--format", "json"])

    payload = json.loads(result.stdout)
    assert (payload["scanned"], payload["emitted"]) == (1, 1)


def test_the_data_dir_is_not_the_log_dir_and_says_so(data_dir: Path) -> None:
    _write_row(data_dir)

    exit_code, payload, text = _run("--log-dir", str(data_dir))

    assert exit_code == EXIT_VALIDATION
    assert payload["status"] == "no_log"
    assert payload["scanned"] == 0
    assert payload["log_path"].endswith("pack_feedback.jsonl")
    assert "omit --log-dir" in payload["message"]
    assert "No log" in text
    assert "omit --log-dir" in " ".join(text.split())


def test_no_log_under_the_default_path_is_reported_not_passed(data_dir: Path) -> None:
    exit_code, payload, text = _run()

    assert exit_code == EXIT_OK
    assert payload["status"] == "no_log"
    assert payload["scanned"] == 0
    assert "No log" in text


def test_dry_run_reports_no_log_too(data_dir: Path) -> None:
    exit_code, payload, _ = _run("--dry-run")

    assert exit_code == EXIT_OK
    assert payload["status"] == "no_log"
    assert payload["dry_run"] is True
