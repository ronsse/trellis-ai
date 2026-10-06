"""Tests for ``trellis extract traces`` backfill CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import pytest
import typer
from typer.testing import CliRunner, Result

from tests.cli_output import plain
from trellis.core.error_sanitize import SUPPRESSED_MARKER, sanitized_error_payload
from trellis.errors import StoreError
from trellis.mutate.executor import MutationExecutor
from trellis_cli import extract_refresh as extract_cli
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_POLICY, EXIT_STORE
from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def _ingest(trace: dict) -> str:
    """Ingest a trace via the CLI (flag off, so no graph yet) and return its id."""
    payload = runner.invoke(
        app, ["ingest", "trace", "-", "--format", "json"], input=json.dumps(trace)
    )
    assert payload.exit_code == 0, payload.stdout
    return json.loads(payload.stdout.strip())["trace_id"]


_TRACE_A: dict = {
    "source": "agent",
    "intent": "fix the import",
    "steps": [{"step_type": "tool_call", "name": "grep"}],
    "context": {"agent_id": "a1", "domain": "backend"},
}

_TRACE_B: dict = {
    "source": "agent",
    "intent": "deploy the service",
    "steps": [{"step_type": "tool_call", "name": "deploy"}],
    "context": {"agent_id": "a2", "domain": "platform"},
}


class TestBackfill:
    def test_dry_run_reports_drafts_without_executing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Ingest with the flag OFF so the graph starts empty.
        monkeypatch.delenv("TRELLIS_ENABLE_TRACE_EXTRACTION", raising=False)
        _ingest(_TRACE_A)
        _ingest(_TRACE_B)

        result = runner.invoke(
            app, ["extract", "traces", "--dry-run", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "backfilled"
        assert data["dry_run"] is True
        assert data["traces_scanned"] == 2
        assert data["total_entities"] > 0
        assert len(data["per_trace"]) == 2

        # Dry-run executed nothing — graph still empty.
        from trellis_cli.stores import get_graph_store

        assert get_graph_store().count_nodes() == 0

    def test_executes_and_populates_graph(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRELLIS_ENABLE_TRACE_EXTRACTION", raising=False)
        trace_id = _ingest(_TRACE_A)

        result = runner.invoke(app, ["extract", "traces", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "backfilled"
        assert data["dry_run"] is False
        assert data["total_entities"] > 0

        from trellis_cli.stores import get_graph_store

        graph = get_graph_store()
        assert graph.count_nodes() > 0
        assert graph.get_node(f"trace:{trace_id}") is not None

    def test_domain_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRELLIS_ENABLE_TRACE_EXTRACTION", raising=False)
        _ingest(_TRACE_A)  # backend
        _ingest(_TRACE_B)  # platform

        result = runner.invoke(
            app,
            ["extract", "traces", "--domain", "platform", "--format", "json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["traces_scanned"] == 1
        assert data["per_trace"][0]["domain"] == "platform"

    def test_text_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRELLIS_ENABLE_TRACE_EXTRACTION", raising=False)
        _ingest(_TRACE_A)
        result = runner.invoke(app, ["extract", "traces"])
        assert result.exit_code == 0
        assert "backfill" in result.stdout.lower()

    def test_empty_store_scans_zero(self) -> None:
        result = runner.invoke(app, ["extract", "traces", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["traces_scanned"] == 0
        assert data["total_entities"] == 0

    def test_since_filter_excludes_old(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRELLIS_ENABLE_TRACE_EXTRACTION", raising=False)
        _ingest(_TRACE_A)
        # since=0 days -> window start is "now"; the just-ingested trace
        # falls outside (created_at < now), so nothing is scanned.
        result = runner.invoke(
            app, ["extract", "traces", "--since", "0", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["traces_scanned"] == 0


class _DriverError(Exception):
    """An exception outside the executor's handler tuple, as ``psycopg.Error`` is."""


#: A credentialed DSN, which the sanitizer suppresses whole, and a closing
#: tag Rich raises ``MarkupError`` on if the line prints it as markup.
_LOOP_FAILURE = "no route to postgres://ops:synthetic-secret@db [/x]"

