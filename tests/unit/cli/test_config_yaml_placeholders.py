"""A literal ``${VAR}`` in config.yaml reaches the operator as a named key.

The operator path end to end: a config.yaml whose ``knowledge:`` block
carries a password written as a placeholder with a default, and
``trellis admin health``, which builds the registry from that file. Before
the refusal existed this exited ``0``.

Both ``--format`` arms are asserted. The JSON arm is the one a sanitizer
guards, and a message rendered as ``password: <value>`` would trip its
secret-assignment heuristic and be replaced wholesale by the suppression
marker, key name and all. Paths handed to the CLI are relative, so the
message carries no long ``--basetemp`` component for the sanitizer's
long-token heuristic to trip on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis_cli.exit_codes import EXIT_STORE
from trellis_cli.main import app

runner = CliRunner()

#: The value, written out, so a redefined ``EXIT_STORE`` cannot pass.
_STORE = 5

_CONFIG = """\
knowledge:
  graph:
    backend: neo4j
    uri: bolt://example.invalid:7687
    password: ${TRELLIS_NEO4J_PASSWORD:-SENTINEL123}
"""

_NAMED = "knowledge.graph.password is the literal text ${TRELLIS_NEO4J_PASSWORD}"


@pytest.fixture
def _placeholder_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", "config")
    monkeypatch.setenv("TRELLIS_DATA_DIR", "data")
    Path("config").mkdir()
    (Path("config") / "config.yaml").write_text(_CONFIG, encoding="utf-8")


@pytest.mark.usefixtures("_placeholder_config")
@pytest.mark.parametrize("arm", [[], ["--format", "json"]], ids=["text", "json"])
def test_a_placeholder_exits_store_and_names_the_key(arm: list[str]) -> None:
    result = runner.invoke(app, ["admin", "health", *arm])

    assert result.exit_code == _STORE
    assert result.exit_code == EXIT_STORE
    assert "SENTINEL123" not in result.stdout
    assert "SENTINEL123" not in result.stderr
    if arm:
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert payload["error_code"] == "CONFIG_ERROR"
        assert payload["setting"] == "knowledge.graph.password"
        assert _NAMED in payload["message"]
    else:
        assert _NAMED in plain(result.output)
