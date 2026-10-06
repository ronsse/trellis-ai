"""A catch-all failure line prints as one line, however long its message.

Each site is an ``except Exception`` arm that prints ``<what failed>:
<message>`` through a module-level Rich console in text mode. Rich
hard-wraps a line at the console width unless it is printed with
``soft_wrap``, and the width is 80 columns when no standard stream is a
terminal, so without it a caller reading one line gets part of a long
message.

Each case makes one call inside the site's ``try`` raise a synthetic
message, patching the call the site's existing failure test patches where
one reaches the arm. The read sites' tests reach it with a real unreadable
file, whose OS message depends on the temp path, so those cases patch
``Path.read_text`` for the one input file. ``extract refresh``, ``extract
traces``' query and ``admin migrate-provenance`` had no test that reaches
the arm, so their cases patch a call inside it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unit.cli.test_admin_proposals import _init_stores
from tests.unit.cli.test_extract_refresh import _SAMPLE_MANIFEST_V1
from tests.unit.cli.test_extract_traces import (
    _TRACE_A,
    _DriverError,
    _ingest,
    _raise_from,
)
from tests.unit.cli.test_ingest import _batch_input
from trellis.stores.sqlite.trace import SQLiteTraceStore
from trellis_cli import admin_migrate_provenance as migrate_cli
from trellis_cli import ingest as ingest_cli
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.main import app

runner = CliRunner()

#: Wider than 80 columns with or without a site's prefix, and carrying a
#: tag Rich would act on if a line printed the message as markup.
_LONG = (
    "synthetic driver failure: the backend closed the connection while the "
    "batch was being written [bold]b[/x] so nothing after it was stored"
)

Arrange = Callable[[Path, pytest.MonkeyPatch], list[str]]


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def _raise(*_args: object, **_kwargs: object) -> NoReturn:
    raise _DriverError(_LONG)


def _manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_SAMPLE_MANIFEST_V1))
    return manifest


def _refresh_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    _raise_from(monkeypatch, "_run_refresh", _DriverError(_LONG))
    path = str(_manifest(tmp_path))
    return ["extract", "refresh", "--type", "dbt-manifest", "--path", path]


def _refresh_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    _raise_from(monkeypatch, "_run_refresh", _DriverError(_LONG))
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        "sources:\n"
        "  - name: jaffle\n"
        "    type: dbt-manifest\n"
        f"    path: {_manifest(tmp_path)}\n"
    )
    return ["extract", "refresh", "--source", "jaffle", "--sources-file", str(sources)]


def _traces_query(_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(SQLiteTraceStore, "query", _raise)
    return ["extract", "traces"]


def _traces_loop(_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    _ingest(_TRACE_A)
    _raise_from(monkeypatch, "execute_batch", _DriverError(_LONG))
    return ["extract", "traces"]


def _unreadable(command: str) -> Arrange:
    """Reading the input file raises, as an I/O error on a present file does."""

    def arrange(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        target = tmp_path / "in.json"
        target.write_text("{}")
        read_text = Path.read_text

        def _read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == target:
                _raise()
            return read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _read_text)
        return ["ingest", command, str(target)]

    return arrange


def _batch(command: str) -> Arrange:
    def arrange(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        monkeypatch.setattr(ingest_cli, "_run_extraction", _raise)
        return ["ingest", command, _batch_input(tmp_path, command)]

    return arrange


def _conversations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr("trellis.ingest_corpus.sync_conversations", _raise)
    export = tmp_path / "conversations.json"
    export.write_text("[]")
    return ["ingest", "conversations", str(export)]


def _corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr("trellis.ingest_corpus.sync_corpus", _raise)
    vault = tmp_path / "vault"
    vault.mkdir()
    return ["ingest", "corpus", str(vault)]


def _migrate_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    _init_stores(tmp_path, monkeypatch)
    monkeypatch.setattr(migrate_cli, "run_migrate_provenance", _raise)
    return ["admin", "migrate-provenance"]


def _proposals(*command: str) -> Arrange:
    def arrange(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        _init_stores(tmp_path, monkeypatch)
        monkeypatch.setattr("trellis_cli.admin_proposals.get_event_log", _raise)
        return ["admin", *command]

    return arrange


_STORE_ERROR = f"store error: {_DriverError.__name__}: "

_SITES = [
    pytest.param(
        _refresh_source, "Refresh failed: ", EXIT_INTERNAL, id="refresh-source"
    ),
    pytest.param(_refresh_type, "Refresh failed: ", EXIT_INTERNAL, id="refresh-type"),
    pytest.param(
        _traces_query, "Trace query failed: ", EXIT_INTERNAL, id="traces-query"
    ),
    pytest.param(
        _traces_loop, "Trace backfill failed: ", EXIT_INTERNAL, id="traces-loop"
    ),
    pytest.param(_unreadable("trace"), "Invalid trace: ", EXIT_VALIDATION, id="trace"),
    pytest.param(
        _unreadable("evidence"), "Invalid evidence: ", EXIT_VALIDATION, id="evidence"
    ),
    pytest.param(
        _unreadable("dbt-manifest"),
        "Could not read manifest: ",
        EXIT_VALIDATION,
        id="dbt-read",
    ),
    pytest.param(
        _batch("dbt-manifest"), "dbt ingest failed: ", EXIT_INTERNAL, id="dbt"
    ),
    pytest.param(
        _unreadable("openlineage"),
        "Could not read events file: ",
        EXIT_VALIDATION,
        id="openlineage-read",
    ),
    pytest.param(
        _batch("openlineage"),
        "OpenLineage ingest failed: ",
        EXIT_INTERNAL,
        id="openlineage",
    ),
    pytest.param(
        _conversations,
        "Conversation ingest failed: ",
        EXIT_INTERNAL,
        id="conversations",
    ),
    pytest.param(_corpus, "Corpus ingest failed: ", EXIT_INTERNAL, id="corpus"),
    pytest.param(
        _migrate_provenance, _STORE_ERROR, EXIT_STORE, id="migrate-provenance"
    ),
    pytest.param(
        _proposals("generate-proposals"),
        _STORE_ERROR,
        EXIT_STORE,
        id="generate-proposals",
    ),
    pytest.param(
        _proposals("list-proposals"), _STORE_ERROR, EXIT_STORE, id="list-proposals"
    ),
    pytest.param(
        _proposals("show-proposal", "p-1"),
        _STORE_ERROR,
        EXIT_STORE,
        id="show-proposal",
    ),
]


@pytest.mark.parametrize(("arrange", "prefix", "exit_code"), _SITES)
def test_a_long_failure_message_prints_as_one_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arrange: Arrange,
    prefix: str,
    exit_code: int,
) -> None:
    # Rich's width when no standard stream is a terminal, pinned so a run
    # under ``-s`` from a wide terminal still measures the line against it.
    monkeypatch.setenv("COLUMNS", "80")
    result = runner.invoke(app, arrange(tmp_path, monkeypatch))
    assert result.exit_code == exit_code, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert plain(result.output).splitlines() == [f"{prefix}{_LONG}"]
