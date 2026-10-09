"""Tests for ``trellis admin migrate-provenance``.

Lives under ``tests/unit/cli`` so it runs in the no-live-infra
fast suite.  Exercises the programmatic entry point
:func:`run_migrate_provenance` directly against an in-memory SQLite
store — no subprocess, no Typer wrapping.  A separate CliRunner
test asserts the command registers and exit codes route correctly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from tests.cli_output import assert_coloured, force_colour, plain
from trellis.core.error_sanitize import SUPPRESSED_MARKER
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.graph import SQLiteGraphStore
from trellis_cli import admin_migrate_provenance as migrate_provenance_module
from trellis_cli.admin import admin_app
from trellis_cli.admin_migrate_provenance import (
    MigrateProvenanceReport,
    MigrationDriftError,
    _print_text_report,
    migrate_provenance_command,
    run_migrate_provenance,
)
from trellis_cli.exit_codes import EXIT_STORE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path) -> SQLiteGraphStore:
    store = SQLiteGraphStore(tmp_path / "graph.db")
    store.upsert_node("a", "service", {})
    store.upsert_node("b", "service", {})
    return store


def _seed_legacy_edge(
    store: SQLiteGraphStore,
    edge_type: str,
    properties: dict[str, Any],
) -> str:
    """Insert an edge with provenance keys ONLY in the legacy JSON blob.

    Uses ``upsert_edge`` without the keyword provenance args so the
    typed columns stay NULL — this is the exact shape of a row
    written before Phase 1 of Item 2 landed.
    """
    return store.upsert_edge("a", "b", edge_type, properties=properties)


# ---------------------------------------------------------------------------
# run_migrate_provenance — programmatic entry point
# ---------------------------------------------------------------------------


class TestRunMigrateProvenance:
    def test_dry_run_reports_without_writing(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        try:
            _seed_legacy_edge(
                store,
                "depends_on",
                {
                    "source_trace_id": "t1",
                    "agent_id": "agent-1",
                    "confidence": 0.7,
                    "extractor_tier": "DETERMINISTIC",
                },
            )

            report = run_migrate_provenance(store, dry_run=True, batch_size=10)

            assert report.edges_scanned == 1
            assert report.edges_migrated == 1
            assert report.dry_run is True

            # Typed columns must still be NULL — dry-run did not write.
            edge = store.get_edges("a", direction="outgoing")[0]
            assert edge["source_trace_id"] is None
            assert edge["confidence"] is None
        finally:
            store.close()

    def test_basic_migration_lifts_legacy_keys(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        try:
            _seed_legacy_edge(
                store,
                "depends_on",
                {
                    "source_trace_id": "t1",
                    "agent_id": "agent-x",
                    "confidence": 0.6,
                    "evidence_ref": "doc-9",
                    "extractor_tier": "HYBRID",
                    "unrelated_key": "kept",
                },
            )

            report = run_migrate_provenance(store, dry_run=False, batch_size=10)

            assert report.edges_migrated == 1
            assert report.edges_malformed == 0

            edge = store.get_edges("a", direction="outgoing")[0]
            assert edge["source_trace_id"] == "t1"
            assert edge["agent_id"] == "agent-x"
            assert float(edge["confidence"]) == pytest.approx(0.6)
            assert edge["evidence_ref"] == "doc-9"
            assert edge["extractor_tier"] == "HYBRID"
            # Unrelated property keys must survive.
            assert edge["properties"].get("unrelated_key") == "kept"
            # Migrated keys are stripped from the legacy blob — the
            # typed columns are now the source of truth.
            assert "source_trace_id" not in edge["properties"]
            assert "confidence" not in edge["properties"]
        finally:
            store.close()

    def test_idempotent_rerun(self, tmp_path: Path) -> None:
        """Running the migration twice in succession is a no-op the second time."""
        store = _make_store(tmp_path)
        try:
            _seed_legacy_edge(
                store,
                "depends_on",
                {
                    "source_trace_id": "t1",
                    "confidence": 0.4,
                    "extractor_tier": "DETERMINISTIC",
                },
            )

            first = run_migrate_provenance(store, dry_run=False, batch_size=10)
            assert first.edges_migrated == 1

            second = run_migrate_provenance(store, dry_run=False, batch_size=10)
            assert second.edges_migrated == 0
            # The edge now has typed columns set, so the scan classifies
            # it as already-migrated rather than a no-legacy candidate.
            assert second.edges_already_migrated == 1
        finally:
            store.close()

    def test_edges_without_legacy_keys_classified_separately(
        self, tmp_path: Path
    ) -> None:
        store = _make_store(tmp_path)
        try:
            # All-NULL typed columns AND empty properties — pure no-op.
            store.upsert_edge("a", "b", "depends_on")

            report = run_migrate_provenance(store, dry_run=False, batch_size=10)

            assert report.edges_scanned == 1
            assert report.edges_migrated == 0
            assert report.edges_no_legacy_provenance == 1
        finally:
            store.close()

    def test_malformed_legacy_value_skips_and_emits_event(self, tmp_path: Path) -> None:
        """Malformed legacy values emit ``EXTRACTION_FAILED`` and skip the row."""
        import os

        store = _make_store(tmp_path)
        # Disable sampling so the event lands deterministically.
        os.environ["EXTRACTION_FAILURE_NO_SAMPLE"] = "1"
        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            # 1 malformed against 200 good edges — well below 1% drift.
            _seed_legacy_edge(
                store,
                "bad_edge",
                {"confidence": "high"},  # string, not a float
            )
            for i in range(200):
                store.upsert_node(f"n{i}", "service", {})
                store.upsert_edge(
                    "a",
                    f"n{i}",
                    f"good_{i}",
                    properties={"confidence": 0.5},
                )

            report = run_migrate_provenance(
                store,
                dry_run=False,
                batch_size=50,
                event_log=event_log,
            )

            assert report.edges_malformed == 1

            events = event_log.get_events(
                event_type=EventType.EXTRACTION_FAILED, limit=10
            )
            assert any(
                (e.payload or {}).get("failure_kind") == "parse_error" for e in events
            )
        finally:
            os.environ.pop("EXTRACTION_FAILURE_NO_SAMPLE", None)
            event_log.close()
            store.close()

    def test_drift_threshold_raises(self, tmp_path: Path) -> None:
        """Above 1% malformed legacy edges, the run raises MigrationDriftError."""
        store = _make_store(tmp_path)
        try:
            # 5 malformed, 50 total scanned → 10% well above the 1% gate.
            for i in range(50):
                _seed_legacy_edge(
                    store,
                    f"edge_{i}",
                    {"confidence": "high" if i < 5 else 0.5},
                )

            with pytest.raises(MigrationDriftError) as exc:
                run_migrate_provenance(store, dry_run=False, batch_size=10)
            assert exc.value.malformed_count == 5
            assert exc.value.scanned == 50
        finally:
            store.close()

    def test_already_migrated_edges_pass_through(self, tmp_path: Path) -> None:
        """Edges with any typed provenance set are SKIPPED, not overwritten."""
        store = _make_store(tmp_path)
        try:
            # Edge written with typed columns AND a stale legacy
            # ``confidence`` in properties.  Migration must not
            # overwrite — the typed column wins.
            store.upsert_edge(
                "a",
                "b",
                "depends_on",
                properties={"confidence": 0.1},
                confidence=0.9,
            )
            report = run_migrate_provenance(store, dry_run=False, batch_size=10)
            assert report.edges_migrated == 0
            assert report.edges_already_migrated == 1
            edge = store.get_edges("a", direction="outgoing")[0]
            assert float(edge["confidence"]) == pytest.approx(0.9)
        finally:
            store.close()


# ---------------------------------------------------------------------------
# CLI surface — register + exit codes
# ---------------------------------------------------------------------------


runner = CliRunner()


# ---------------------------------------------------------------------------
# ``_print_text_report``'s per-edge error line, verbatim and unwrapped.
# Each ``report.errors`` entry carries a store exception's text, so Rich
# must neither drop a ``[...]`` nor turn a ``:name:`` into an emoji, nor
# hard-wrap a long token at the console width. ``migrate_provenance_command``
# raises a non-zero exit right after this line when ``report.errors`` is
# non-empty, so it is listed as a cross-function entry in
# ``tests/unit/test_cli_failure_soft_wrap_rule.py``.
# ---------------------------------------------------------------------------


#: Long enough to wrap at 80 columns without ``soft_wrap``.
_LONG_EDGE_ID = "edge-" + "a" * 90
_UPSERT_ERROR = (
    f"edge:{_LONG_EDGE_ID}: upsert failed: ValueError: [bold]x[/bold] [tag] :smile:"
)


class TestPrintTextReportErrorVerbatim:
    @pytest.mark.parametrize("colour", [False, True], ids=["plain", "colour"])
    def test_error_line_prints_verbatim_and_unwrapped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        colour: bool,
    ) -> None:
        monkeypatch.setenv("COLUMNS", "80")
        if colour:
            force_colour(monkeypatch, migrate_provenance_module)

        report = MigrateProvenanceReport(dry_run=False, errors=[_UPSERT_ERROR])
        _print_text_report(report)

        out = capsys.readouterr().out
        text = assert_coloured(out) if colour else plain(out)
        lines = [ln for ln in text.splitlines() if _LONG_EDGE_ID in ln]
        assert lines == [f"  {_UPSERT_ERROR}"], text


# ---------------------------------------------------------------------------
# A per-edge upsert failure (``report.errors`` non-empty) exits
# ``EXIT_STORE``, with the JSON payload's ``status`` distinguishing a
# partial run from a total one, and the failing exception's text passed
# through ``sanitize_error_message`` so a secret-shaped fragment never
# reaches stdout. At main this exits ``0`` with no ``status`` key.
# ---------------------------------------------------------------------------


#: Secret-shaped per the ``password=`` leak heuristic in
#: ``trellis.core.error_sanitize`` — must never appear in captured output.
_SYNTHETIC_SECRET = "password=sk-canary-do-not-leak-0042"  # noqa: S105


class _OneEdgeUpsertFailsStore:
    """Wraps a real :class:`SQLiteGraphStore`; ``upsert_edge`` raises for
    every edge type named in *fail_edge_types*, carrying *message* (by
    default a synthetic secret-shaped one). Everything else delegates
    straight through — this is a write-time failure validation cannot
    catch, since validation runs on the legacy values before the store is
    ever called.
    """

    def __init__(
        self,
        inner: SQLiteGraphStore,
        fail_edge_types: frozenset[str],
        *,
        message: str = f"write rejected: {_SYNTHETIC_SECRET}",
    ) -> None:
        self._inner = inner
        self._fail_edge_types = fail_edge_types
        self._message = message

    def query(self, **kwargs: Any) -> Any:
        return self._inner.query(**kwargs)

    def get_edges(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.get_edges(*args, **kwargs)

    def upsert_edge(
        self, source_id: str, target_id: str, edge_type: str, **kwargs: Any
    ) -> str:
        if edge_type in self._fail_edge_types:
            raise RuntimeError(self._message)
        return self._inner.upsert_edge(source_id, target_id, edge_type, **kwargs)

    def close(self) -> None:
        self._inner.close()


def _seed_two_legacy_edges(tmp_path: Path) -> SQLiteGraphStore:
    """One ``fail_edge`` (a->b) and one ``ok_edge`` (a->c), both carrying
    valid legacy provenance so both reach the upsert call."""
    store = _make_store(tmp_path)
    store.upsert_node("c", "service", {})
    _seed_legacy_edge(
        store, "fail_edge", {"confidence": 0.5, "extractor_tier": "DETERMINISTIC"}
    )
    store.upsert_edge(
        "a",
        "c",
        "ok_edge",
        properties={"confidence": 0.4, "extractor_tier": "DETERMINISTIC"},
    )
    return store


def _edge_id(store: SQLiteGraphStore, edge_type: str) -> str:
    (edge_id,) = [
        edge["edge_id"]
        for edge in store.get_edges("a", direction="outgoing")
        if edge["edge_type"] == edge_type
    ]
    return str(edge_id)


class TestExitCodeOnPerEdgeFailures:
    @pytest.mark.parametrize("output_format", ["text", "json"])
    def test_one_failing_edge_exits_store_with_status_partial(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        output_format: str,
    ) -> None:
        inner = _seed_two_legacy_edges(tmp_path)
        fail_edge_id = _edge_id(inner, "fail_edge")
        store = _OneEdgeUpsertFailsStore(inner, frozenset({"fail_edge"}))
        monkeypatch.setattr(migrate_provenance_module, "get_graph_store", lambda: store)
        monkeypatch.setattr(migrate_provenance_module, "get_event_log", lambda: None)

        try:
            with pytest.raises(typer.Exit) as exc:
                migrate_provenance_command(
                    dry_run=False,
                    batch_size=10,
                    output_format=output_format,
                    no_meta_trace=True,
                )
            assert exc.value.exit_code == EXIT_STORE
        finally:
            store.close()

        out = capsys.readouterr().out
        assert _SYNTHETIC_SECRET not in plain(out)
        # Only the exception's text is withheld: the edge and the
        # exception type still say which row failed and how.
        entry = f"edge:{fail_edge_id}: upsert failed: RuntimeError: {SUPPRESSED_MARKER}"

        if output_format == "json":
            payload = json.loads(out)
            assert payload["status"] == "partial"
            assert payload["edges_migrated"] == 1
            assert payload["errors"] == [entry]
        else:
            assert "errors (1)" in plain(out)
            assert entry in plain(out)

    def test_a_clean_store_message_reaches_the_report_verbatim(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The sanitizer withholds only leak-shaped text, so an ordinary
        driver message survives: the report carries the error, not just
        its type."""
        clean = "could not serialize access due to concurrent update"
        inner = _seed_two_legacy_edges(tmp_path)
        fail_edge_id = _edge_id(inner, "fail_edge")
        store = _OneEdgeUpsertFailsStore(inner, frozenset({"fail_edge"}), message=clean)
        monkeypatch.setattr(migrate_provenance_module, "get_graph_store", lambda: store)
        monkeypatch.setattr(migrate_provenance_module, "get_event_log", lambda: None)

        try:
            with pytest.raises(typer.Exit) as exc:
                migrate_provenance_command(
                    dry_run=False,
                    batch_size=10,
                    output_format="json",
                    no_meta_trace=True,
                )
            assert exc.value.exit_code == EXIT_STORE
        finally:
            store.close()

        payload = json.loads(capsys.readouterr().out)
        assert payload["errors"] == [
            f"edge:{fail_edge_id}: upsert failed: RuntimeError: {clean}"
        ]

    def test_every_edge_failing_reports_status_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        inner = _seed_two_legacy_edges(tmp_path)
        store = _OneEdgeUpsertFailsStore(inner, frozenset({"fail_edge", "ok_edge"}))
        monkeypatch.setattr(migrate_provenance_module, "get_graph_store", lambda: store)
        monkeypatch.setattr(migrate_provenance_module, "get_event_log", lambda: None)

        try:
            with pytest.raises(typer.Exit) as exc:
                migrate_provenance_command(
                    dry_run=False,
                    batch_size=10,
                    output_format="json",
                    no_meta_trace=True,
                )
            assert exc.value.exit_code == EXIT_STORE
        finally:
            store.close()

        payload = json.loads(capsys.readouterr().out)
        assert _SYNTHETIC_SECRET not in json.dumps(payload)
        assert payload["status"] == "error"
        assert payload["edges_migrated"] == 0
        assert len(payload["errors"]) == 2

    def test_no_errors_still_exits_ok_with_status_ok(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Negative control: a clean run keeps exiting 0, now with status."""
        inner = _seed_two_legacy_edges(tmp_path)
        store = _OneEdgeUpsertFailsStore(inner, frozenset())
        monkeypatch.setattr(migrate_provenance_module, "get_graph_store", lambda: store)
        monkeypatch.setattr(migrate_provenance_module, "get_event_log", lambda: None)

        try:
            with pytest.raises(typer.Exit) as exc:
                migrate_provenance_command(
                    dry_run=False,
                    batch_size=10,
                    output_format="json",
                    no_meta_trace=True,
                )
            assert exc.value.exit_code == 0
        finally:
            store.close()

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["errors"] == []


class _QueryFailsStore:
    """Wraps a real store; ``query`` raises with a synthetic secret.

    Exercises the *other* store-error output named in the plan: the
    generic ``except Exception`` handler in ``migrate_provenance_command``
    (reached when the scan itself fails, before any report exists), as
    opposed to ``_OneEdgeUpsertFailsStore`` above which exercises the
    per-edge upsert failure path inside an already-building report.
    """

    def __init__(self, inner: SQLiteGraphStore) -> None:
        self._inner = inner

    def query(self, **kwargs: Any) -> Any:
        msg = f"connection failed: {_SYNTHETIC_SECRET}"
        raise RuntimeError(msg)

    def get_edges(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.get_edges(*args, **kwargs)

    def close(self) -> None:
        self._inner.close()


class TestStoreErrorHandlerSanitizesSecret:
    @pytest.mark.parametrize("output_format", ["text", "json"])
    def test_scan_failure_exits_store_without_leaking_secret(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        output_format: str,
    ) -> None:
        inner = _make_store(tmp_path)
        store = _QueryFailsStore(inner)
        monkeypatch.setattr(migrate_provenance_module, "get_graph_store", lambda: store)
        monkeypatch.setattr(migrate_provenance_module, "get_event_log", lambda: None)

        try:
            with pytest.raises(typer.Exit) as exc:
                migrate_provenance_command(
                    dry_run=False,
                    batch_size=10,
                    output_format=output_format,
                    no_meta_trace=True,
                )
            assert exc.value.exit_code == EXIT_STORE
        finally:
            store.close()

        out = capsys.readouterr().out
        assert _SYNTHETIC_SECRET not in plain(out)

        if output_format == "json":
            payload = json.loads(out)
            assert payload["error"] == "store_error"
            assert "RuntimeError" in payload["message"]
        else:
            assert "store error" in plain(out)
            assert "RuntimeError" in plain(out)


class TestMigrateProvenanceCLI:
    def test_command_registered_on_admin_app(self) -> None:
        names = [cmd.name for cmd in admin_app.registered_commands]
        assert "migrate-provenance" in names

    def test_dry_run_emits_json_format(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        # Initialise stores so _get_registry doesn't bail.
        init = runner.invoke(admin_app, ["init"])
        assert init.exit_code == 0

        result = runner.invoke(
            admin_app, ["migrate-provenance", "--dry-run", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        # The output is a JSON object printed by Rich's Console.  Rich
        # may wrap with terminal control chars; the field we care
        # about is observable in the raw output.
        assert "edges_scanned" in result.output
        assert "dry_run" in result.output
