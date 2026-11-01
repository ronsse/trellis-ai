"""A refused command under ``--format json`` still answers in JSON.

``trellis_cli`` promises machine output "whatever happens": a caller that
asked for ``--format json`` parses stdout, so a refusal has to arrive as one
``{"status": "error", "message": ...}`` object, not as a Rich line. The exit
code is the other half of the promise and must not depend on the format
(``tests/unit/test_format_exit_parity_rule.py``), so every case here runs
both formats and asserts the same exit and the same message.

The cases are the failure branches that printed prose on both formats:
``extract refresh``, ``classify shadow``, ``analyze replay`` and the two
``worker`` helpers (the LLM-client guard and the auto-promote config guard).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.errors import BackendNotInstalledError
from trellis_cli import classify as classify_cli
from trellis_cli import worker as worker_cli
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.output import active_output_format
from trellis_cli.stores import _reset_registry

runner = CliRunner()

# (arguments without --format, a phrase both formats must carry, exit code)
Setup = Callable[[Path, pytest.MonkeyPatch], tuple[list[str], str, int]]


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    # A phrase is matched after whitespace is folded, but keep paths whole.
    monkeypatch.setenv("COLUMNS", "500")
    _reset_registry()


def _sources_file(tmp_path: Path, body: str) -> str:
    path = tmp_path / "sources.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _llm_unavailable(
    monkeypatch: pytest.MonkeyPatch, module: Any, *, sdk_missing: bool
) -> None:
    registry = MagicMock()
    if sdk_missing:
        registry.build_llm_client.side_effect = BackendNotInstalledError(
            backend_name="openai", extra="llm-openai"
        )
    else:
        registry.build_llm_client.return_value = None
    monkeypatch.setattr(module, "_get_registry", lambda: registry)


def _write_config(tmp_path: Path, body: str) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(body, encoding="utf-8")


def _extract_neither(tmp_path, monkeypatch):
    return ["extract", "refresh"], "exactly one of --source or --type", EXIT_INTERNAL


def _extract_type_without_path(tmp_path, monkeypatch):
    args = ["extract", "refresh", "--type", "dbt-manifest"]
    return args, "--type requires --path", EXIT_INTERNAL


def _extract_sources_file_missing(tmp_path, monkeypatch):
    args = ["extract", "refresh", "--source", "x"]
    args += ["--sources-file", str(tmp_path / "absent.yaml")]
    return args, "sources.yaml not found", EXIT_INTERNAL


def _extract_source_not_declared(tmp_path, monkeypatch):
    sources = _sources_file(
        tmp_path,
        "sources:\n  - name: other\n    type: dbt-manifest\n    path: m.json\n",
    )
    args = ["extract", "refresh", "--source", "ghost", "--sources-file", sources]
    return args, "not declared in", EXIT_INTERNAL


def _extract_source_disabled(tmp_path, monkeypatch):
    sources = _sources_file(
        tmp_path,
        "sources:\n  - name: retired\n    type: dbt-manifest\n    path: m.json\n"
        "    enabled: false\n",
    )
    args = ["extract", "refresh", "--source", "retired", "--sources-file", sources]
    return args, "is disabled in", EXIT_INTERNAL


def _extract_source_bad_parameter(tmp_path, monkeypatch):
    sources = _sources_file(
        tmp_path,
        "sources:\n  - name: streaming\n    type: openlineage\n"
        "    endpoint: https://example.com/api\n",
    )
    args = ["extract", "refresh", "--source", "streaming", "--sources-file", sources]
    return args, "uses 'endpoint', not 'path'", EXIT_INTERNAL


def _extract_type_path_missing(tmp_path, monkeypatch):
    args = ["extract", "refresh", "--type", "dbt-manifest"]
    args += ["--path", str(tmp_path / "absent.json")]
    return args, "Path not found", EXIT_INTERNAL


def _extract_type_bad_parameter(tmp_path, monkeypatch):
    manifest = tmp_path / "input.json"
    manifest.write_text("{}", encoding="utf-8")
    args = ["extract", "refresh", "--type", "no-such-type", "--path", str(manifest)]
    return args, "No extractor registered for type", EXIT_INTERNAL


def _classify_no_client(tmp_path, monkeypatch):
    _llm_unavailable(monkeypatch, classify_cli, sdk_missing=False)
    phrase = "classify shadow requires an LLM client but none is configured"
    return ["classify", "shadow"], phrase, EXIT_INTERNAL


def _classify_sdk_missing(tmp_path, monkeypatch):
    _llm_unavailable(monkeypatch, classify_cli, sdk_missing=True)
    phrase = "classify shadow requires an LLM SDK that is not installed"
    return ["classify", "shadow"], phrase, EXIT_INTERNAL


def _replay_graph_selection_conflict(tmp_path, monkeypatch):
    args = ["analyze", "replay", "--graph-selection", "seeded", "--max-items", "5"]
    return (
        args,
        "--graph-selection cannot be combined with --max-items",
        EXIT_VALIDATION,
    )


def _replay_graph_selection_unknown(tmp_path, monkeypatch):
    args = ["analyze", "replay", "--graph-selection", "no-such-arm"]
    return args, "unknown graph_selection", EXIT_VALIDATION


def _replay_bad_policy(tmp_path, monkeypatch):
    args = ["analyze", "replay", "--body-items", "0"]
    return args, "body_items must be >= 1", EXIT_VALIDATION


def _worker_enrich_no_client(tmp_path, monkeypatch):
    _llm_unavailable(monkeypatch, worker_cli, sdk_missing=False)
    phrase = "worker enrich requires an LLM client but none is configured"
    return ["worker", "enrich"], phrase, EXIT_INTERNAL


def _worker_enrich_sdk_missing(tmp_path, monkeypatch):
    _llm_unavailable(monkeypatch, worker_cli, sdk_missing=True)
    phrase = "worker enrich requires an LLM SDK that is not installed"
    return ["worker", "enrich"], phrase, EXIT_INTERNAL


def _worker_mine_no_client(tmp_path, monkeypatch):
    _llm_unavailable(monkeypatch, worker_cli, sdk_missing=False)
    phrase = "worker mine-precedents requires an LLM client but none is configured"
    return ["worker", "mine-precedents"], phrase, EXIT_INTERNAL


def _worker_tune_bad_parameter(tmp_path, monkeypatch):
    _write_config(
        tmp_path, "learning:\n  auto_promote:\n    enabled: true\n    bogus: 1\n"
    )
    return ["worker", "tune"], "unknown key", EXIT_INTERNAL


def _worker_tune_looser_than_manual(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        "learning:\n  auto_promote:\n    enabled: true\n    min_sample_size: 2\n",
    )
    return (
        ["worker", "tune"],
        "invalid learning.auto_promote config (ValueError)",
        EXIT_INTERNAL,
    )


CASES: dict[str, Setup] = {
    "extract-refresh-neither-source-nor-type": _extract_neither,
    "extract-refresh-type-without-path": _extract_type_without_path,
    "extract-refresh-sources-file-missing": _extract_sources_file_missing,
    "extract-refresh-source-not-declared": _extract_source_not_declared,
    "extract-refresh-source-disabled": _extract_source_disabled,
    "extract-refresh-source-refused": _extract_source_bad_parameter,
    "extract-refresh-type-path-missing": _extract_type_path_missing,
    "extract-refresh-type-unregistered": _extract_type_bad_parameter,
    "classify-shadow-no-llm-client": _classify_no_client,
    "classify-shadow-sdk-missing": _classify_sdk_missing,
    "analyze-replay-graph-selection-conflict": _replay_graph_selection_conflict,
    "analyze-replay-graph-selection-unknown": _replay_graph_selection_unknown,
    "analyze-replay-bad-policy": _replay_bad_policy,
    "worker-enrich-no-llm-client": _worker_enrich_no_client,
    "worker-enrich-sdk-missing": _worker_enrich_sdk_missing,
    "worker-mine-precedents-no-llm-client": _worker_mine_no_client,
    "worker-tune-unknown-config-key": _worker_tune_bad_parameter,
    "worker-tune-looser-than-manual": _worker_tune_looser_than_manual,
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_refusal_is_one_json_object_and_exits_as_text_does(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, phrase, code = CASES[case](tmp_path, monkeypatch)

    text = runner.invoke(app, [*args, "--format", "text"])
    machine = runner.invoke(app, [*args, "--format", "json"])

    assert text.exit_code == code, text.output
    assert machine.exit_code == code, machine.output
    assert phrase in " ".join(plain(text.stdout).split())
    # ``json.loads`` of the whole stream: any Rich line beside the object fails.
    payload = json.loads(machine.stdout)
    assert isinstance(payload, dict), machine.stdout
    assert payload["status"] == "error"
    assert phrase in payload["message"]


def test_the_active_format_is_text_outside_a_command() -> None:
    """A helper called directly, as the unit tests do, keeps its Rich output."""
    assert active_output_format() == "text"


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_the_active_format_is_the_running_commands_format(fmt: str) -> None:
    probe = typer.Typer()

    @probe.command()
    def run(output_format: str = typer.Option("text", "--format")) -> None:
        typer.echo(active_output_format())

    @probe.command()
    def other() -> None:  # a second command keeps ``run`` a subcommand
        pass

    result = runner.invoke(probe, ["run", "--format", fmt])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == fmt