#: The calls the per-trace loop makes, any of which can raise.
_LOOP_CALLS = pytest.mark.parametrize(
    "call", ["extract_trace_batch", "reconcile_node_roles", "execute_batch"]
)

_FORMATS = pytest.mark.parametrize("fmt", ["json", "text"])


def _raise_from(monkeypatch: pytest.MonkeyPatch, call: str, exc: BaseException) -> None:
    """Make the loop's *call* raise *exc*."""

    def _raise(*_args: object, **_kwargs: object) -> NoReturn:
        raise exc

    owner = MutationExecutor if call == "execute_batch" else extract_cli
    monkeypatch.setattr(owner, call, _raise)


def _backfill(fmt: str) -> Result:
    """Run the backfill, adding ``--format json`` when *fmt* asks for it."""
    args = ["extract", "traces"]
    return runner.invoke(app, [*args, "--format", "json"] if fmt == "json" else args)


class TestLoopFailure:
    """A failure inside the per-trace loop is reported, not a traceback.

    The trace query had a catch-all and the loop after it had none, so an
    exception the executor does not turn into a result left the CLI as a
    traceback with exit ``1`` and nothing on stdout: a ``--format json``
    caller had no JSON to parse. The loop now reports it as ``extract
    refresh`` reports its run. ``CliRunner`` also exits ``1`` for an
    uncaught exception, so each test asserts the ``SystemExit`` too.
    """

    @_LOOP_CALLS
    def test_json_prints_the_sanitized_payload(
        self, monkeypatch: pytest.MonkeyPatch, call: str
    ) -> None:
        _ingest(_TRACE_A)
        exc = _DriverError(_LOOP_FAILURE)
        _raise_from(monkeypatch, call, exc)
        result = _backfill("json")
        assert result.exit_code == EXIT_INTERNAL, result.output
        assert isinstance(result.exception, SystemExit), repr(result.exception)
        payload = json.loads(result.stdout)
        assert payload == sanitized_error_payload(exc)
        assert payload["message"] == SUPPRESSED_MARKER
        assert "synthetic-secret" not in result.output

    @_LOOP_CALLS
    def test_text_prints_one_line(
        self, monkeypatch: pytest.MonkeyPatch, call: str
    ) -> None:
        _ingest(_TRACE_A)
        _raise_from(monkeypatch, call, _DriverError(_LOOP_FAILURE))
        result = _backfill("text")
        assert result.exit_code == EXIT_INTERNAL, result.output
        assert isinstance(result.exception, SystemExit), repr(result.exception)
        assert plain(result.output).splitlines() == [
            f"Trace backfill failed: {_LOOP_FAILURE}"
        ]

    @_FORMATS
    def test_a_trellis_error_still_exits_by_its_type(
        self, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        """A typed failure passes the catch to the root boundary, as before it.

        The boundary exits it through ``exit_code_for``, ``5`` for a
        ``StoreError`` where the catch-all would answer ``1``. The graph
        read in ``reconcile_node_roles`` is where a backend's mapped
        ``StoreError`` would come from.
        """
        _ingest(_TRACE_A)
        _raise_from(monkeypatch, "reconcile_node_roles", StoreError("graph down"))
        result = _backfill(fmt)
        assert result.exit_code == EXIT_STORE, result.output
        assert isinstance(result.exception, SystemExit), repr(result.exception)
        if fmt == "json":
            assert json.loads(result.stdout)["error_code"] == "STORE_ERROR"
        else:
            assert plain(result.output).startswith("STORE_ERROR")

    @_FORMATS
    def test_an_exit_raised_in_the_loop_keeps_its_code(
        self, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        """``typer.Exit`` is a ``RuntimeError``; the catch must pass it on.

        Nothing in the loop raises one today. ``extract refresh`` re-raises
        it because its run reaches ``_get_registry()``, which does, and the
        loop's catch keeps the same rule so an exit is never reported as a
        failure and turned into a ``1``.
        """
        _ingest(_TRACE_A)
        _raise_from(monkeypatch, "execute_batch", typer.Exit(code=EXIT_POLICY))
        result = _backfill(fmt)
        assert result.exit_code == EXIT_POLICY, result.output
        assert result.output == ""
