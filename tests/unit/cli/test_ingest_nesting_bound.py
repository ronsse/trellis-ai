"""``trellis ingest trace`` refuses a trace nested past the bound (#1186).

The exit code must not depend on ``--format``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.stores.base.trace import MAX_TRACE_PAYLOAD_DEPTH
from trellis_cli.exit_codes import EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry, get_trace_store

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()


def _write(tmp_path: Path, levels: int) -> Path:
    result: dict[str, Any] = {"leaf": 1}
    for _ in range(levels - 1):
        result = {"k": result}
    path = tmp_path / "trace.json"
    path.write_text(
        json.dumps(
            {
                "source": "agent",
                "intent": "probe",
                "context": {"agent_id": "a"},
                "steps": [{"step_type": "t", "name": "n", "result": result}],
            }
        )
    )
    return path


def test_a_trace_at_the_bound_is_stored_and_listed(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["ingest", "trace", str(_write(tmp_path, MAX_TRACE_PAYLOAD_DEPTH))],
    )

    assert result.exit_code == 0, result.stdout
    assert len(get_trace_store().query()) == 1


def test_a_trace_past_the_bound_exits_the_same_in_both_formats(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, 200)

    as_text = runner.invoke(app, ["ingest", "trace", str(path)])
    as_json = runner.invoke(app, ["ingest", "trace", str(path), "--format", "json"])

    assert as_text.exit_code == EXIT_VALIDATION
    assert as_json.exit_code == EXIT_VALIDATION
    assert f"more than {MAX_TRACE_PAYLOAD_DEPTH} levels" in plain(as_text.stdout)
    payload = json.loads(as_json.stdout.strip())
    assert payload["status"] == "error"
    assert f"more than {MAX_TRACE_PAYLOAD_DEPTH} levels" in payload["message"]
    assert get_trace_store().count() == 0
