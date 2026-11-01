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
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import feedback_log_dir, record_feedback
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, EXIT_VALIDATION
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
    return as_json.exit_code, json.loads(as_json.stdout), plain(as_text.output)


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


def test_no_log_message_never_claims_nothing_was_recorded(data_dir: Path) -> None:
    _, payload, text = _run()

    assert payload["status"] == "no_log"
    assert "Nothing has been recorded" not in payload["message"]
    assert "Nothing has been recorded" not in " ".join(text.split())
    assert "legacy" not in payload["message"]


def test_a_legacy_log_in_the_data_dir_is_named_not_read_by_default(
    data_dir: Path,
) -> None:
    legacy = data_dir / "pack_feedback.jsonl"
    row = PackFeedback(
        run_id="run-1", phase="p", intent="i", outcome="success", items_served=["a"]
    )
    record_feedback(row, log_dir=data_dir)
    assert legacy.is_file()

    exit_code, payload, text = _run()

    assert exit_code == EXIT_OK
    assert payload["status"] == "no_log"  # the default directory still holds none
    assert payload["scanned"] == 0
    assert "Nothing has been recorded" not in payload["message"]
    assert str(legacy) in payload["message"]
    assert f"--log-dir {data_dir}" in payload["message"]
    assert f"--log-dir {data_dir}" in " ".join(text.split())

    # The flag the message names does read it.
    exit_code, payload, _ = _run("--log-dir", str(data_dir), "--dry-run")
    assert exit_code == EXIT_OK
    assert (payload["status"], payload["scanned"]) == ("ok", 1)


def _log_path(data_dir: Path) -> Path:
    directory = feedback_log_dir(data_dir / "stores")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "pack_feedback.jsonl"


def _dangling_symlink(log: Path) -> None:
    log.symlink_to(log.parent / "gone.jsonl")


def _symlink_loop(log: Path) -> None:
    other = log.parent / "other.jsonl"
    log.symlink_to(other)
    other.symlink_to(log)


def _directory(log: Path) -> None:
    log.mkdir()


def _no_permission(log: Path) -> None:
    log.write_text("{}\n")
    log.chmod(0)


@pytest.mark.parametrize(
    "make_unreadable",
    [
        pytest.param(_dangling_symlink, id="dangling-symlink"),
        pytest.param(_symlink_loop, id="symlink-loop"),
        pytest.param(_directory, id="directory"),
        pytest.param(
            _no_permission,
            id="chmod-000",
            marks=pytest.mark.skipif(
                hasattr(os, "geteuid") and os.geteuid() == 0,
                reason="root reads a mode-000 file",
            ),
        ),
    ],
)
@pytest.mark.parametrize("extra", [(), ("--dry-run",)], ids=["real", "dry-run"])
def test_a_log_that_is_there_but_unreadable_is_exit_5_not_ok(
    data_dir: Path, make_unreadable: Callable[[Path], None], extra: tuple[str, ...]
) -> None:
    log = _log_path(data_dir)
    make_unreadable(log)

    exit_code, payload, text = _run(*extra)

    assert exit_code == EXIT_STORE
    assert payload["status"] == "unreadable"
    assert payload["log_path"] == str(log)
    assert payload["scanned"] == 0
    assert "cannot be read" in payload["message"]
    assert "Unreadable log" in text
    assert "Traceback" not in text


def test_an_unreadable_log_under_an_explicit_dir_is_exit_5_too(
    data_dir: Path,
) -> None:
    log = _log_path(data_dir)
    _dangling_symlink(log)

    exit_code, payload, _ = _run("--log-dir", str(log.parent))

    assert exit_code == EXIT_STORE
    assert payload["status"] == "unreadable"
