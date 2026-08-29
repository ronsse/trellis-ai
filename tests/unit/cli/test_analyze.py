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
    event_log = registry.operational.event_log
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


class TestCost:
    def test_empty_events_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        result = runner.invoke(app, ["analyze", "cost", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["overhead_events"] == 0
        assert data["overhead_dollars"] == 0.0

    def test_prices_injected_overhead(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        from trellis.retrieve.token_tracker import track_token_usage

        log = temp_stores.operational.event_log
        for _ in range(10):
            track_token_usage(
                log, layer="mcp", operation="get_context", response_tokens=1500
            )
        result = runner.invoke(
            app,
            ["analyze", "cost", "--model", "claude-opus", "--format", "json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["overhead_tokens"] == 15_000
        assert data["price_per_mtok"] == 15.0
        assert data["overhead_dollars"] == pytest.approx(0.225)

    def test_text_output_shows_dollars(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        from trellis.retrieve.token_tracker import track_token_usage

        track_token_usage(
            temp_stores.operational.event_log,
            layer="mcp",
            operation="get_context",
            response_tokens=4000,
        )
        result = runner.invoke(app, ["analyze", "cost", "--price-per-mtok", "3"])
        assert result.exit_code == 0
        assert "Trellis Cost Overhead" in result.stdout
        assert "$" in result.stdout


class TestReplay:
    """``trellis analyze replay`` — the same window under a different policy."""

    @staticmethod
    def _seed(registry: StoreRegistry, count: int, *, items: int = 12) -> None:
        """Packs with a fat, uncited tail and a full budget_trace."""
        event_log = registry.operational.event_log
        for index in range(count):
            pack_id = f"rpack_{index}"
            ids = [f"{pack_id}_{k}" for k in range(items)]
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "pack_id": pack_id,
                    "intent_family": "general_context",
                    "budget_max_tokens": 2000,
                    "injected_item_ids": ids,
                    "injected_items": [
                        {
                            "item_id": item_id,
                            "item_type": "vector",
                            "strategy_source": "semantic",
                            "estimated_tokens": 120,
                            "rank": rank,
                            "title": f"Memory {rank}",
                        }
                        for rank, item_id in enumerate(ids, start=1)
                    ],
                    "budget_trace": [
                        {
                            "item_id": item_id,
                            "item_tokens": 120,
                            "running_total": 120 * (rank + 1),
                            "included": True,
                        }
                        for rank, item_id in enumerate(ids)
                    ],
                },
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="mcp",
                entity_id=pack_id,
                payload={
                    "pack_id": pack_id,
                    "helpful_item_ids": [ids[0]],
                    "unhelpful_item_ids": [ids[-1]],
                    "intent_family": "general_context",
                    "rating": 0.5,
                    "success": True,
                },
            )

    def test_empty_events_refuse_the_ratio(self) -> None:
        result = runner.invoke(app, ["analyze", "replay", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["attributed_packs"] == 0
        assert data["suppressed"] is True
        assert data["baseline"]["useful_token_fraction"] is None

    def test_no_policy_is_an_identity(self, temp_stores: StoreRegistry) -> None:
        """Every delta is measured against this; it must be exact."""
        self._seed(temp_stores, 6)
        result = runner.invoke(app, ["analyze", "replay", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["token_delta"] == 0.0
        assert data["fraction_delta"] == 0.0

    def test_graduation_saves_tokens_and_lifts_the_fraction(
        self, temp_stores: StoreRegistry
    ) -> None:
        self._seed(temp_stores, 6)
        result = runner.invoke(
            app, ["analyze", "replay", "--body-items", "4", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["token_delta"] < 0
        assert data["fraction_delta"] > 0
        assert data["counterfactual"]["pointer_items_served"] > 0
        # The head was cited, so nothing useful lost its body.
        assert data["helpful_bodies_withheld"] == 0
        assert data["helpful_items_dropped"] == 0

    def test_text_output_prints_the_cost_beside_the_saving(
        self, temp_stores: StoreRegistry
    ) -> None:
        """A saving reported without its recall cost is the failure mode."""
        self._seed(temp_stores, 6)
        result = runner.invoke(app, ["analyze", "replay", "--body-items", "4"])
        assert result.exit_code == 0
        assert "What the policy cost" in result.stdout
        assert "body withheld" in result.stdout
        assert "dropped" in result.stdout

    def test_degenerate_policy_exits_two(self) -> None:
        result = runner.invoke(app, ["analyze", "replay", "--body-items", "0"])
        assert result.exit_code == 2


class TestValue:
    """``trellis analyze value`` — serving precision, with its coverage."""

    @staticmethod
    def _seed_attributed_packs(
        registry: StoreRegistry, count: int, *, helpful: bool = True
    ) -> None:
        event_log = registry.operational.event_log
        for index in range(count):
            pack_id = f"vpack_{index}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "intent_family": "general_context",
                    "injected_item_ids": [f"{pack_id}_a", f"{pack_id}_b"],
                    "injected_items": [
                        {
                            "item_id": f"{pack_id}_a",
                            "item_type": "vector",
                            "strategy_source": "semantic",
                            "estimated_tokens": 100,
                            "rank": 0,
                        },
                        {
                            "item_id": f"{pack_id}_b",
                            "item_type": "document",
                            "strategy_source": "keyword",
                            "estimated_tokens": 100,
                            "rank": 1,
                        },
                    ],
                },
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="mcp",
                entity_id=pack_id,
                payload={
                    "pack_id": pack_id,
                    "helpful_item_ids": [f"{pack_id}_a"] if helpful else [],
                    "unhelpful_item_ids": [f"{pack_id}_b"]
                    if helpful
                    else [f"{pack_id}_a", f"{pack_id}_b"],
                    "intent_family": "general_context",
                    "rating": 0.5,
                    "success": True,
                },
            )

    def test_empty_events_json_refuses_ratio(self) -> None:
        result = runner.invoke(app, ["analyze", "value", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["attributed_packs"] == 0
        # Refused, not zero — the distinction the whole report turns on.
        assert data["useful_token_fraction"] is None
        assert data["suppressed"] is True
        assert data["min_attributed_packs"] > 0

    def test_reports_fraction_with_sample_size(
        self, temp_stores: StoreRegistry
    ) -> None:
        self._seed_attributed_packs(temp_stores, 6)
        result = runner.invoke(app, ["analyze", "value", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())

        assert data["attributed_packs"] == 6
        assert data["useful_token_fraction"] == 0.5
        assert data["suppressed"] is False
        # Coverage rides alongside the ratio, never separately.
        assert data["pack_targeted_feedback"] == 6
        assert data["pack_targeted_attributed"] == 6
        assert data["injected_tokens"] == 1200
        assert data["helpful_tokens"] == 600

    def test_json_carries_every_axis(self, temp_stores: StoreRegistry) -> None:
        self._seed_attributed_packs(temp_stores, 6)
        result = runner.invoke(app, ["analyze", "value", "--format", "json"])
        data = json.loads(result.stdout.strip())

        assert {c["key"] for c in data["by_strategy"]} == {"semantic", "keyword"}
        assert {c["key"] for c in data["by_item_type"]} == {"vector", "document"}
        assert {c["key"] for c in data["by_intent_family"]} == {"general_context"}
        for cell in data["by_strategy"]:
            assert "attributed_packs" in cell

    def test_text_output_states_n_and_never_says_benefit(
        self, temp_stores: StoreRegistry
    ) -> None:
        self._seed_attributed_packs(temp_stores, 6)
        result = runner.invoke(app, ["analyze", "value"])
        assert result.exit_code == 0
        assert "useful-token fraction" in result.stdout
        assert "n=6" in result.stdout
        # Naming discipline: this is serving precision, not benefit. Rich
        # wraps, so normalise whitespace before reading the phrase — every
        # occurrence of the word must be the disclaimer denying it.
        flat = " ".join(result.stdout.split())
        assert flat.count("benefit") == flat.count("not benefit") >= 1

    def test_text_output_refuses_thin_sample_visibly(
        self, temp_stores: StoreRegistry
    ) -> None:
        self._seed_attributed_packs(temp_stores, 2)
        result = runner.invoke(app, ["analyze", "value"])
        assert result.exit_code == 0
        assert "Ratio refused" in result.stdout
        assert "minimum" in result.stdout

    def test_price_override_moves_dollars_per_cited_item(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        self._seed_attributed_packs(temp_stores, 6)
        cheap = json.loads(
            runner.invoke(
                app,
                ["analyze", "value", "--price-per-mtok", "3", "--format", "json"],
            ).stdout.strip()
        )
        dear = json.loads(
            runner.invoke(
                app,
                ["analyze", "value", "--price-per-mtok", "30", "--format", "json"],
            ).stdout.strip()
        )
        assert dear["dollars_per_cited_item"] == pytest.approx(
            cheap["dollars_per_cited_item"] * 10, rel=1e-6
        )


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
        temp_stores.operational.event_log.emit(
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


class TestPackQualityAssembly:
    """``analyze pack-quality``'s live assembly mirrors production wiring.

    #259: the eval harness must assemble packs the way the MCP server / API
    do — with near-duplicate suppression enabled. Without the wire-up, a
    scenario containing a cross-source near-dup pair scores 2 items in eval
    while production ships 1, and every pack-quality score silently measures
    a pack shape production never serves.
    """

    #: Synthetic fact + frontmatter-wrapped copy (the F14 pair shape).
    _FACT = (
        "The staging deployment pipeline runs the full migration suite before "
        "promoting a build, then validates the schema against a read replica "
        "and posts a summary to the release channel. Rollbacks trigger "
        "automatically when the post-deploy smoke tests fail. The pipeline "
        "config is reviewed by two engineers before any change merges."
    )
    _CORPUS_COPY = (
        "---\n"
        "source: notes-import\n"
        "tags: [deploy, pipeline]\n"
        "---\n"
        "# Note: staging deployment pipeline\n\n" + _FACT
    )

    def test_assembly_suppresses_near_duplicates(
        self, temp_stores: StoreRegistry
    ) -> None:
        from trellis.retrieve.evaluate import EvaluationScenario
        from trellis_cli.analyze import _assemble_pack_for_scenario

        doc_store = temp_stores.knowledge.document_store
        doc_store.put("sm-pipeline", self._FACT)
        doc_store.put("corpus-pipeline", self._CORPUS_COPY)

        pack = _assemble_pack_for_scenario(
            EvaluationScenario(
                name="near-dup-pair",
                intent="staging deployment pipeline migration rollback",
            )
        )
        served = {item.item_id for item in pack.items}  # type: ignore[attr-defined]
        survivors = served & {"sm-pipeline", "corpus-pipeline"}
        assert len(survivors) == 1, f"expected one survivor, got {survivors}"


class TestLearningCandidates:
    def _seed_promote_signal(
        self,
        registry: StoreRegistry,
        *,
        item_id: str = "lc:doc:helpful",
        rounds: int = 3,
    ) -> None:
        """Emit ``rounds`` graded packs marking ``item_id`` as helpful + successful."""
        event_log = registry.operational.event_log
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


class TestLearningParameterRegistry:
    """CLI-side wiring for the registry-required scoring path.

    Plan §6 in docs/design/plan-parameter-registry-wiring.md requires the
    CLI to (a) load a config-file registry when present and (b) WARN
    when it falls back to in-module seed defaults. Library-side TypeError
    / KeyError tests live in tests/unit/learning/test_scoring.py.
    """

    def test_cli_warns_on_default_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Capture the structlog warning emitted when no learning_params.yaml
        # is present. The CliRunner muting (TRELLIS_LOG_LEVEL=CRITICAL via
        # tests/unit/cli/conftest.py) means we must reach past the muted
        # logger and capture the call directly.
        from trellis_cli import analyze as analyze_module

        captured: list[dict[str, object]] = []

        def _capture(event: str, **kwargs: object) -> None:
            captured.append({"event": event, **kwargs})

        monkeypatch.setattr(analyze_module.logger, "warning", _capture)

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
        assert captured, "expected a WARN entry when no learning_params.yaml exists"
        events = [entry["event"] for entry in captured]
        assert "learning.parameter_registry.seeded_defaults" in events

    def test_cli_uses_config_registry_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Write a config file. Set the noise_success_threshold high enough
        # that an otherwise-neutral 0.6 success rate flips to
        # investigate_noise — proves the file values actually drive
        # decisions and the WARN path is bypassed.
        import yaml as _yaml

        from trellis_cli import analyze as analyze_module
        from trellis_cli.config import get_config_dir

        config_dir = get_config_dir()
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "learning_params.yaml").write_text(
            _yaml.dump(
                {
                    "promote_success_threshold": 0.75,
                    "promote_retry_threshold": 0.25,
                    "noise_success_threshold": 0.99,
                    "noise_retry_threshold": 0.5,
                }
            ),
            encoding="utf-8",
        )

        captured: list[dict[str, object]] = []
        monkeypatch.setattr(
            analyze_module.logger,
            "warning",
            lambda event, **kwargs: captured.append({"event": event, **kwargs}),
        )

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
        events = [entry["event"] for entry in captured]
        assert "learning.parameter_registry.seeded_defaults" not in events, (
            "config-loaded registry must not emit the defaults WARN"
        )


class TestAnalyzeDomains:
    """`trellis analyze domains` — domain usage report (WP7)."""

    def _seed(self, registry: StoreRegistry) -> None:
        from trellis.schemas.enums import TraceSource
        from trellis.schemas.trace import Trace, TraceContext

        trace_store = registry.operational.trace_store
        document_store = registry.knowledge.document_store
        event_log = registry.operational.event_log

        for _ in range(2):
            trace_store.append(
                Trace(
                    source=TraceSource.AGENT,
                    intent="x",
                    steps=[],
                    context=TraceContext(agent_id="a", domain="payments"),
                )
            )
        trace_store.append(
            Trace(
                source=TraceSource.AGENT,
                intent="x",
                steps=[],
                context=TraceContext(agent_id="a", domain=None),
            )
        )
        document_store.put("d1", "doc", {"content_tags": {"domain": ["payments"]}})
        document_store.put("d2", "doc", {"author": "alice"})

        for pid in ("p1", "p2"):
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pid,
                entity_type="pack",
                payload={"domain": "payments", "intent": "x"},
            )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id="p1",
            entity_type="pack",
            payload={"pack_id": "p1", "success": True},
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id="p2",
            entity_type="pack",
            payload={"pack_id": "p2", "success": False},
        )

    def test_empty_text(self) -> None:
        result = runner.invoke(app, ["analyze", "domains"])
        assert result.exit_code == 0, result.output
        assert "Domains observed: 0" in result.stdout

    def test_json_shape_and_counts(self, temp_stores: StoreRegistry) -> None:
        self._seed(temp_stores)
        result = runner.invoke(app, ["analyze", "domains", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        rows = {row["domain"]: row for row in data["domains"]}

        payments = rows["payments"]
        assert payments["trace_count"] == 2
        assert payments["document_count"] == 1
        assert payments["packs_served"] == 2
        assert payments["graded_packs"] == 2
        assert payments["graded_successes"] == 1
        assert payments["success_rate"] == 0.5

        none_row = rows["(none)"]
        assert none_row["trace_count"] == 1
        assert none_row["document_count"] == 1
        assert none_row["success_rate"] is None

    def test_text_table_renders(self, temp_stores: StoreRegistry) -> None:
        self._seed(temp_stores)
        result = runner.invoke(app, ["analyze", "domains"])
        assert result.exit_code == 0, result.output
        assert "payments" in result.stdout
        assert "(none)" in result.stdout


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
            "domains",
            "learning-candidates",
            "schema-evolution",
        ]:
            assert cmd in result.stdout


# ---------------------------------------------------------------------------
# Schema-evolution CLI tests (self-improvement item 5)
# ---------------------------------------------------------------------------


def _seed_schema_evolution_candidate(registry: StoreRegistry) -> None:
    """Plant 30 ``metric`` nodes across two extractors / two domains."""
    graph_store = registry.knowledge.graph_store
    event_log = registry.operational.event_log
    for i in range(15):
        nid = graph_store.upsert_node(
            node_id=f"metric_{i}",
            node_type="metric",
            properties={
                "content_tags": {
                    "domain": ["analytics"],
                    "signal_quality": "standard",
                },
            },
        )
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            source="mutation_executor",
            entity_id=nid,
            entity_type="metric",
            payload={"requested_by": "worker:dbt"},
        )
    for i in range(15, 30):
        nid = graph_store.upsert_node(
            node_id=f"metric_{i}",
            node_type="metric",
            properties={
                "content_tags": {
                    "domain": ["finance"],
                    "signal_quality": "standard",
                },
            },
        )
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            source="mutation_executor",
            entity_id=nid,
            entity_type="metric",
            payload={"requested_by": "worker:lineage"},
        )


def _override_schema_evolution_thresholds(registry: StoreRegistry) -> None:
    """Persist a low-threshold snapshot so 30 synthetic nodes surface."""
    from trellis.learning.schema_evolution import (
        PARAM_COMPONENT_ID,
        RECOMMENDED_SEED_VALUES,
    )
    from trellis.schemas.parameters import ParameterScope, ParameterSet

    values: dict[str, float | int | str | bool] = dict(RECOMMENDED_SEED_VALUES)
    values["well_known_count_threshold"] = 20
    values["well_known_window_days"] = 0
    registry.operational.parameter_store.put(
        ParameterSet(
            scope=ParameterScope(component_id=PARAM_COMPONENT_ID),
            values=values,
            source="test:cli_schema_evolution",
        )
    )


class TestSchemaEvolutionCLI:
    def test_empty_graph_no_candidates_text(self, temp_stores: StoreRegistry) -> None:
        _override_schema_evolution_thresholds(temp_stores)
        result = runner.invoke(app, ["analyze", "schema-evolution"])
        assert result.exit_code == 0, result.output
        assert "0 surfaced" in result.output

    def test_empty_graph_no_candidates_json(self, temp_stores: StoreRegistry) -> None:
        _override_schema_evolution_thresholds(temp_stores)
        result = runner.invoke(app, ["analyze", "schema-evolution", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        assert data["candidate_count"] == 0
        assert data["candidates"] == []
        assert data["emitted"] is False

    def test_surfaces_candidate_and_emits_event(
        self, temp_stores: StoreRegistry
    ) -> None:
        _override_schema_evolution_thresholds(temp_stores)
        _seed_schema_evolution_candidate(temp_stores)
        result = runner.invoke(app, ["analyze", "schema-evolution", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["candidate_count"] == 1
        assert data["candidates"][0]["open_string_value"] == "metric"
        assert data["candidates"][0]["suggested_canonical_name"] == "Metric"
        assert data["emitted"] is True
        events = temp_stores.operational.event_log.get_events(
            event_type=EventType.WELL_KNOWN_CANDIDATE, limit=5
        )
        assert len(events) == 1

    def test_dry_run_no_events_emitted(self, temp_stores: StoreRegistry) -> None:
        _override_schema_evolution_thresholds(temp_stores)
        _seed_schema_evolution_candidate(temp_stores)
        result = runner.invoke(
            app,
            ["analyze", "schema-evolution", "--no-emit", "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["candidate_count"] == 1
        assert data["emitted"] is False
        events = temp_stores.operational.event_log.get_events(
            event_type=EventType.WELL_KNOWN_CANDIDATE, limit=5
        )
        assert events == []

    def test_strict_exits_nonzero_when_candidate_surfaces(
        self, temp_stores: StoreRegistry
    ) -> None:
        _override_schema_evolution_thresholds(temp_stores)
        _seed_schema_evolution_candidate(temp_stores)
        result = runner.invoke(
            app, ["analyze", "schema-evolution", "--strict", "--format", "json"]
        )
        assert result.exit_code == 1, result.output

    def test_invalid_kinds_rejected(self, temp_stores: StoreRegistry) -> None:
        _override_schema_evolution_thresholds(temp_stores)
        result = runner.invoke(
            app, ["analyze", "schema-evolution", "--kinds", "not_a_kind"]
        )
        assert result.exit_code != 0


class TestAnalyzeHealthCaptureSection:
    """E2 + #365 reach the operator surface, in both output formats."""

    def _emit_sweep(self, registry: StoreRegistry, **payload: object) -> None:
        base: dict[str, object] = {
            "dry_run": False,
            "sessions_seen": 40,
            "sessions_parsed": 20,
            "sessions_triggered": 20,
            "sessions_skipped_watermark": 20,
            "sessions_skipped_empty": 0,
            "sessions_sampled_out": 0,
            "sessions_judge_unavailable": 0,
            "sessions_with_memory": 12,
        }
        base.update(payload)
        registry.operational.event_log.emit(
            EventType.CAPTURE_SWEEP_COMPLETED,
            source="worker:session-capture",
            payload=base,
        )

    def test_json_carries_capture_and_availability_fields(
        self, temp_stores: StoreRegistry
    ) -> None:
        self._emit_sweep(temp_stores)
        temp_stores.operational.event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mcp:record_feedback",
            payload={"rating": 0.9, "success": True},
        )

        result = runner.invoke(app, ["analyze", "health", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)

        assert data["capture"]["state"] == "measured"
        assert data["capture"]["capture_rate"] == pytest.approx(0.6)
        assert data["capture"]["eligible_sessions"] == 20
        assert data["capture"]["funnel"]["sessions_seen"] == 40
        assert "#365" in data["serve"]["retrieval_availability_note"]
        assert data["serve"]["retrieval_availability_measured"] is False

    def test_text_output_prints_the_rate_and_the_funnel(
        self, temp_stores: StoreRegistry
    ) -> None:
        self._emit_sweep(temp_stores)
        result = runner.invoke(app, ["analyze", "health"])
        assert result.exit_code == 0
        assert "Capture:" in result.stdout
        assert "funnel:" in result.stdout

    def test_absent_capture_data_prints_a_state_not_a_zero(
        self, temp_stores: StoreRegistry
    ) -> None:
        """The whole point: no sweeps must not render as 0% coverage."""
        result = runner.invoke(app, ["analyze", "health"])
        assert result.exit_code == 0
        capture_line = next(
            line for line in result.stdout.splitlines() if "Capture:" in line
        )
        assert "unobserved" in capture_line
        assert "%" not in capture_line

    def test_json_capture_rate_is_null_when_unobserved(
        self, temp_stores: StoreRegistry
    ) -> None:
        result = runner.invoke(app, ["analyze", "health", "--format", "json"])
        data = json.loads(result.stdout)
        assert data["capture"]["capture_rate"] is None
        assert data["capture"]["state"] == "unobserved"


class TestTruncationReachesTheOperator:
    """A capped report must say so on the surface a human reads (#374).

    `tests/unit/ops/test_analyzer_truncation.py` pins the note into
    `report.notes` / `report.scan`. That is not the same claim. Both
    `pack-telemetry` and `extractor-fallbacks` rendered `report.notes` only
    inside their `== 0` early return — and a truncated scan has
    `total_packs == limit`, so the branch that printed the caveat was
    exactly the branch truncation guarantees is not taken. The note was
    reachable in text mode only in the one state where it cannot exist.

    Asserting a value lands in a model is not asserting an operator sees it,
    so these drive the real CLI and read stdout.
    """

    @staticmethod
    def _flood(registry: StoreRegistry, event_type: EventType, count: int) -> None:
        event_log = registry.operational.event_log
        for index in range(count):
            event_log.emit(
                event_type,
                source="test",
                entity_id=f"e_{index}",
                payload={
                    "injected_items": [],
                    "rejected_items": [],
                    "source_hint": "src",
                },
            )

    def test_pack_telemetry_prints_the_truncation_note(
        self, temp_stores: StoreRegistry
    ) -> None:
        self._flood(temp_stores, EventType.PACK_ASSEMBLED, 5001)
        result = runner.invoke(app, ["analyze", "pack-telemetry", "--days", "30"])
        assert result.exit_code == 0
        assert "TRUNCATED" in result.stdout
        # And the paired negative: an uncapped window makes no such claim.
        assert "Packs assembled: 5000" in result.stdout

    def test_pack_telemetry_makes_no_truncation_claim_when_uncapped(
        self, temp_stores: StoreRegistry
    ) -> None:
        self._flood(temp_stores, EventType.PACK_ASSEMBLED, 3)
        result = runner.invoke(app, ["analyze", "pack-telemetry", "--days", "30"])
        assert result.exit_code == 0
        assert "TRUNCATED" not in result.stdout

    def test_extractor_fallbacks_prints_the_truncation_note(
        self, temp_stores: StoreRegistry
    ) -> None:
        self._flood(temp_stores, EventType.EXTRACTION_DISPATCHED, 5001)
        result = runner.invoke(app, ["analyze", "extractor-fallbacks", "--days", "30"])
        assert result.exit_code == 0
        assert "TRUNCATED" in result.stdout

    def test_extractor_fallbacks_makes_no_claim_when_uncapped(
        self, temp_stores: StoreRegistry
    ) -> None:
        self._flood(temp_stores, EventType.EXTRACTION_DISPATCHED, 3)
        result = runner.invoke(app, ["analyze", "extractor-fallbacks", "--days", "30"])
        assert result.exit_code == 0
        assert "TRUNCATED" not in result.stdout
