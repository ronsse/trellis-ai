"""Tests for CLI meta-trace wiring (Item 6 Phase 2).

Covers ``src/trellis_cli/_meta_wiring.py`` plus the per-command
integration: every wrapped ``analyze`` / ``admin`` subcommand emits an
``Activity`` node when invoked, and the ``--no-meta-trace`` flag turns
recording off without affecting the command's primary output.

The tests don't try to exhaustively cover every analyze subcommand —
they pick a representative pair (``context-effectiveness`` for
analyze; ``draft-promotion-adr`` for admin) and verify the cross-cutting
contract. The wiring is shared infrastructure; spot-checking is enough
to catch a regression in the helper or its invocation pattern.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis.meta import META_AGENT_PREFIX
from trellis.schemas import well_known as wk
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli._meta_wiring import cli_meta_agent_id, wrap_cli_meta_analysis
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared fixture — point CLI registry + worktree-wide registry at tmp dir
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    """Wire the CLI at ``tmp_path`` and return a direct-access registry."""
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()

    return StoreRegistry(stores_dir=stores_dir)


# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------


class TestCliMetaAgentId:
    def test_no_suffix_returns_bare_prefix(self) -> None:
        agent_id = cli_meta_agent_id()
        assert agent_id == f"{META_AGENT_PREFIX}cli"
        assert agent_id.startswith(META_AGENT_PREFIX)

    def test_suffix_appended(self) -> None:
        assert cli_meta_agent_id("analyze") == f"{META_AGENT_PREFIX}cli_analyze"
        assert cli_meta_agent_id("admin") == f"{META_AGENT_PREFIX}cli_admin"

    def test_empty_suffix_treated_as_no_suffix(self) -> None:
        assert cli_meta_agent_id("") == f"{META_AGENT_PREFIX}cli"

    def test_agent_id_always_synthetic_prefix(self) -> None:
        """Every CLI agent_id must start with the reserved namespace."""
        for suffix in ("analyze", "admin", "tune", "schema_evolution", ""):
            assert cli_meta_agent_id(suffix).startswith(META_AGENT_PREFIX)


class TestWrapCliMetaAnalysisHelper:
    def test_disabled_yields_noop_record(self, temp_stores: StoreRegistry) -> None:
        """``disabled=True`` short-circuits to a no-op record."""
        with wrap_cli_meta_analysis(
            agent_suffix="analyze",
            analyzer_name="cli.test.disabled",
            disabled=True,
        ) as record:
            assert record.enabled is False
            assert record.activity_id is None
            # No-op methods accept calls without writing anything.
            record.consumed_event("evt-1")
            record.produced_finding("finding-1", finding_type="Test")

        # Activity store stays empty since the recorder was bypassed.
        graph = temp_stores.knowledge.graph_store
        activities = graph.query(node_type=wk.ACTIVITY, limit=10)
        assert activities == []

    def test_enabled_records_activity_in_graph(
        self, temp_stores: StoreRegistry
    ) -> None:
        """Running enabled writes a real Activity + Agent + wasAssociatedWith edge."""
        with wrap_cli_meta_analysis(
            agent_suffix="analyze",
            analyzer_name="cli.test.enabled",
        ) as record:
            assert record.enabled is True
            assert record.activity_id is not None

        graph = temp_stores.knowledge.graph_store
        activities = graph.query(
            node_type=wk.ACTIVITY,
            properties={"analyzer_name": "cli.test.enabled"},
            limit=10,
        )
        assert len(activities) == 1
        props = activities[0]["properties"]
        assert props["agent_id"] == cli_meta_agent_id("analyze")

        # The synthetic Agent node was created.
        agent = graph.get_node(cli_meta_agent_id("analyze"))
        assert agent is not None
        assert agent["node_type"] == wk.AGENT


# ---------------------------------------------------------------------------
# Per-command integration tests
# ---------------------------------------------------------------------------


def _activities_for(registry: StoreRegistry, analyzer_name: str) -> list[dict]:
    return registry.knowledge.graph_store.query(
        node_type=wk.ACTIVITY,
        properties={"analyzer_name": analyzer_name},
        limit=10,
    )


class TestAnalyzeContextEffectivenessWiring:
    """Spot-check the analyze wiring against ``context-effectiveness``."""

    def test_records_activity_by_default(self, temp_stores: StoreRegistry) -> None:
        # Emit some PACK_ASSEMBLED + FEEDBACK_RECORDED so the analyzer
        # has something to crunch (otherwise ``produced_finding`` skips,
        # but the Activity itself is still recorded).
        event_log = temp_stores.operational.event_log
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id="pack_1",
            entity_type="pack",
            payload={
                "intent": "test",
                "item_ids": ["item_a"],
                "total_items": 1,
            },
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id="pack_1",
            entity_type="pack",
            payload={"success": True, "rating": 1.0},
        )

        result = runner.invoke(
            app,
            ["analyze", "context-effectiveness", "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        # Output preserved — the JSON parses to a non-error shape.
        payload = json.loads(result.stdout.strip())
        assert "total_packs" in payload

        # An Activity was recorded under the CLI agent.
        activities = _activities_for(temp_stores, "cli.analyze.context-effectiveness")
        assert len(activities) == 1, f"expected exactly 1 Activity; got {activities}"
        assert activities[0]["properties"]["agent_id"].startswith(META_AGENT_PREFIX)

    def test_no_meta_trace_disables_recording(self, temp_stores: StoreRegistry) -> None:
        result = runner.invoke(
            app,
            [
                "analyze",
                "context-effectiveness",
                "--no-meta-trace",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output

        activities = _activities_for(temp_stores, "cli.analyze.context-effectiveness")
        assert activities == [], (
            f"--no-meta-trace must skip recording; got {activities}"
        )

    def test_command_output_unchanged_with_meta_wiring(
        self, temp_stores: StoreRegistry
    ) -> None:
        """Wrapping the command must not alter its primary stdout shape."""
        with_meta = runner.invoke(
            app,
            ["analyze", "context-effectiveness", "--format", "json"],
        )
        without_meta = runner.invoke(
            app,
            [
                "analyze",
                "context-effectiveness",
                "--no-meta-trace",
                "--format",
                "json",
            ],
        )
        assert with_meta.exit_code == 0
        assert without_meta.exit_code == 0
        # The analyzer's report shape is identical regardless of meta-trace.
        assert (
            json.loads(with_meta.stdout.strip()).keys()
            == json.loads(without_meta.stdout.strip()).keys()
        )


class TestAnalyzeSchemaEvolutionWiring:
    """Spot-check that schema-evolution records an Activity too."""

    def test_records_activity_in_dry_run(self, temp_stores: StoreRegistry) -> None:
        result = runner.invoke(
            app,
            [
                "analyze",
                "schema-evolution",
                "--no-emit",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output

        activities = _activities_for(temp_stores, "cli.analyze.schema-evolution")
        assert len(activities) == 1


class TestAdminDraftPromotionADRWiring:
    """Spot-check the admin wrapping on ``draft-promotion-adr``.

    This one's safer than ``migrate-graph`` (which needs two yaml configs)
    and ``migrate-provenance`` (which writes to the live graph). It only
    needs a single WELL_KNOWN_CANDIDATE event seeded.
    """

    def _seed_candidate(self, registry: StoreRegistry) -> str:
        event_log = registry.operational.event_log
        candidate_id = "wkc-test-1"
        event_log.emit(
            EventType.WELL_KNOWN_CANDIDATE,
            source="test",
            entity_id=candidate_id,
            entity_type="well_known_candidate",
            payload={
                "candidate_id": candidate_id,
                "open_string_value": "test_kind",
                "candidate_kind": "entity_type",
                "count": 10,
                "distinct_extractors": ["a", "b"],
                "distinct_domains": ["d1", "d2"],
                "suggested_canonical_name": "TestKind",
                "naming_collision": False,
                "notes": [],
            },
        )
        return candidate_id

    def test_records_activity_by_default(
        self, temp_stores: StoreRegistry, tmp_path: Path
    ) -> None:
        candidate_id = self._seed_candidate(temp_stores)
        output_path = tmp_path / "out" / "adr.md"
        result = runner.invoke(
            app,
            [
                "admin",
                "draft-promotion-adr",
                candidate_id,
                "--output",
                str(output_path),
            ],
        )
        assert result.exit_code == 0, result.output
        # Side effect: the ADR was actually written.
        assert output_path.exists()

        activities = _activities_for(temp_stores, "cli.admin.draft-promotion-adr")
        assert len(activities) == 1

    def test_no_meta_trace_disables_recording(
        self, temp_stores: StoreRegistry, tmp_path: Path
    ) -> None:
        candidate_id = self._seed_candidate(temp_stores)
        output_path = tmp_path / "out" / "adr.md"
        result = runner.invoke(
            app,
            [
                "admin",
                "draft-promotion-adr",
                candidate_id,
                "--output",
                str(output_path),
                "--no-meta-trace",
            ],
        )
        assert result.exit_code == 0, result.output
        assert output_path.exists()

        activities = _activities_for(temp_stores, "cli.admin.draft-promotion-adr")
        assert activities == []


class TestMetaTraceEnvOff:
    """When ``TRELLIS_META_TRACES=off`` no Activity is recorded.

    Sanity-check: the wrapping helper delegates to the recorder, so the
    recorder's env-var gate must propagate through CLI calls too.
    """

    def test_env_off_skips_recording(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_META_TRACES", "off")
        result = runner.invoke(
            app,
            ["analyze", "context-effectiveness", "--format", "json"],
        )
        assert result.exit_code == 0, result.output

        activities = _activities_for(temp_stores, "cli.analyze.context-effectiveness")
        assert activities == [], (
            f"TRELLIS_META_TRACES=off must skip recording; got {activities}"
        )
