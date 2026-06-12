"""Tests for ``trellis admin migrate-provenance``.

Lives under ``tests/unit/cli`` so it runs in the no-live-infra
fast suite.  Exercises the programmatic entry point
:func:`run_migrate_provenance` directly against an in-memory SQLite
store — no subprocess, no Typer wrapping.  A separate CliRunner
test asserts the command registers and exit codes route correctly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.graph import SQLiteGraphStore
from trellis_cli.admin import admin_app
from trellis_cli.admin_migrate_provenance import (
    MigrationDriftError,
    run_migrate_provenance,
)

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
