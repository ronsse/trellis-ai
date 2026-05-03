"""Tests for analyze CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis.learning import PROMOTE_RECOMMENDATIONS
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    """Point CLI stores at a temp directory and return the registry."""
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()

    return StoreRegistry(stores_dir=stores_dir)


@pytest.fixture
def temp_stores(_temp_stores: StoreRegistry) -> StoreRegistry:
    """Expose the autouse registry for tests that need direct access."""
    return _temp_stores


def _emit_pack_and_feedback(registry: StoreRegistry, *, success: bool) -> None:
    """Emit a PACK_ASSEMBLED + FEEDBACK_RECORDED pair for testing."""
    event_log = registry.event_log
    event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id="pack_1",
        entity_type="pack",
        payload={
            "intent": "test intent",
            "item_ids": ["item_a", "item_b"],
            "total_items": 2,
        },
    )
    event_log.emit(
        EventType.FEEDBACK_RECORDED,
        source="test",
        entity_id="pack_1",
        entity_type="pack",
        payload={
            "success": success,
            "rating": 1.0 if success else 0.0,
        },
    )


class TestContextEffectiveness:
    def test_empty_events(self) -> None:
        result = runner.invoke(app, ["analyze", "context-effectiveness"])
        assert result.exit_code == 0
        assert "Effectiveness" in result.stdout or "0" in result.stdout

    def test_empty_events_json(self) -> None:
        result = runner.invoke(
            app, ["analyze", "context-effectiveness", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["total_packs"] == 0

    def test_with_days_option(self) -> None:
        result = runner.invoke(app, ["analyze", "context-effectiveness", "--days", "7"])
        assert result.exit_code == 0

    def test_with_min_appearances_option(self) -> None:
        result = runner.invoke(
            app, ["analyze", "context-effectiveness", "--min-appearances", "5"]
        )
        assert result.exit_code == 0


class TestApplyNoiseTags:
    def test_no_noise_candidates(self) -> None:
        result = runner.invoke(app, ["analyze", "apply-noise-tags"])
        assert result.exit_code == 0

    def test_no_noise_json(self) -> None:
        result = runner.invoke(app, ["analyze", "apply-noise-tags", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["total_packs"] == 0

    def test_with_options(self) -> None:
        result = runner.invoke(
            app,
            ["analyze", "apply-noise-tags", "--days", "14", "--min-appearances", "3"],
        )
        assert result.exit_code == 0


class TestTokenUsage:
    def test_empty_events(self) -> None:
        result = runner.invoke(app, ["analyze", "token-usage"])
        assert result.exit_code == 0

    def test_empty_events_json(self) -> None:
        result = runner.invoke(app, ["analyze", "token-usage", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["total_responses"] == 0

    def test_with_days_option(self) -> None:
        result = runner.invoke(app, ["analyze", "token-usage", "--days", "1"])
        assert result.exit_code == 0


class TestAdvisoryEffectiveness:
    def test_empty_events(self) -> None:
        result = runner.invoke(app, ["analyze", "advisory-effectiveness"])
        assert result.exit_code == 0
        assert "Advisory Effectiveness" in result.stdout

    def test_empty_events_json(self) -> None:
        result = runner.invoke(
            app, ["analyze", "advisory-effectiveness", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["total_packs_with_advisories"] == 0

    def test_dry_run_flag(self) -> None:
        result = runner.invoke(app, ["analyze", "advisory-effectiveness", "--dry-run"])
        assert result.exit_code == 0

    def test_with_options(self) -> None:
        result = runner.invoke(
            app,
            [
                "analyze",
                "advisory-effectiveness",
                "--days",
                "14",
                "--min-presentations",
                "5",
                "--suppress-below",
                "0.2",
                "--blend-weight",
                "0.5",
            ],
        )
        assert result.exit_code == 0


class TestPackSections:
    def test_empty_events(self) -> None:
        result = runner.invoke(app, ["analyze", "pack-sections"])
        assert result.exit_code == 0
        assert "Sectioned packs analyzed: 0" in result.stdout

    def test_json_format_empty(self) -> None:
        result = runner.invoke(app, ["analyze", "pack-sections", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total_sectioned_packs"] == 0
        assert data["section_stats"] == []
        assert data["empty_section_flags"] == []

    def test_reports_section_stats(self, temp_stores: StoreRegistry) -> None:
        temp_stores.event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="pack_builder",
            entity_id="pk",
            entity_type="sectioned_pack",
            payload={
                "intent": "test",
                "sections": [
                    {"name": "domain", "items_count": 2, "item_ids": ["a", "b"]},
                    {"name": "tactical", "items_count": 0, "item_ids": []},
                ],
            },
        )
        result = runner.invoke(app, ["analyze", "pack-sections", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total_sectioned_packs"] == 1
        names = {row["name"] for row in data["section_stats"]}
        assert {"domain", "tactical"} <= names
        assert "tactical" in data["empty_section_flags"]


class TestLearningCandidates:
    def _seed_promote_signal(
        self,
        registry: StoreRegistry,
        *,
        item_id: str = "lc:doc:helpful",
        rounds: int = 3,
    ) -> None:
        """Emit ``rounds`` graded packs marking ``item_id`` as helpful + successful."""
        event_log = registry.event_log
        for i in range(rounds):
            pack_id = f"lc-pack-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "intent": "test intent",
                    "domain": "lc-test",
                    "injected_items": [
                        {
                            "item_id": item_id,
                            "item_type": "document",
                            "rank": 0,
                            "strategy_source": "document",
                        }
                    ],
                    "injected_item_ids": [item_id],
                },
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "pack_id": pack_id,
                    "outcome": "success",
                    "success": True,
                    "helpful_item_ids": [item_id],
                },
            )

    def test_empty_event_log_writes_artifacts(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "review"
        result = runner.invoke(
            app,
            [
                "analyze",
                "learning-candidates",
                "--output-dir",
                str(out_dir),
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        assert data["observation_count"] == 0
        assert data["candidate_count"] == 0
        assert data["candidates"] == []
        assert Path(data["candidates_path"]).exists()
        assert Path(data["decisions_template_path"]).exists()

    def test_promote_signal_surfaces_candidate(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        self._seed_promote_signal(temp_stores)
        out_dir = tmp_path / "review"
        result = runner.invoke(
            app,
            [
                "analyze",
                "learning-candidates",
                "--output-dir",
                str(out_dir),
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["candidate_count"] == 1, data
        candidate = data["candidates"][0]
        assert candidate["item_id"] == "lc:doc:helpful"
        assert candidate["recommendation_type"] in PROMOTE_RECOMMENDATIONS
        decisions = json.loads(
            Path(data["decisions_template_path"]).read_text(encoding="utf-8")
        )
        ids = {d["candidate_id"] for d in decisions["decisions"]}
        assert candidate["candidate_id"] in ids

    def test_min_support_filters(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        self._seed_promote_signal(temp_stores, rounds=1)
        out_dir = tmp_path / "review"
        result = runner.invoke(
            app,
            [
                "analyze",
                "learning-candidates",
                "--output-dir",
                str(out_dir),
                "--min-support",
                "5",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["candidate_count"] == 0, data


class TestAnalyzeHelp:
    def test_help(self) -> None:
        result = runner.invoke(app, ["analyze", "--help"])
        assert result.exit_code == 0
        for cmd in [
            "context-effectiveness",
            "apply-noise-tags",
            "token-usage",
            "advisory-effectiveness",
            "pack-sections",
            "learning-candidates",
        ]:
            assert cmd in result.stdout
