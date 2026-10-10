"""Tests for ``trellis worker`` — config plumbing for tier-1 auto-promotion.

The store-touching behaviour of ``worker tune`` is exercised end-to-end in
``tests/unit/learning/tuners/test_auto_promote.py`` (the library it calls).
These tests pin the CLI-side contract: the ``learning.auto_promote`` config
section parses correctly, is absent-safe (disabled default), rejects
malformed input loudly, and never weakens the gate below the manual floor.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

from tests.cli_output import assert_coloured, force_colour, plain
from tests.document_recency import fake_document_clock
from tests.recovery_command import expected_recovery
from trellis.core.vector_metadata import vector_metadata_diverges
from trellis.errors import BackendNotInstalledError
from trellis.llm import LLMResponse, Message
from trellis.llm.routing import LLMConsumer
from trellis.ops.capture_health import check_capture_health, is_capture_surface
from trellis.ops.write_health import WriteHealthReport, summarize_write_health
from trellis.schemas.advisory import (
    Advisory,
    AdvisoryCategory,
    AdvisoryEvidence,
)
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import Outcome, Trace, TraceContext
from trellis.stores.advisory_source import (
    ADVISORY_FILENAME,
    ADVISORY_WRITER_SURFACE,
)
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli import worker
from trellis_cli.exit_codes import EXIT_STORE
from trellis_cli.main import app, worker_app
from trellis_cli.stores import _get_registry, _reset_registry
from trellis_workers.session_capture import sweep as capture_sweep
from trellis_workers.session_capture.models import CaptureReport

if TYPE_CHECKING:
    from click.testing import Result

runner = CliRunner()


def _write_config(config_dir: Path, body: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / worker.CONFIG_FILENAME).write_text(body, encoding="utf-8")


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Shared fixtures + stubs for the WP3 curate / enrich / mine-precedents tests.
# These point the CLI store getters at temp SQLite stores (same pattern as
# tests/unit/cli/test_analyze.py) and provide canned LLM clients.
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    """Point CLI stores at a temp directory and return the registry."""
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


def _seed_promote_signal(
    registry: StoreRegistry,
    *,
    item_id: str = "wc:doc:helpful",
    rounds: int = 3,
) -> None:
    """Emit ``rounds`` graded packs marking ``item_id`` helpful + successful.

    Produces both the learning-observation signal and the noise/effectiveness
    signal the curate cycle consumes.
    """
    event_log = registry.operational.event_log
    for i in range(rounds):
        pack_id = f"wc-pack-{i}"
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "intent": "test intent",
                "domain": "wc-test",
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


class _StubLLM:
    """LLMClient stub returning a canned ``LLMResponse``."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        return LLMResponse(content=self._content, model=model or "test-model")


def _make_feedback(*, outcome: str, items: list[str]):
    """Build a minimal PackFeedback for the JSONL audit log."""
    from trellis.feedback.models import PackFeedback

    return PackFeedback(
        run_id="run-1",
        phase="execute",
        intent="test intent",
        outcome=outcome,
        items_served=items,
    )


_CURATE_KEPT = "wc:doc:kept"
_CURATE_NOISY = "wc:doc:noisy"
#: A second real document, admitted and written alongside ``_CURATE_NOISY``
#: so the written and refused counts differ (2 vs 1) and a mutant that
#: swaps them, or inverts the dry-run predicate, fails on the count
#: itself rather than only incidentally on another assertion.
_CURATE_NOISY_2 = "wc:doc:noisy2"
#: A trace id, never put into the document store, used by
#: ``test_live_run_separates_written_from_refused_not_document`` to stand
#: in for an id the demotion gate admits on citation evidence alone
#: (#833).
_CURATE_PHANTOM = "wc:trace:phantom"
_CURATE_FINDINGS = frozenset(
    {"NoiseTagsApplied", "AdvisoryCycleReport", "LearningCandidatesReport"}
)


def _seed_curate_signal(registry: StoreRegistry) -> None:
    """Seed a store on which a live curate cycle writes in every stage.

    Five successful packs serve both docs and cite the noisy one unhelpful;
    five failed packs serve the noisy one alone. On this store a live cycle
    noise-tags the noisy doc, changes the advisory file, writes both review
    files and records one finding per stage, so a dry run that leaves each
    of them alone is evidence rather than an empty fixture.
    """
    event_log = registry.operational.event_log
    for outcome, served in (
        ("success", [_CURATE_KEPT, _CURATE_NOISY]),
        ("failure", [_CURATE_NOISY]),
    ):
        for i in range(5):
            pack_id = f"wc-curate-{outcome}-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "intent": "test intent",
                    "intent_family": "wc-curate",
                    "domain": "wc-test",
                    "injected_item_ids": served,
                    "injected_items": [
                        {
                            "item_id": item_id,
                            "item_type": "document",
                            "rank": rank,
                            "strategy_source": "document",
                        }
                        for rank, item_id in enumerate(served)
                    ],
                },
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "pack_id": pack_id,
                    "run_id": f"wc-run-{outcome}-{i}",
                    "intent_family": "wc-curate",
                    "outcome": outcome,
                    "success": outcome == "success",
                    "helpful_item_ids": [_CURATE_KEPT] if outcome == "success" else [],
                    "unhelpful_item_ids": [_CURATE_NOISY],
                },
            )
    registry.knowledge.document_store.put(
        _CURATE_NOISY,
        "noisy test content",
        {"content_tags": {"signal_quality": "standard"}},
    )


def _seed_second_noisy_document(registry: StoreRegistry, pack_prefix: str) -> None:
    """Seed a second real document the gate admits and the writer resolves.

    Mirrors ``_seed_curate_signal``'s evidence shape for ``_CURATE_NOISY``
    but for ``_CURATE_NOISY_2``, so a live or dry curate cycle alongside a
    refused non-document id writes (or previews) two documents, not one.
    """
    event_log = registry.operational.event_log
    for i in range(5):
        pack_id = f"{pack_prefix}-{i}"
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "intent": "test intent",
                "intent_family": "wc-curate",
                "domain": "wc-test",
                "injected_item_ids": [_CURATE_NOISY_2],
                "injected_items": [
                    {
                        "item_id": _CURATE_NOISY_2,
                        "item_type": "document",
                        "rank": 0,
                        "strategy_source": "document",
                    }
                ],
            },
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "pack_id": pack_id,
                "run_id": f"{pack_prefix}-run-{i}",
                "intent_family": "wc-curate",
                "outcome": "failure",
                "success": False,
                "helpful_item_ids": [],
                "unhelpful_item_ids": [_CURATE_NOISY_2],
            },
        )
    registry.knowledge.document_store.put(
        _CURATE_NOISY_2,
        "second noisy test content",
        {"content_tags": {"signal_quality": "standard"}},
    )


def _graph_shape(registry: StoreRegistry) -> dict[str, int]:
    """Current nodes counted by type, plus the edge total under ``"edges"``."""
    graph = registry.knowledge.graph_store
    shape = {"edges": graph.count_edges()}
    for node in graph.query(limit=1000):
        shape[node["node_type"]] = shape.get(node["node_type"], 0) + 1
    return shape


def _file_state(path: Path) -> bytes | None:
    """``path``'s bytes, or ``None`` while it does not exist."""
    return path.read_bytes() if path.exists() else None


# ---------------------------------------------------------------------------
# worker_app moved here from main; tune is its sole subcommand today.
# ---------------------------------------------------------------------------


def test_worker_app_exposes_tune() -> None:
    names = {
        cmd.name or cmd.callback.__name__ for cmd in worker_app.registered_commands
    }
    assert "tune" in names


def test_main_imports_worker_app_from_module() -> None:
    # worker_app on main is the same object defined in trellis_cli.worker.
    assert worker_app is worker.worker_app


# ---------------------------------------------------------------------------
# Config absent => disabled default (global default OFF).
# ---------------------------------------------------------------------------


def test_absent_config_yields_disabled_policy(config_dir: Path) -> None:
    policy = worker._build_auto_promote_policy()
    assert policy.enabled is False
    # Still armed with monitoring, still stricter than manual.
    assert policy.post_promotion.auto_demote is True
    assert policy.min_sample_size >= 30


def test_section_absent_yields_disabled_policy(config_dir: Path) -> None:
    _write_config(config_dir, "learning:\n  scoring:\n    foo: 1\n")
    policy = worker._build_auto_promote_policy()
    assert policy.enabled is False


# ---------------------------------------------------------------------------
# Config present and well-formed.
# ---------------------------------------------------------------------------


def test_enabled_config_parses(config_dir: Path) -> None:
    _write_config(
        config_dir,
        "learning:\n"
        "  auto_promote:\n"
        "    enabled: true\n"
        "    min_sample_size: 50\n"
        "    min_effect_size: 0.30\n"
        "    post_min_samples: 40\n"
        "    post_regression_threshold: 0.15\n"
        "    post_lookback_days: 14\n",
    )
    policy = worker._build_auto_promote_policy()
    assert policy.enabled is True
    assert policy.min_sample_size == 50
    assert policy.min_effect_size == 0.30
    assert policy.post_promotion.min_samples_post_promote == 40
    assert policy.post_promotion.regression_threshold == 0.15
    assert policy.post_promotion.lookback_window.days == 14
    assert policy.post_promotion.auto_demote is True


def test_partial_config_uses_defaults(config_dir: Path) -> None:
    _write_config(config_dir, "learning:\n  auto_promote:\n    enabled: true\n")
    policy = worker._build_auto_promote_policy()
    assert policy.enabled is True
    assert policy.min_sample_size == 30  # default
    assert policy.min_effect_size == 0.25  # default


# ---------------------------------------------------------------------------
# Loud on malformed input.
# ---------------------------------------------------------------------------


def test_unknown_key_rejected(config_dir: Path) -> None:
    _write_config(
        config_dir,
        "learning:\n  auto_promote:\n    enabled: true\n    bogus: 1\n",
    )
    with pytest.raises(typer.BadParameter, match="unknown key"):
        worker._build_auto_promote_policy()


def test_non_bool_enabled_rejected(config_dir: Path) -> None:
    _write_config(config_dir, "learning:\n  auto_promote:\n    enabled: yesplease\n")
    with pytest.raises(typer.BadParameter, match="true/false"):
        worker._build_auto_promote_policy()


def test_non_numeric_threshold_rejected(config_dir: Path) -> None:
    _write_config(
        config_dir,
        "learning:\n  auto_promote:\n    min_effect_size: abc\n",
    )
    with pytest.raises(typer.BadParameter, match="not a number"):
        worker._build_auto_promote_policy()


def test_section_not_mapping_rejected(config_dir: Path) -> None:
    _write_config(config_dir, "learning:\n  auto_promote: 7\n")
    with pytest.raises(typer.BadParameter, match="must be a mapping"):
        worker._build_auto_promote_policy()


def test_looser_than_manual_rejected_via_exit(config_dir: Path) -> None:
    # min_sample_size below the manual floor (5) must be rejected — the
    # AutoPromotePolicy constructor raises ValueError, surfaced as Exit.
    _write_config(
        config_dir,
        "learning:\n  auto_promote:\n    enabled: true\n    min_sample_size: 2\n",
    )
    with pytest.raises(typer.Exit):
        worker._build_auto_promote_policy_or_exit()


# ===========================================================================
# worker curate — full cycle (WP3)
# ===========================================================================


class TestWorkerCurate:
    def test_curation_cycle_requires_an_explicit_vector_store(self) -> None:
        """``vector_store`` must have no default — omission has to be loud.

        #381: the nightly cron is the only *automated* demotion path, and
        it called ``run_effectiveness_feedback`` without a vector store for
        the whole life of #338's fix. Every tag it wrote reached the
        document store and no vector row, so the semantic axis — 65% of
        injected tokens — kept serving the pre-demotion snapshot.

        A default of ``None`` is what made that omission invisible, so the
        parameter is required keyword-only. ``None`` remains a legal
        *value* (a deployment may have no vector store); what is not legal
        is declining to say. This test exists so the next person who hits
        the ``TypeError`` fixes the call site rather than the signature.
        """
        import inspect

        params = inspect.signature(worker.run_curation_cycle).parameters
        assert "vector_store" in params, (
            "run_curation_cycle must accept a vector_store — without it the "
            "nightly demotion cannot reach the semantic axis (#381)"
        )
        vector_store = params["vector_store"]
        assert vector_store.kind is inspect.Parameter.KEYWORD_ONLY
        assert vector_store.default is inspect.Parameter.empty, (
            "vector_store must not default to None — that is exactly how "
            "#381 stayed invisible. Pass resolve_vector_store(registry), or "
            "an explicit None on a deployment that has no vector store."
        )

    def test_worker_app_exposes_new_subcommands(self) -> None:
        names = {
            cmd.name or cmd.callback.__name__ for cmd in worker_app.registered_commands
        }
        assert {"curate", "enrich", "mine-precedents", "capture-sessions"} <= names

    def test_full_cycle_happy_path(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        _seed_promote_signal(temp_stores)
        out_dir = tmp_path / "review"
        result = runner.invoke(
            app,
            ["worker", "curate", "--output-dir", str(out_dir), "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        assert data["dry_run"] is False
        assert data["learning_observations"] >= 3
        assert data["learning_candidates"] >= 1
        # Promote-half artifacts are written for human review.
        assert data["candidates_path"] is not None
        assert Path(data["candidates_path"]).exists()
        # Promotable digest (#845): this seed's one candidate has 3
        # helpful citations and 0 unhelpful, so it is promotable end to
        # end through the real JSON CLI payload, not just the unit tests
        # on the pure digest builder.
        promotable = data["learning_promotable"]
        assert promotable["count"] == 1
        assert len(promotable["top"]) == 1
        top_row = promotable["top"][0]
        assert top_row["helpful_count"] == 3
        assert top_row["times_served"] == 3
        assert top_row["success_rate"] == 1.0
        # The item ("wc:doc:helpful") was only ever referenced in event
        # payloads, never put into the document store, so the
        # readable-name fallback can't resolve it and the bare item id
        # survives in the name.
        assert top_row["precedent_name"].endswith("wc:doc:helpful")
        candidates_payload = json.loads(Path(data["candidates_path"]).read_text())
        assert candidates_payload["promotable"] == promotable
        assert Path(data["decisions_path"]).exists()
        assert data["skipped_stages"] == []

    def test_full_cycle_names_title_less_candidate_from_its_document(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        # The nightly path must hand its document store to the artifact
        # write (#845): a title-less candidate whose item resolves carries
        # the readable name into the digest and the decisions template.
        temp_stores.knowledge.document_store.put(
            "wc:doc:named",
            "---\nkind: note\n---\n# Readable Doc Heading\n\nBody line.",
            {},
        )
        _seed_promote_signal(temp_stores, item_id="wc:doc:named")
        out_dir = tmp_path / "review"
        result = runner.invoke(
            app,
            ["worker", "curate", "--output-dir", str(out_dir), "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        top_row = data["learning_promotable"]["top"][0]
        name = top_row["precedent_name"]
        assert name.endswith(":: Readable Doc Heading"), name
        decisions = json.loads(Path(data["decisions_path"]).read_text("utf-8"))
        by_id = {d["candidate_id"]: d["promotion_name"] for d in decisions["decisions"]}
        assert by_id[top_row["candidate_id"]] == name

    def test_skip_noise_tags(self, tmp_path: Path, temp_stores: StoreRegistry) -> None:
        _seed_promote_signal(temp_stores)
        out_dir = tmp_path / "review"
        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(out_dir),
                "--skip-noise-tags",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert "noise_tags" in data["skipped_stages"]
        assert data["noise_tagged"] == 0
        # Other stages still ran.
        assert data["learning_candidates"] >= 1

    def test_skip_advisories(self, tmp_path: Path, temp_stores: StoreRegistry) -> None:
        _seed_promote_signal(temp_stores)
        out_dir = tmp_path / "review"
        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(out_dir),
                "--skip-advisories",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert "advisories" in data["skipped_stages"]
        assert data["advisories_generated"] == 0

    def test_skip_learning(self, tmp_path: Path, temp_stores: StoreRegistry) -> None:
        _seed_promote_signal(temp_stores)
        out_dir = tmp_path / "review"
        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(out_dir),
                "--skip-learning",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert "learning" in data["skipped_stages"]
        assert data["learning_candidates"] == 0
        assert data["candidates_path"] is None
        # No artifacts written when the learning stage is skipped.
        assert not (out_dir / "intent_learning_candidates.json").exists()

    @pytest.mark.parametrize("output_format", ["json", "text"])
    @pytest.mark.parametrize(
        ("meta_args", "graph_after"),
        [
            # Each stage that runs records its Activity, as every other
            # meta-traced dry run does. No stage records a finding.
            ([], {"edges": 2, "Agent": 1, "Activity": 2}),
            # The documented way to a dry run that writes nothing at all.
            (["--no-meta-trace"], {"edges": 0}),
        ],
        ids=["meta-trace", "no-meta-trace"],
    )
    def test_dry_run_writes_no_finding_and_nothing_else(
        self,
        tmp_path: Path,
        temp_stores: StoreRegistry,
        output_format: str,
        meta_args: list[str],
        graph_after: dict[str, int],
    ) -> None:
        """``--dry-run`` writes nothing but the meta-Activities its help names.

        It recorded a ``NoiseTagsApplied`` and a ``LearningCandidatesReport``
        finding for tags and review files it never wrote. A live cycle on
        this seed writes in every stage (pinned by
        ``test_live_run_keeps_meta_trace_and_reconcile``), so the graph,
        advisory, document and review-file checks below each have something
        to catch. The event count is only a guard: a live cycle on this seed
        emits no event either.
        """
        _seed_curate_signal(temp_stores)
        out_dir = tmp_path / "review"
        advisory_path = Path(temp_stores.stores_dir) / ADVISORY_FILENAME
        events_before = temp_stores.operational.event_log.count()
        advisories_before = _file_state(advisory_path)
        noisy_before = temp_stores.knowledge.document_store.get(_CURATE_NOISY)

        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(out_dir),
                "--dry-run",
                *meta_args,
                "--format",
                output_format,
            ],
        )

        assert result.exit_code == 0, result.output
        if output_format == "json":
            data = json.loads(result.stdout.strip())
            assert data["dry_run"] is True
            # Advisories are skipped wholesale in dry-run (they mutate the store).
            assert "advisories" in data["skipped_stages"]
            assert data["candidates_path"] is None
            # A live run would have tagged and written candidates here.
            assert data["noise_tagged"] >= 1
            assert data["learning_candidates"] >= 1
        assert _graph_shape(temp_stores) == graph_after
        assert temp_stores.operational.event_log.count() == events_before
        assert _file_state(advisory_path) == advisories_before
        assert temp_stores.knowledge.document_store.get(_CURATE_NOISY) == noisy_before
        assert not out_dir.exists()

    def test_reconcile_first_backfills(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        # Write a pack_feedback.jsonl row that is NOT yet in the event log.
        from trellis.feedback.recording import record_feedback

        data_dir = Path(temp_stores.stores_dir).parent
        record_feedback(
            _make_feedback(outcome="success", items=["x"]),
            log_dir=data_dir,
        )
        out_dir = tmp_path / "review"
        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(out_dir),
                "--reconcile-first",
                "--skip-advisories",
                "--skip-learning",
                "--skip-noise-tags",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        # The reconcile pass should have emitted the missing FEEDBACK_RECORDED.
        fb_events = temp_stores.operational.event_log.get_events(
            event_type=EventType.FEEDBACK_RECORDED, limit=10
        )
        assert len(fb_events) >= 1

    @staticmethod
    def _invoke_curate(out_dir: Path) -> Result:
        """Run one reconcile-first curate cycle with the analyses skipped."""
        return runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(out_dir),
                "--reconcile-first",
                "--skip-advisories",
                "--skip-learning",
                "--skip-noise-tags",
                "--format",
                "json",
            ],
        )

    def test_reconcile_first_backfills_stores_feedback_dir(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """The MCP / REST surfaces write to ``<stores_dir>/feedback``.

        Rows landing there are the common case in a live deployment, so
        the cycle has to scan that directory and not only ``<data_dir>``.
        """
        from trellis.feedback.recording import feedback_log_dir, record_feedback

        registry = _get_registry()
        assert registry.stores_dir is not None
        record_feedback(
            _make_feedback(outcome="success", items=["x"]),
            log_dir=feedback_log_dir(registry.stores_dir),
        )
        result = self._invoke_curate(tmp_path / "review")
        assert result.exit_code == 0, result.output
        fb_events = temp_stores.operational.event_log.get_events(
            event_type=EventType.FEEDBACK_RECORDED, limit=10
        )
        assert len(fb_events) == 1

    def test_reconcile_first_honours_config_yaml_data_dir(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """``config.yaml``'s ``data_dir`` beats ``$TRELLIS_DATA_DIR``.

        ``trellis admin init --data-dir`` always writes that key, and
        ``StoreRegistry.from_config_dir`` lets it override the
        environment — so the writers land the row under the *config's*
        stores dir. A worker that re-derived the path from the
        environment would scan an empty directory and log a clean no-op,
        which is indistinguishable from "there was nothing to do".
        """
        from trellis.feedback.recording import feedback_log_dir, record_feedback

        custom_data = tmp_path / "custom-data"
        (custom_data / "stores").mkdir(parents=True)
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(
            f"data_dir: {custom_data}\n", encoding="utf-8"
        )
        _reset_registry()

        registry = _get_registry()
        # Precondition: the two derivations genuinely disagree, so the
        # assertion below can actually fail if the worker uses the wrong one.
        assert registry.stores_dir == custom_data / "stores"
        assert registry.stores_dir != Path(str(temp_stores.stores_dir))

        record_feedback(
            _make_feedback(outcome="success", items=["x"]),
            log_dir=feedback_log_dir(registry.stores_dir),
        )
        result = self._invoke_curate(tmp_path / "review")
        assert result.exit_code == 0, result.output
        fb_events = registry.operational.event_log.get_events(
            event_type=EventType.FEEDBACK_RECORDED, limit=10
        )
        assert len(fb_events) == 1

    def test_reconcile_first_recovers_pack_association(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """A replayed row keeps the pack it was recorded against.

        The MCP/REST writers stamp ``metadata['pack_id']`` precisely so a
        soft-failed emit can be replayed with the association the
        advisory/effectiveness joins need.
        """
        from trellis.feedback.models import PackFeedback
        from trellis.feedback.recording import feedback_log_dir, record_feedback

        registry = _get_registry()
        assert registry.stores_dir is not None
        record_feedback(
            PackFeedback.from_agent_signal(
                run_id="pack-assoc", rating=0.3, pack_id="pack-assoc"
            ),
            log_dir=feedback_log_dir(registry.stores_dir),
        )
        result = self._invoke_curate(tmp_path / "review")
        assert result.exit_code == 0, result.output
        (event,) = temp_stores.operational.event_log.get_events(
            event_type=EventType.FEEDBACK_RECORDED, limit=10
        )
        assert event.entity_id == "pack-assoc"
        assert event.entity_type == "pack"
        assert event.payload["pack_id"] == "pack-assoc"
        assert event.payload["rating"] == 0.3

    @pytest.mark.parametrize("output_format", ["json", "text"])
    @pytest.mark.parametrize(
        "loop_args", [[], ["--interval", "60"]], ids=["one-shot", "interval"]
    )
    def test_dry_run_refuses_reconcile_first(
        self,
        tmp_path: Path,
        temp_stores: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
        output_format: str,
        loop_args: list[str],
    ) -> None:
        """``--dry-run --reconcile-first`` exits 2 before anything is written.

        Reconcile emits a ``FEEDBACK_RECORDED`` event per feedback row the
        EventLog lacks, which is a write the dry run promises not to make.
        The ``--interval`` path reconciles before its first cycle too, so
        the refusal must sit above both.
        """
        from trellis.feedback.recording import feedback_log_dir, record_feedback

        def _no_loop(*_args: object, **_kwargs: object) -> None:
            msg = "the refusal must come before the loop"
            raise AssertionError(msg)

        # Also keeps the pre-fix run from sleeping in the loop forever.
        monkeypatch.setattr(worker, "_run_curate_loop", _no_loop)
        log_dir = feedback_log_dir(Path(temp_stores.stores_dir))
        record_feedback(_make_feedback(outcome="success", items=["x"]), log_dir=log_dir)
        log_before = {path: path.read_bytes() for path in log_dir.iterdir()}
        events_before = temp_stores.operational.event_log.count()
        out_dir = tmp_path / "review"

        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(out_dir),
                "--dry-run",
                "--reconcile-first",
                *loop_args,
                "--format",
                output_format,
            ],
        )

        assert result.exit_code == 2, result.output
        assert result.stdout == ""
        # The refusal names the read-only preview that already exists.
        assert "reconcile-feedback" in plain(result.output)
        assert {path: path.read_bytes() for path in log_dir.iterdir()} == log_before
        assert temp_stores.operational.event_log.count() == events_before
        assert _graph_shape(temp_stores) == {"edges": 0}
        assert not out_dir.exists()

    @pytest.mark.parametrize("output_format", ["json", "text"])
    def test_live_run_keeps_meta_trace_and_reconcile(
        self, tmp_path: Path, temp_stores: StoreRegistry, output_format: str
    ) -> None:
        """A live ``--reconcile-first`` run, the documented nightly shape, is unchanged.

        It reconciles, records every stage's Activity and finding, and
        writes in all three stages. That last part is also what gives the
        dry-run test's write checks something to catch.
        """
        from trellis.feedback.recording import feedback_log_dir, record_feedback

        _seed_curate_signal(temp_stores)
        record_feedback(
            _make_feedback(outcome="success", items=["x"]),
            log_dir=feedback_log_dir(Path(temp_stores.stores_dir)),
        )
        advisory_path = Path(temp_stores.stores_dir) / ADVISORY_FILENAME
        advisories_before = _file_state(advisory_path)
        out_dir = tmp_path / "review"

        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(out_dir),
                "--reconcile-first",
                "--format",
                output_format,
            ],
        )

        assert result.exit_code == 0, result.output
        reconciled = [
            event
            for event in temp_stores.operational.event_log.get_events(
                event_type=EventType.FEEDBACK_RECORDED, limit=100
            )
            if event.source == "feedback.reconcile"
        ]
        assert len(reconciled) == 1
        shape = _graph_shape(temp_stores)
        assert shape["Activity"] == 3
        assert {kind: shape.get(kind, 0) for kind in _CURATE_FINDINGS} == dict.fromkeys(
            _CURATE_FINDINGS, 1
        )
        assert _file_state(advisory_path) != advisories_before
        noisy = temp_stores.knowledge.document_store.get(_CURATE_NOISY)
        assert noisy is not None
        assert noisy["metadata"]["content_tags"]["signal_quality"] == "noise"
        assert (out_dir / "intent_learning_candidates.json").is_file()
        assert (out_dir / "promotion_decisions.template.json").is_file()

    def test_live_run_separates_written_from_refused_not_document(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """``noise_tagged`` counts writes, not gate admissions.

        The demotion gate admits on citation evidence alone, with no
        notion of which store an id belongs to. A phantom id cited
        unhelpful just as often as the real noisy documents is admitted
        right alongside them, but it was never put into the document
        store, so ``apply_noise_tags`` writes nothing for it. A second
        real document is seeded beside ``_CURATE_NOISY`` so the written
        (2) and refused (1) counts differ: before the fix, ``noise_tagged``
        reported all three as demoted (3); it has to report 2 and name
        the other refused (#833).
        """
        _seed_curate_signal(temp_stores)
        _seed_second_noisy_document(temp_stores, "wc-curate-noisy2")
        event_log = temp_stores.operational.event_log
        for i in range(5):
            pack_id = f"wc-curate-phantom-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "intent": "test intent",
                    "intent_family": "wc-curate",
                    "domain": "wc-test",
                    "injected_item_ids": [_CURATE_PHANTOM],
                    "injected_items": [
                        {
                            "item_id": _CURATE_PHANTOM,
                            "item_type": "trace",
                            "rank": 0,
                            "strategy_source": "document",
                        }
                    ],
                },
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "pack_id": pack_id,
                    "run_id": f"wc-run-phantom-{i}",
                    "intent_family": "wc-curate",
                    "outcome": "failure",
                    "success": False,
                    "helpful_item_ids": [],
                    "unhelpful_item_ids": [_CURATE_PHANTOM],
                },
            )
        # Deliberately never put _CURATE_PHANTOM into the document
        # store — it stands in for a trace id or other non-document id
        # the gate still admits.
        assert temp_stores.knowledge.document_store.get(_CURATE_PHANTOM) is None

        out_dir = tmp_path / "review"
        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(out_dir),
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["noise_tagged"] == 2
        assert data["noise_refused_non_document"] == 1
        noisy = temp_stores.knowledge.document_store.get(_CURATE_NOISY)
        assert noisy is not None
        assert noisy["metadata"]["content_tags"]["signal_quality"] == "noise"
        noisy2 = temp_stores.knowledge.document_store.get(_CURATE_NOISY_2)
        assert noisy2 is not None
        assert noisy2["metadata"]["content_tags"]["signal_quality"] == "noise"

    def test_dry_run_separates_written_from_refused_not_document(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """A dry run previews the same split, from a read, not a write.

        Mirrors ``test_live_run_separates_written_from_refused_not_document``
        but with ``--dry-run``: nothing is written either way, so
        ``_curate_stage_noise_tags`` recomputes both counts with a
        read-only ``document_store.get`` check per admitted id instead of
        reading ``report.noise_tags_written`` (which a dry run never sets).
        A second real document is seeded beside ``_CURATE_NOISY`` so the
        would-be-written (2) and refused (1) counts differ. Before that
        recomputation existed, a dry run reported the gate's raw
        admission count (3) under ``noise_tagged`` with no way to see
        that one of the three would refuse; it has to report 2 and 1,
        same as the live run would, without writing anything.
        """
        _seed_curate_signal(temp_stores)
        _seed_second_noisy_document(temp_stores, "wc-curate-noisy2-dry")
        event_log = temp_stores.operational.event_log
        for i in range(5):
            pack_id = f"wc-curate-phantom-dry-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "intent": "test intent",
                    "intent_family": "wc-curate",
                    "domain": "wc-test",
                    "injected_item_ids": [_CURATE_PHANTOM],
                    "injected_items": [
                        {
                            "item_id": _CURATE_PHANTOM,
                            "item_type": "trace",
                            "rank": 0,
                            "strategy_source": "document",
                        }
                    ],
                },
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "pack_id": pack_id,
                    "run_id": f"wc-run-phantom-dry-{i}",
                    "intent_family": "wc-curate",
                    "outcome": "failure",
                    "success": False,
                    "helpful_item_ids": [],
                    "unhelpful_item_ids": [_CURATE_PHANTOM],
                },
            )
        assert temp_stores.knowledge.document_store.get(_CURATE_PHANTOM) is None
        noisy_before = temp_stores.knowledge.document_store.get(_CURATE_NOISY)
        noisy2_before = temp_stores.knowledge.document_store.get(_CURATE_NOISY_2)

        out_dir = tmp_path / "review"
        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(out_dir),
                "--dry-run",
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["dry_run"] is True
        assert data["noise_tagged"] == 2
        assert data["noise_refused_non_document"] == 1
        # A dry run writes nothing — the split above is a preview.
        docs = temp_stores.knowledge.document_store
        assert docs.get(_CURATE_NOISY) == noisy_before
        assert docs.get(_CURATE_NOISY_2) == noisy2_before
        assert docs.get(_CURATE_PHANTOM) is None
        assert not out_dir.exists()


# ===========================================================================
# worker curate --interval — loop mode (WP3)
# ===========================================================================


class TestCurateSurvivesADegradedAdvisoryStore:
    """#393 — the nightly surface is where a corrupt file goes unnoticed.

    Two failures live here. The fitness loop's ``put`` / ``suppress`` /
    ``restore`` all raise on a degraded store, so an unguarded cycle dies
    mid-stage and takes the learning stage with it. And a cycle that simply
    reported zeros for the advisory counts would be indistinguishable from
    a quiet night — which is how a corrupt file survives for weeks.
    """

    @staticmethod
    def _corrupt_advisory_file(tmp_path: Path) -> Path:
        """Write an unreadable advisories.json where the CLI will resolve it."""
        path = tmp_path / "data" / "stores" / ADVISORY_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"advisories": [ torn write', encoding="utf-8")
        return path

    def test_the_cycle_completes_and_the_file_is_untouched(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        _seed_promote_signal(temp_stores)
        path = self._corrupt_advisory_file(tmp_path)
        before = path.read_text(encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(tmp_path / "review"),
                "--format",
                "json",
            ],
        )

        # Non-zero, and the same ``EXIT_STORE`` the ``analyze`` advisory
        # surfaces use (#448, #489). The JSON headline below and this code
        # have to agree — #437 is what happens when they do not.
        assert result.exit_code == EXIT_STORE, result.output
        data = json.loads(result.stdout.strip())
        # The headline, not just the body: a wrapper reading only ``status``
        # would otherwise record a clean nightly run (#393).
        assert data["status"] == "degraded"
        assert data["advisory_store_degraded"] is not None
        assert data["advisory_store_degraded"]["reason"] == "malformed_json"
        assert data["advisory_store_degraded"]["recovery"] == expected_recovery(path)
        assert "advisories" in data["skipped_stages"]
        # The rest of the cycle still ran — this is a skip, not a crash.
        assert data["learning_observations"] >= 3
        assert path.read_text(encoding="utf-8") == before

    def test_the_text_surface_says_so_too(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """A warning honest only in ``--format json`` is a warning nobody reads."""
        _seed_promote_signal(temp_stores)
        path = self._corrupt_advisory_file(tmp_path)

        result = runner.invoke(
            app,
            ["worker", "curate", "--output-dir", str(tmp_path / "review")],
        )

        assert result.exit_code == EXIT_STORE, result.output
        rendered = plain(result.output)
        assert "ADVISORY STORE DEGRADED" in rendered
        assert expected_recovery(path) in rendered.replace("\n", "")

    def test_a_clean_cycle_carries_no_degradation(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """The field must stay ``None`` on the happy path, or it means nothing."""
        _seed_promote_signal(temp_stores)

        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(tmp_path / "review"),
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["advisory_store_degraded"] is None
        assert data["status"] == "ok"

    def test_a_clean_text_cycle_prints_no_banner(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """The negative control for the *text* renderer.

        Asserting the banner's absence from a ``--format json`` run is
        vacuous — that renderer never runs there.
        """
        _seed_promote_signal(temp_stores)

        result = runner.invoke(
            app, ["worker", "curate", "--output-dir", str(tmp_path / "review")]
        )

        assert result.exit_code == 0, result.output
        assert "ADVISORY STORE DEGRADED" not in plain(result.output)

    def test_a_dry_run_still_reports_the_degradation(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """A dry run is the natural "is my nightly healthy?" probe.

        The advisory stages are skipped on a dry run anyway, so a guard
        keyed on "were we going to write?" reported a perfectly clean
        cycle — silent in exactly the command an operator runs to look for
        this.
        """
        _seed_promote_signal(temp_stores)
        self._corrupt_advisory_file(tmp_path)

        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(tmp_path / "review"),
                "--dry-run",
                "--format",
                "json",
            ],
        )

        assert result.exit_code == EXIT_STORE, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "degraded"
        assert data["advisory_store_degraded"]["reason"] == "malformed_json"

    def test_a_bracketed_path_survives_the_banner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rich eats ``[...]``, and the recovery command is the whole point.

        An unescaped path under ``/tmp/d [staging]/`` renders the fix as
        ``mv /tmp/d /data/...`` — a command that does not run, printed to
        an operator as the thing to type. Silently: nothing errors.

        Colour is **forced**, and that is the half this test was missing
        (#495). Reading raw ``result.output`` made it fail on a coloured
        build with its own "Rich ate the bracketed path" message, which
        sends the next reader after a renderer that is not broken. It now
        asserts three things: colour really happened, the bracketed segment
        survived it, and the printed command still runs.

        The bracketed segment on its own was **not** enough, and that is
        measured rather than argued: deleting the ``escape()`` around the
        recovery command leaves this test green on ``origin/main``, because
        ``[staging]`` still appears in the separately-escaped ``file:``
        line printed above it. The shell-parse below is what kills that
        mutant.
        """
        import shlex

        force_colour(monkeypatch, worker)
        data_dir = tmp_path / "d [staging]" / "data"
        (data_dir / "stores").mkdir(parents=True)
        advisory_file = data_dir / "stores" / ADVISORY_FILENAME
        advisory_file.write_text('{"advisories": [ torn', encoding="utf-8")
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
        _reset_registry()

        result = runner.invoke(
            app, ["worker", "curate", "--output-dir", str(tmp_path / "review")]
        )

        # 1. The coloured branch really ran.
        rendered = assert_coloured(result.output)
        assert "ADVISORY STORE DEGRADED" in rendered
        # 2. The bracketed segment survived rendering.
        assert "[staging]" in rendered, (
            "Rich ate the bracketed path segment, so the recovery command "
            "printed to the operator does not run"
        )
        # 3. And the copied line is still one runnable command with exactly
        #    two operands — brackets intact is necessary, not sufficient.
        line = next(
            ln for ln in rendered.splitlines() if ln.strip().startswith("To reset:")
        )
        command = line.split("To reset:", 1)[1].strip()
        assert shlex.split(command) == [
            "mv",
            str(advisory_file),
            f"{advisory_file}.corrupt",
        ], f"the printed recovery command does not parse as `mv src dst`: {command!r}"


class TestWorkerCurateLoop:
    def test_loop_runs_n_cycles_then_stops(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """The factored loop body runs a bounded number of cycles.

        Driven directly (not through CliRunner) with ``max_cycles`` so it
        does not sleep through real intervals or touch process signals.
        """
        _seed_promote_signal(temp_stores)
        out_dir = tmp_path / "review"
        calls: list[int] = []
        flag = worker._ShutdownFlag()

        original = worker.run_curation_cycle

        def _counting_cycle(**kwargs: object) -> worker.CurateCycleResult:
            calls.append(1)
            return original(**kwargs)  # type: ignore[arg-type]

        worker.run_curation_cycle = _counting_cycle  # type: ignore[assignment]
        try:
            worker._run_curate_loop(
                interval=1,
                output_dir=out_dir,
                days=30,
                dry_run=False,
                skip_noise_tags=False,
                skip_advisories=False,
                skip_learning=False,
                no_meta_trace=True,
                output_format="json",
                max_cycles=3,
                shutdown=flag,
            )
        finally:
            worker.run_curation_cycle = original  # type: ignore[assignment]

        assert len(calls) == 3

    def test_loop_stops_on_shutdown_flag(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """A pre-set shutdown flag short-circuits the loop before any cycle."""
        out_dir = tmp_path / "review"
        flag = worker._ShutdownFlag()
        flag.stop = True

        calls: list[int] = []
        original = worker.run_curation_cycle

        def _counting_cycle(**kwargs: object) -> worker.CurateCycleResult:
            calls.append(1)
            return original(**kwargs)  # type: ignore[arg-type]

        worker.run_curation_cycle = _counting_cycle  # type: ignore[assignment]
        try:
            worker._run_curate_loop(
                interval=1,
                output_dir=out_dir,
                days=30,
                dry_run=False,
                skip_noise_tags=False,
                skip_advisories=False,
                skip_learning=False,
                no_meta_trace=True,
                output_format="json",
                max_cycles=5,
                shutdown=flag,
            )
        finally:
            worker.run_curation_cycle = original  # type: ignore[assignment]

        assert calls == []

    def test_shutdown_flag_request_sets_stop(self) -> None:
        flag = worker._ShutdownFlag()
        assert flag.stop is False
        flag.request(2, None)  # SIGINT
        assert flag.stop is True

    def test_interval_zero_rejected(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        out_dir = tmp_path / "review"
        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(out_dir),
                "--interval",
                "0",
            ],
        )
        assert result.exit_code != 0


# ===========================================================================
# worker enrich — loud failure without LLM (WP3)
# ===========================================================================


class TestWorkerEnrich:
    def test_loud_failure_without_llm_config(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        # No llm: block configured => no client => loud non-zero exit.
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == worker.EXIT_INTERNAL
        assert "LLM" in result.output

    @pytest.mark.parametrize("colour", [False, True], ids=["plain", "colour"])
    def test_the_missing_sdk_hint_keeps_its_extra(
        self,
        temp_stores: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
        colour: bool,
    ) -> None:
        """Rich read ``[llm-openai]`` as a markup tag and deleted it.

        Both install lines lost it: the hint's own, and the one
        ``BackendNotInstalledError`` puts in its message, so neither
        installed the SDK the error is about.
        """
        registry = MagicMock()
        registry.build_llm_client.side_effect = BackendNotInstalledError(
            backend_name="openai", extra="llm-openai"
        )
        monkeypatch.setattr(worker, "_get_registry", lambda: registry)
        if colour:
            force_colour(monkeypatch, worker)

        result = runner.invoke(app, ["worker", "enrich"])

        assert result.exit_code == worker.EXIT_INTERNAL, result.output
        text = assert_coloured(result.stdout) if colour else plain(result.stdout)
        text = " ".join(text.split())
        assert "'uv pip install trellis-ai[llm-openai]'" in text
        assert 'Run: uv pip install -e ".[llm-openai]"' in text

    @pytest.mark.parametrize("colour", [False, True], ids=["plain", "colour"])
    def test_the_no_client_hint_names_both_extras(
        self,
        temp_stores: StoreRegistry,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        colour: bool,
    ) -> None:
        """Rich read both extras as markup tags, leaving ``extra ( / ).``.

        The hint names the config.yaml the CLI reads, not ``~/.trellis``: a
        ``[dev]`` in its dir name is deleted the same way unless escaped, and
        ``COLUMNS`` keeps the tmp path on one line.
        """
        config_dir = tmp_path / "cfg[dev]"
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("COLUMNS", "500")
        registry = MagicMock()
        registry.build_llm_client.return_value = None
        monkeypatch.setattr(worker, "_get_registry", lambda: registry)
        if colour:
            force_colour(monkeypatch, worker)

        result = runner.invoke(app, ["worker", "enrich"])

        assert result.exit_code == worker.EXIT_INTERNAL, result.output
        text = assert_coloured(result.stdout) if colour else plain(result.stdout)
        text = " ".join(text.split())
        assert "the matching extra ([llm-openai] / [llm-anthropic])." in text
        assert f"Add an 'llm:' block to {config_dir / 'config.yaml'} (provider," in text
        assert "~/.trellis" not in text

    @pytest.mark.parametrize("colour", [False, True], ids=["plain", "colour"])
    def test_the_help_names_both_extras(
        self, monkeypatch: pytest.MonkeyPatch, colour: bool
    ) -> None:
        """Typer renders this docstring as Rich markup, which deleted both extras.

        ``--help`` showed an empty pair of double backticks where each one
        belonged. Typer's help console colours from the ``FORCE_COLOR`` that
        ``force_colour`` sets.
        """
        if colour:
            force_colour(monkeypatch, worker)

        result = runner.invoke(app, ["worker", "enrich", "--help"])

        assert result.exit_code == 0, result.output
        text = assert_coloured(result.stdout) if colour else plain(result.stdout)
        text = " ".join(text.split())
        assert "the matching ``[llm-openai]`` / ``[llm-anthropic]`` extra." in text

    def test_dry_run_selects_without_llm_call(
        self, tmp_path: Path, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        # Seed an unenriched document (no content_tags).
        doc_store = temp_stores.knowledge.document_store
        doc_store.put("doc-untagged", "some content", {"title": "Untagged"})
        # Seed a document already through the enrichment path — must be
        # excluded. Note the shape: `classified_mode` is a real ContentTags
        # field, unlike the `tag_confidence` / `tags` keys this test used to
        # seed, which ContentTags forbids and nothing ever wrote.
        doc_store.put(
            "doc-tagged",
            "other content",
            {"content_tags": {"classified_mode": "enrichment"}},
        )

        # Inject a stub LLM so the client check passes; dry-run won't call it.
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: _StubLLM("{}"),
        )
        result = runner.invoke(
            app, ["worker", "enrich", "--dry-run", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["dry_run"] is True
        assert "doc-untagged" in data["doc_ids"]
        assert "doc-tagged" not in data["doc_ids"]

    def test_missing_model_identity_emits_failure_not_judged(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        doc_store = temp_stores.knowledge.document_store
        doc_store.put("doc-model-less", "classify me", {"title": "Unknown judge"})

        class ModelLessLLM:
            async def generate(self, **kwargs: object) -> LLMResponse:
                return LLMResponse(
                    content=json.dumps(
                        {
                            "tags": ["test"],
                            "class": "notes",
                            "summary": "Summary.",
                            "importance": 0.4,
                            "class_confidence": 0.8,
                        }
                    ),
                    model=None,
                )

        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: ModelLessLLM(),
        )

        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout.strip())["failed"] == 1
        assert (
            temp_stores.operational.event_log.get_events(
                event_type=EventType.MEMORY_OP_JUDGED
            )
            == []
        )
        failures = temp_stores.operational.event_log.get_events(
            event_type=EventType.EXTRACTION_FAILED
        )
        assert len(failures) == 1
        assert failures[0].payload["failure_kind"] == "model_identity_missing"

    def test_only_successful_candidate_emits_judged_event(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        doc_store = temp_stores.knowledge.document_store
        doc_store.put("doc-ok", "classify this", {"title": "Good"})
        doc_store.put("doc-fail", "fail this", {"title": "Bad"})

        class SelectiveLLM:
            async def generate(self, **kwargs: Any) -> LLMResponse:
                messages = kwargs["messages"]
                prompt = messages[1].content
                if "fail this" in prompt:
                    message = "judge unavailable"
                    raise RuntimeError(message)
                return LLMResponse(
                    content=json.dumps(
                        {
                            "tags": ["architecture"],
                            "class": "architecture",
                            "summary": "Summary.",
                            "importance": 0.7,
                            "class_confidence": 0.91,
                        }
                    ),
                    model="test-model-v1",
                )

        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: SelectiveLLM(),
        )

        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout.strip())["enriched"] == 1
        events = temp_stores.operational.event_log.get_events(
            event_type=EventType.MEMORY_OP_JUDGED
        )
        assert len(events) == 1
        assert events[0].entity_id == "doc-ok"
        assert events[0].payload["decision"] == "architecture"
        assert events[0].payload["confidence"] == pytest.approx(0.91)
        assert events[0].payload["model_id"] == "test-model-v1"

    def test_selection_predicate_uses_classified_mode(
        self, temp_stores: StoreRegistry
    ) -> None:
        """Candidacy is "has not been enriched", a fact the path records.

        It used to be "``tag_confidence`` below a threshold", which could
        never skip anything: that key is not part of ``ContentTags`` and is
        never written, so the read returned ``None`` for every document.
        """
        doc_store = temp_stores.knowledge.document_store
        doc_store.put(
            "doc-ingested",
            "c",
            {"content_tags": {"classified_mode": "ingestion"}},
        )
        doc_store.put(
            "doc-enriched",
            "c",
            {"content_tags": {"classified_mode": "enrichment"}},
        )

        ids = {
            c["doc_id"]
            for c in worker._select_enrichment_candidates(doc_store, limit=50)
        }
        assert "doc-ingested" in ids
        assert "doc-enriched" not in ids

    def test_reenrich_takes_already_enriched_documents(
        self, temp_stores: StoreRegistry
    ) -> None:
        doc_store = temp_stores.knowledge.document_store
        doc_store.put(
            "doc-enriched",
            "c",
            {"content_tags": {"classified_mode": "enrichment"}},
        )
        ids = {
            c["doc_id"]
            for c in worker._select_enrichment_candidates(
                doc_store, limit=50, reenrich=True
            )
        }
        assert "doc-enriched" in ids

    def test_enrich_writes_tags_back(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        doc_store = temp_stores.knowledge.document_store
        doc_store.put("doc-x", "enrich me", {"title": "X"})
        canned = json.dumps(
            {
                "tags": ["alpha", "beta"],
                "class": "reference",
                "summary": "A summary.",
                "importance": 0.6,
                "tag_confidence": 0.9,
                "class_confidence": 0.9,
            }
        )
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: _StubLLM(canned),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["enriched"] == 1
        doc = doc_store.get("doc-x")
        metadata = doc["metadata"]
        tags = metadata["content_tags"]

        # The written shape must be a real ContentTags — it previously carried
        # `tags` / `auto_class` / `auto_importance` / `tag_confidence`, none of
        # which the schema permits, so every reader discarded it.
        from trellis.schemas.classification import ContentTags

        ContentTags.model_validate(tags)
        assert tags["custom"]["llm_tags"] == ["alpha", "beta"]
        assert tags["classified_mode"] == "enrichment"
        assert "classified_at" in tags

        # Flat keys go where their readers actually look.
        assert metadata["auto_importance"] == pytest.approx(0.6)
        assert metadata["document_form"] == "reference"

        events = temp_stores.operational.event_log.get_events(
            event_type=EventType.MEMORY_OP_JUDGED
        )
        assert len(events) == 1
        assert events[0].source == "worker:enrich"
        assert events[0].entity_id == "doc-x"
        assert events[0].payload["op_type"] == "classification"
        assert events[0].payload["decision"] == "reference"
        assert events[0].payload["confidence"] == pytest.approx(0.9)
        assert events[0].payload["model_id"] == "test-model"
        assert events[0].payload["input_digest"]["source_refs"] == ["doc-x"]

    def test_enrich_mirrors_tags_onto_the_vector_row(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        """#338 on a second path, found while fixing #381.

        ``worker enrich`` is a *post-embed* writer: it selects documents
        that are already stored and already embedded, then rewrites exactly
        the two keys ``MIRRORED_METADATA_KEYS`` covers. Writing them to the
        document store alone leaves ``SemanticSearch`` scoring the document
        on its pre-enrichment ``auto_importance`` and serving its
        pre-enrichment ``content_tags``, because the vector row's metadata
        is a snapshot frozen at embed time.

        Asserted on the row, not on a call argument — the whole defect
        class is a parameter nobody passed while every document-store
        assertion still passed.
        """
        doc_store = temp_stores.knowledge.document_store
        vector_store = temp_stores.knowledge.vector_store
        doc_store.put("doc-x", "enrich me", {"title": "X"})
        # The pre-enrichment snapshot the semantic axis would keep serving.
        vector_store.upsert(
            "doc-x",
            [0.4, 0.5, 0.6],
            {
                "doc_id": "doc-x",
                "content": "enrich me",
                "content_tags": {"classified_mode": "ingestion"},
                "auto_importance": 0.1,
            },
        )
        canned = json.dumps(
            {
                "tags": ["alpha", "beta"],
                "class": "reference",
                "summary": "A summary.",
                "importance": 0.6,
                "tag_confidence": 0.9,
                "class_confidence": 0.9,
            }
        )
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: _StubLLM(canned),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == 0, result.output

        doc = doc_store.get("doc-x")
        row = vector_store.get("doc-x")
        assert row is not None
        assert row["metadata"]["auto_importance"] == pytest.approx(0.6)
        assert row["metadata"]["content_tags"]["classified_mode"] == "enrichment"
        # Same predicate the writer enforces, so the test cannot drift from
        # the invariant it is pinning.
        assert not vector_metadata_diverges(doc["metadata"], row["metadata"])
        # Metadata-only: the embedding rode through, nothing re-embedded,
        # and the row's own excerpt was not clobbered by the document bag.
        assert [round(v, 3) for v in row["vector"]] == [0.4, 0.5, 0.6]
        assert row["metadata"]["content"] == "enrich me"

    def test_enrich_without_a_vector_store_still_writes_the_document(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        """A deployment with no vector store must still enrich.

        The mirror is fail-soft by design: the document store is the
        authority and has already been written by the time the sync runs.
        Refusing the enrichment to report a mirror failure would lose the
        tag, which is strictly worse than the divergence.
        """
        doc_store = temp_stores.knowledge.document_store
        doc_store.put("doc-x", "enrich me", {"title": "X"})
        monkeypatch.setattr(worker, "resolve_vector_store", lambda _registry: None)
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: _StubLLM(
                json.dumps({"tags": ["a"], "importance": 0.6})
            ),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout.strip())["enriched"] == 1
        assert doc_store.get("doc-x")["metadata"]["auto_importance"] == pytest.approx(
            0.6
        )

    def test_a_failed_item_logs_its_failure_kind(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        """``failure_kind`` is a plain string, so the log carries it as one.

        Reading ``.value`` off it (an enum idiom) always produced ``None``.
        """
        doc_store = temp_stores.knowledge.document_store
        doc_store.put("doc-x", "enrich me", {"title": "X"})
        doc_store.put("doc-y", "fail the call", {"title": "Y"})
        calls: list[tuple[str, str, dict[str, Any]]] = []

        class _Recorder:
            def __getattr__(self, level: str):
                def record(event: str, **kwargs: Any) -> None:
                    calls.append((level, event, kwargs))

                return record

        class _SplitLLM:
            """An unparseable reply for doc-x, a transport error for doc-y."""

            async def generate(
                self, *, messages: list[Message], **_kwargs: Any
            ) -> LLMResponse:
                if any("fail the call" in m.content for m in messages):
                    msg = "transport down"
                    raise RuntimeError(msg)
                return LLMResponse(content="I cannot classify this.", model="stub")

        monkeypatch.setattr(worker, "logger", _Recorder())
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: _SplitLLM(),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == 0, result.output
        failed = [
            kw for level, event, kw in calls if event == "worker_enrich.item_failed"
        ]
        # Two kinds, so a constant slug cannot satisfy it.
        assert sorted((kw["doc_id"], kw["failure_kind"]) for kw in failed) == [
            ("doc-x", "parse_error"),
            ("doc-y", "model_error"),
        ]

    def test_a_nan_importance_writes_no_importance(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        """``NaN`` decodes, and is truthy, so it used to reach the row unstamped.

        The serve-time reader raises on an unstamped ``auto_importance``, so on
        SQLite that one row dropped the keyword axis from every pack it met.
        """
        doc_store = temp_stores.knowledge.document_store
        doc_store.put("doc-x", "enrich me", {"title": "X"})
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: _StubLLM(
                '{"tags": ["alpha"], "importance": NaN}'
            ),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout.strip())["enriched"] == 1
        metadata = doc_store.get("doc-x")["metadata"]
        assert metadata["content_tags"]["custom"]["llm_tags"] == ["alpha"]
        assert "auto_importance" not in metadata
        assert metadata["content_tags"].get("importance_scored_at") is None


# ===========================================================================
# worker mine-precedents (WP3)
# ===========================================================================


def _make_failure_trace(domain: str = "mining") -> Trace:
    return Trace(
        source=TraceSource.AGENT,
        intent="do something risky",
        outcome=Outcome(status=OutcomeStatus.FAILURE, summary="it broke"),
        context=TraceContext(domain=domain),
    )


class TestWorkerMinePrecedents:
    def test_loud_failure_without_llm(self, temp_stores: StoreRegistry) -> None:
        result = runner.invoke(app, ["worker", "mine-precedents", "--format", "json"])
        assert result.exit_code == worker.EXIT_INTERNAL
        assert "LLM" in result.output

    def test_dry_run_counts_failure_traces(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        trace_store = temp_stores.operational.trace_store
        for _ in range(3):
            trace_store.append(_make_failure_trace())
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: _StubLLM("[]"),
        )
        result = runner.invoke(
            app,
            ["worker", "mine-precedents", "--dry-run", "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["dry_run"] is True
        assert data["failure_traces_in_scope"] == 3
        assert data["would_mine"] is True

    def test_generates_candidates(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        trace_store = temp_stores.operational.trace_store
        for _ in range(3):
            trace_store.append(_make_failure_trace())
        canned = json.dumps(
            [
                {
                    "title": "Failure pattern",
                    "description": "Common breakage",
                    "pattern": "p",
                    "confidence": 0.8,
                }
            ]
        )
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: _StubLLM(canned),
        )
        result = runner.invoke(app, ["worker", "mine-precedents", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["candidate_count"] == 1
        assert data["candidates"][0]["title"] == "Failure pattern"


# ===========================================================================
# admin reconcile-feedback (WP4)
# ===========================================================================


class TestAdminReconcileFeedback:
    def _write_feedback_log(self, temp_stores: StoreRegistry) -> Path:
        from trellis.feedback.recording import record_feedback

        data_dir = Path(temp_stores.stores_dir).parent
        record_feedback(
            _make_feedback(outcome="success", items=["a"]),
            log_dir=data_dir,
        )
        record_feedback(
            _make_feedback(outcome="failure", items=["b"]),
            log_dir=data_dir,
        )
        return data_dir

    def test_reconcile_emits_counts(self, temp_stores: StoreRegistry) -> None:
        data_dir = self._write_feedback_log(temp_stores)
        result = runner.invoke(
            app,
            [
                "admin",
                "reconcile-feedback",
                "--log-dir",
                str(data_dir),
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        assert data["scanned"] == 2
        assert data["emitted"] == 2
        assert data["failed"] == 0
        assert data["already_present"] == 0

    def test_reconcile_idempotent(self, temp_stores: StoreRegistry) -> None:
        data_dir = self._write_feedback_log(temp_stores)
        first = runner.invoke(
            app,
            [
                "admin",
                "reconcile-feedback",
                "--log-dir",
                str(data_dir),
                "--format",
                "json",
            ],
        )
        assert first.exit_code == 0, first.output
        second = runner.invoke(
            app,
            [
                "admin",
                "reconcile-feedback",
                "--log-dir",
                str(data_dir),
                "--format",
                "json",
            ],
        )
        assert second.exit_code == 0, second.output
        data = json.loads(second.stdout.strip())
        assert data["already_present"] == 2
        assert data["emitted"] == 0

    def test_reconcile_dry_run_emits_nothing(self, temp_stores: StoreRegistry) -> None:
        data_dir = self._write_feedback_log(temp_stores)
        result = runner.invoke(
            app,
            [
                "admin",
                "reconcile-feedback",
                "--log-dir",
                str(data_dir),
                "--dry-run",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["dry_run"] is True
        assert data["would_emit"] == 2
        # Nothing was actually emitted.
        fb = temp_stores.operational.event_log.get_events(
            event_type=EventType.FEEDBACK_RECORDED, limit=10
        )
        assert len(fb) == 0


# ===========================================================================
# worker capture-sessions — the CLI front door for the session-capture sweep
# ===========================================================================


class TestWorkerCaptureSessions:
    """The sweep itself is covered in tests/unit/workers/session_capture/;
    these pin the CLI contract — same code path, loud on a missing judge."""

    def _report(self, **overrides: object) -> CaptureReport:
        report = CaptureReport(transcripts_root="transcripts-root")
        for key, value in overrides.items():
            setattr(report, key, value)
        return report

    def test_delegates_to_run_sweep_with_the_cli_registry(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = MagicMock(return_value=self._report(sessions_seen=4))
        monkeypatch.setattr(capture_sweep, "run_sweep", spy)

        result = runner.invoke(
            worker_app, ["capture-sessions", "--dry-run", "--format", "json"]
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["status"] == "ok"
        assert payload["sessions_seen"] == 4
        assert payload["sessions_judge_unavailable"] == 0
        kwargs = spy.call_args[1]
        assert kwargs["dry_run"] is True
        assert kwargs["registry"] is not None

    def test_missing_judge_exits_nonzero(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            capture_sweep,
            "run_sweep",
            MagicMock(
                side_effect=capture_sweep.CaptureJudgeUnavailableError(
                    "no distillation judge is configured."
                )
            ),
        )

        result = runner.invoke(worker_app, ["capture-sessions"])

        assert result.exit_code == 1
        assert "no distillation judge is configured" in result.output

    def test_missing_judge_reports_json_under_format_json(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure path most likely to be piped into jq must stay parseable."""
        monkeypatch.setattr(
            capture_sweep,
            "run_sweep",
            MagicMock(
                side_effect=capture_sweep.CaptureJudgeUnavailableError(
                    "no distillation judge is configured."
                )
            ),
        )

        result = runner.invoke(worker_app, ["capture-sessions", "--format", "json"])

        assert result.exit_code == 1
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["status"] == "error"
        assert "no distillation judge is configured" in payload["message"]

    def test_unjudged_sessions_exit_nonzero_with_a_count(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRELLIS_CAPTURE_STRICT", raising=False)
        monkeypatch.setattr(
            capture_sweep,
            "run_sweep",
            MagicMock(
                return_value=self._report(
                    sessions_seen=2,
                    warnings=[{"kind": "distill_unavailable", "session_id": "a"}],
                )
            ),
        )

        result = runner.invoke(worker_app, ["capture-sessions", "--format", "json"])

        assert result.exit_code == 1
        payload = json.loads(result.stdout.strip().splitlines()[0])
        assert payload["sessions_judge_unavailable"] == 1
        # Not "ok": the command itself treats this run as failed.
        assert payload["status"] == "partial"

    def test_strict_opt_out_keeps_the_count_but_exits_zero(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_CAPTURE_STRICT", "0")
        monkeypatch.setattr(
            capture_sweep,
            "run_sweep",
            MagicMock(
                return_value=self._report(
                    sessions_seen=2,
                    warnings=[{"kind": "distill_unavailable", "session_id": "a"}],
                )
            ),
        )

        result = runner.invoke(worker_app, ["capture-sessions", "--format", "json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout.strip().splitlines()[0])
        assert payload["status"] == "partial"
        assert payload["sessions_judge_unavailable"] == 1

    def test_text_output_renders_the_report(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            capture_sweep,
            "run_sweep",
            MagicMock(return_value=self._report(sessions_seen=1, memories_written=1)),
        )

        result = runner.invoke(worker_app, ["capture-sessions"])

        assert result.exit_code == 0, result.output
        rendered = plain(result.output)
        assert "worker capture-sessions" in rendered
        assert "memories written: 1" in rendered

    def test_unapplied_supersessions_are_surfaced_not_just_counted(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A counter nobody prints is half a fix (#407).

        The reconcile tally rides ``reconcile_enabled``, since a deployment
        with the flag off has nothing to say; the failure line does not, since
        a non-zero count is a defect the operator has to see either way.
        """
        monkeypatch.setattr(
            capture_sweep,
            "run_sweep",
            MagicMock(
                return_value=self._report(
                    reconcile_enabled=True,
                    candidates_reconciled_supersede=3,
                    supersessions_failed=1,
                )
            ),
        )

        result = runner.invoke(worker_app, ["capture-sessions"])

        assert result.exit_code == 0, result.output
        rendered = plain(result.output)
        assert "3 supersede" in rendered
        assert "1 supersession(s) could not be applied" in rendered

    def test_reconcile_tally_is_hidden_when_the_flag_is_off(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            capture_sweep,
            "run_sweep",
            MagicMock(return_value=self._report(sessions_seen=1)),
        )

        result = runner.invoke(worker_app, ["capture-sessions"])

        assert result.exit_code == 0, result.output
        assert "supersede" not in plain(result.output)

    def test_errored_sessions_are_rendered_in_text(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session that raised mid-sweep is a defect, so it prints in red.

        Strict mode is off here so the render is pinned apart from the exit.
        """
        monkeypatch.setenv("TRELLIS_CAPTURE_STRICT", "0")
        spy = MagicMock(return_value=self._report(sessions_seen=5, sessions_errored=2))
        monkeypatch.setattr(capture_sweep, "run_sweep", spy)

        result = runner.invoke(worker_app, ["capture-sessions"])

        assert result.exit_code == 0, result.output
        assert "2 session(s) raised mid-sweep" in plain(result.output)

        spy.return_value = self._report(sessions_seen=5)
        clean = runner.invoke(worker_app, ["capture-sessions"])

        assert clean.exit_code == 0, clean.output
        assert "raised mid-sweep" not in plain(clean.output)

    def test_errored_sessions_fail_a_strict_run(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRELLIS_CAPTURE_STRICT", raising=False)
        monkeypatch.setattr(
            capture_sweep,
            "run_sweep",
            MagicMock(return_value=self._report(sessions_seen=3, sessions_errored=2)),
        )

        result = runner.invoke(worker_app, ["capture-sessions", "--format", "json"])

        assert result.exit_code == 1
        payload = json.loads(result.stdout.strip().splitlines()[0])
        assert payload["sessions_errored"] == 2
        assert payload["sessions_judge_unavailable"] == 0
        assert payload["status"] == "partial"

    def test_errored_sessions_under_the_opt_out_exit_zero_and_stay_partial(
        self, temp_stores: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_CAPTURE_STRICT", "0")
        monkeypatch.setattr(
            capture_sweep,
            "run_sweep",
            MagicMock(return_value=self._report(sessions_seen=3, sessions_errored=1)),
        )

        result = runner.invoke(worker_app, ["capture-sessions", "--format", "json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout.strip().splitlines()[0])
        assert payload["sessions_errored"] == 1
        assert payload["status"] == "partial"


class TestEnrichedContentTags:
    """``worker enrich``'s write-back must produce a parseable ``ContentTags``.

    It used to write four keys the schema forbids — ``tags``, ``auto_class``,
    ``auto_importance``, ``tag_confidence`` — so every enrichment run produced
    a ``content_tags`` the refresh path logged as ``existing_tags_malformed``
    and re-classified from scratch. The subsystem's whole output was discarded
    in silence.
    """

    @staticmethod
    def _result(**overrides):
        from trellis_workers.enrichment.service import EnrichmentResult

        base = {
            "auto_tags": ["todoist", "productivity"],
            "auto_class": "reference",
            "auto_importance": 0.8,
            "tag_confidence": 0.9,
            "success": True,
        }
        base.update(overrides)
        return EnrichmentResult(**base)

    def test_output_validates_as_content_tags(self) -> None:
        from datetime import UTC, datetime

        from trellis.schemas.classification import ContentTags
        from trellis_cli.worker import _enriched_content_tags

        out = _enriched_content_tags(None, self._result(), stamp=datetime.now(UTC))
        tags = ContentTags.model_validate(out)
        assert tags.classified_mode == "enrichment"
        assert "llm_facet" in tags.classified_by

    def test_llm_topic_guesses_do_not_land_in_the_domain_facet(self) -> None:
        """``domain`` hard-excludes; an unreviewed LLM guess cannot go there."""
        from datetime import UTC, datetime

        from trellis_cli.worker import _enriched_content_tags

        out = _enriched_content_tags(None, self._result(), stamp=datetime.now(UTC))
        assert out["domain"] == []
        assert out["custom"]["llm_tags"] == ["todoist", "productivity"]

    def test_preserves_prior_tags(self) -> None:
        from datetime import UTC, datetime

        from trellis.schemas.classification import ContentTags
        from trellis_cli.worker import _enriched_content_tags

        prior = ContentTags(
            content_type="procedure",
            domain=["ops"],
            classified_by=["structural"],
        ).model_dump(mode="json")
        out = _enriched_content_tags(prior, self._result(), stamp=datetime.now(UTC))
        assert out["content_type"] == "procedure"
        assert out["domain"] == ["ops"]
        assert out["classified_by"] == ["structural", "llm_facet"]

    def test_classified_at_is_a_real_datetime(self) -> None:
        """``model_copy`` does not coerce — a string stamp would survive as one."""
        from datetime import UTC, datetime

        from trellis.schemas.classification import ContentTags
        from trellis_cli.worker import _enriched_content_tags

        out = _enriched_content_tags(None, self._result(), stamp=datetime.now(UTC))
        assert ContentTags.model_validate(out).classified_at is not None


class TestEnrichPreservesRecency:
    """A whole-corpus tagging pass must not re-date the corpus (#406).

    Why the write is metadata-only, and why the enrichment pass is the widest
    exposure of the five, are argued once at the call site in
    ``_run_batch_enrichment`` — not restated here.

    Two tests rather than one because ``_select_enrichment_candidates``
    filters on nothing but ``content_tags``: a ``superseded`` row is an
    ordinary candidate, and ``superseded`` is the one lifecycle state that
    reaches ``mutate.retention._classify_document``'s age gate. The stamp
    assertion alone would not catch a regression that only that gate sees.
    """

    _CANNED = json.dumps(
        {
            "tags": ["alpha"],
            "class": "reference",
            "summary": "A summary.",
            "importance": 0.6,
            "tag_confidence": 0.9,
            "class_confidence": 0.9,
        }
    )

    def test_enrich_keeps_the_prior_updated_at(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        """Fails against the un-fixed call site, which re-stamps every row."""
        from datetime import timedelta

        doc_store = temp_stores.knowledge.document_store
        clock = fake_document_clock(monkeypatch)
        now = clock["now"]

        clock["now"] = now - timedelta(days=365)
        doc_store.put("doc-x", "enrich me", {"title": "X"})
        before = doc_store.get("doc-x")["updated_at"]

        clock["now"] = now
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: _StubLLM(self._CANNED),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout.strip())["enriched"] == 1
        doc = doc_store.get("doc-x")
        # The enrichment landed...
        assert doc["metadata"]["content_tags"]["classified_mode"] == "enrichment"
        # ...and the row does not claim to have been modified by it.
        assert doc["updated_at"] == before

    def test_an_enriched_superseded_row_still_ages_out_of_retention(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        """The retention age gate, which nothing masks.

        ``retention.prune`` with ``lifecycle_states=["superseded"]`` means
        "archive superseded rows older than N days", and
        ``_classify_document`` implements *older* as
        ``updated_at or created_at``. Un-fixed, enriching a year-old
        superseded note reset its age to zero and shielded it from the prune
        for a further 30 days — the criterion measuring time since the
        enrichment run rather than the age it claims to.

        The ``enriched == 1`` assertion is load-bearing beyond a smoke check:
        it is what pins "``_select_enrichment_candidates`` applies no
        lifecycle filter", the premise the whole paragraph above rests on. Add
        such a filter and this line fails rather than the argument silently
        becoming false.
        """
        from datetime import timedelta

        from trellis.mutate.retention import RetentionCriteria, resolve_candidates
        from trellis.schemas.classification import LIFECYCLE_KEY

        doc_store = temp_stores.knowledge.document_store
        clock = fake_document_clock(monkeypatch)
        now = clock["now"]

        clock["now"] = now - timedelta(days=365)
        doc_store.put(
            "doc-stale",
            "a year-old note that has since been replaced",
            {"title": "Stale", LIFECYCLE_KEY: {"state": "superseded"}},
        )

        clock["now"] = now
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: _StubLLM(self._CANNED),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout.strip())["enriched"] == 1

        report = resolve_candidates(
            RetentionCriteria(lifecycle_states=["superseded"], older_than_days=30),
            temp_stores,
        )
        assert [(c.item_id, c.reason_code) for c in report.candidates] == [
            ("doc-stale", "lifecycle_stale")
        ]


class TestEnrichSurvivesAConcurrentWrite:
    """#421 — the write-back must not revert a write made inside the LLM window.

    ``_select_enrichment_candidates`` hands back snapshots; ``batch_enrich``
    then runs *all N* model calls to completion before the first write. On a
    corpus-scale batch that window is minutes, and the un-fixed write-back
    landed ``doc["content"]`` and ``doc["metadata"]`` from before it — so a
    concurrent write was reverted with no error and no log line.

    Post-#406/#418 the revert is worse than a lost update: the write correctly
    passes ``preserve_updated_at=True``, so the row keeps the *losing* writer's
    stamp while holding pre-LLM content, and
    ``retrieve.file_context``'s ``newest_item_at`` — the #307 hook's staleness
    gate — reads a timestamp the content does not correspond to.

    The race is simulated where it actually happens: the stub model performs
    the concurrent write from inside ``generate``, so it lands between
    selection and the write-back exactly as a second process would. The writer
    is a *second* store instance on the same file, which is the real
    deployment shape (host CLI + container, one bind-mounted data dir).
    """

    _CANNED = json.dumps(
        {
            "tags": ["alpha"],
            "class": "reference",
            "summary": "A summary.",
            "importance": 0.6,
            "tag_confidence": 0.9,
            "class_confidence": 0.9,
        }
    )

    @staticmethod
    def _llm_that_writes(canned: str, side_effect) -> _StubLLM:
        """A stub model that performs ``side_effect`` on its first call."""

        class _Racing(_StubLLM):
            def __init__(self) -> None:
                super().__init__(canned)
                self._fired = False

            async def generate(self, **kwargs):
                if not self._fired:
                    self._fired = True
                    side_effect()
                return await super().generate(**kwargs)

        return _Racing()

    def test_the_concurrent_content_survives_and_the_tags_still_land(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        """The core of #421. Fails against the un-fixed write-back.

        Both halves are asserted together on purpose: a fix that protected the
        content by skipping the write would pass the first assertion and lose
        the enrichment, which is the other way this can silently fail.
        """
        doc_store = temp_stores.knowledge.document_store
        doc_store.put("doc-x", "the original body", {"title": "X"})

        def concurrent_write() -> None:
            doc_store.put(
                "doc-x",
                "the body a second writer stored mid-batch",
                {"title": "X", "written_by": "the other process"},
            )

        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: self._llm_that_writes(
                self._CANNED, concurrent_write
            ),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["enriched"] == 1

        doc = doc_store.get("doc-x")
        assert doc["content"] == "the body a second writer stored mid-batch"
        # The metadata half of the same lost update, which a content-hash
        # check cannot see: the concurrent writer's own key survives too.
        assert doc["metadata"]["written_by"] == "the other process"
        assert doc["metadata"]["content_tags"]["classified_mode"] == "enrichment"
        assert doc["metadata"]["auto_importance"] == pytest.approx(0.6)
        from trellis.core.hashing import content_hash

        (judged,) = temp_stores.operational.event_log.get_events(
            event_type=EventType.MEMORY_OP_JUDGED
        )
        assert judged.payload["input_digest"]["hash"] == content_hash(
            "the original body"
        )

    def test_the_race_is_counted(self, temp_stores: StoreRegistry, monkeypatch) -> None:
        """A silent merge would hide a real signal about deployment concurrency."""
        doc_store = temp_stores.knowledge.document_store
        doc_store.put("doc-x", "the original body", {"title": "X"})
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: self._llm_that_writes(
                self._CANNED, lambda: doc_store.put("doc-x", "rewritten", {})
            ),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout.strip())["stale_snapshot"] == 1

    def test_the_counter_reads_zero_when_nothing_raced(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        """Paired with the test above so neither arm can drift to a constant."""
        doc_store = temp_stores.knowledge.document_store
        doc_store.put("doc-x", "the original body", {"title": "X"})
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: _StubLLM(self._CANNED),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["enriched"] == 1
        assert data["stale_snapshot"] == 0
        assert data["vanished"] == 0

    def test_the_losing_writers_stamp_is_left_alone(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        """The "row actively lies" half of #421, now honest.

        The concurrent writer stamps T. The enrichment preserves ``updated_at``
        — correctly, #418 — so before this fix the row reported "current as of
        T" while holding the *pre-LLM* content. Now the content at T is the
        content the row holds, so the same preserved stamp is true.

        Asserted against the seeded stamp, not merely against itself: the
        ``fake_document_clock`` caveat is that a stamp comparison passes
        vacuously if the patch missed.
        """
        from datetime import timedelta

        doc_store = temp_stores.knowledge.document_store
        clock = fake_document_clock(monkeypatch)
        now = clock["now"]

        clock["now"] = now - timedelta(days=365)
        doc_store.put("doc-x", "the original body", {"title": "X"})

        concurrent_stamp = now - timedelta(days=1)

        def concurrent_write() -> None:
            clock["now"] = concurrent_stamp
            doc_store.put("doc-x", "the concurrent body", {"title": "X"})
            clock["now"] = now

        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: self._llm_that_writes(
                self._CANNED, concurrent_write
            ),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == 0, result.output

        doc = doc_store.get("doc-x")
        assert doc["updated_at"] == concurrent_stamp.isoformat()
        assert doc["content"] == "the concurrent body"

    def test_the_enrichment_merges_onto_the_current_tag_bag(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        """The prior tags come from the re-read, not from the snapshot.

        ``_enriched_content_tags`` derives ``custom`` and ``classified_by``
        from the prior value, so taking that prior from the pre-LLM snapshot
        would revert a concurrent tag write while the content survived — the
        same defect, one key deeper.
        """
        doc_store = temp_stores.knowledge.document_store
        doc_store.put("doc-x", "the original body", {"title": "X"})

        def concurrent_tag_write() -> None:
            doc = doc_store.get("doc-x")
            doc_store.put(
                "doc-x",
                doc["content"],
                {**doc["metadata"], "content_tags": {"custom": {"k": ["v"]}}},
            )

        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: self._llm_that_writes(
                self._CANNED, concurrent_tag_write
            ),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == 0, result.output

        custom = doc_store.get("doc-x")["metadata"]["content_tags"]["custom"]
        assert custom["k"] == ["v"]
        assert custom["llm_tags"] == ["alpha"]

    def test_a_document_deleted_mid_batch_is_not_resurrected(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        """``put`` on a missing id inserts, which would undo the delete.

        Un-guarded the row comes back carrying pre-LLM content and a fresh
        ``created_at``, with nothing recording that it had been removed.
        """
        doc_store = temp_stores.knowledge.document_store
        doc_store.put("doc-x", "the original body", {"title": "X"})
        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: self._llm_that_writes(
                self._CANNED, lambda: doc_store.delete("doc-x")
            ),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["enriched"] == 0
        assert data["vanished"] == 1
        assert doc_store.get("doc-x") is None

    def test_the_vector_mirror_carries_the_merged_metadata(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        """#338's mirror must forward what landed, not the snapshot bag.

        Mirroring ``doc["metadata"]`` would push the pre-LLM ``content_tags``
        onto the vector row — reintroducing on the semantic axis exactly the
        revert the re-read removed from the document store.
        """
        doc_store = temp_stores.knowledge.document_store
        vector_store = temp_stores.knowledge.vector_store
        doc_store.put("doc-x", "the original body", {"title": "X"})
        vector_store.upsert(
            "doc-x",
            [0.4, 0.5, 0.6],
            {"doc_id": "doc-x", "content_tags": {"classified_mode": "ingestion"}},
        )

        def concurrent_tag_write() -> None:
            doc = doc_store.get("doc-x")
            doc_store.put(
                "doc-x",
                doc["content"],
                {**doc["metadata"], "content_tags": {"custom": {"k": ["v"]}}},
            )

        monkeypatch.setattr(
            worker,
            "_require_llm_client_or_exit",
            lambda _consumer, *, command: self._llm_that_writes(
                self._CANNED, concurrent_tag_write
            ),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout.strip())["vector_rows_synced"] == 1

        doc = doc_store.get("doc-x")
        row = vector_store.get("doc-x")
        assert not vector_metadata_diverges(doc["metadata"], row["metadata"])
        assert row["metadata"]["content_tags"]["custom"]["k"] == ["v"]


class TestCurateSurvivesASecondWriter:
    """#438 — the nightly cron is not the advisory file's only writer.

    ``advisories.json`` is written by this cron, by the host ``trellis
    analyze`` advisory commands, and by the containerised ``POST
    /api/v1/advisories/generate``, all against one bind-mounted data dir.
    Before the store's stale guard the cycle silently deleted whatever
    another process had landed; after it, the refusal must not take the
    *learning* stage down instead — that stage neither reads nor writes
    this file, and a traceback here would lose it.
    """

    @staticmethod
    def _seed_scored_advisory(registry: StoreRegistry, path: Path) -> str:
        """Put one advisory in the file and enough graded packs to score it.

        The fitness loop writes ``put`` for every advisory it scores, so
        this is what makes the stage actually reach a write — without it a
        cycle with nothing to say would refuse nothing and the test would
        pass vacuously.
        """
        advisory = Advisory(
            category=AdvisoryCategory.ENTITY,
            confidence=0.7,
            message="seeded advisory",
            evidence=AdvisoryEvidence(
                sample_size=10,
                success_rate_with=0.8,
                success_rate_without=0.4,
                effect_size=0.4,
            ),
            scope="global",
        )
        AdvisoryStore(path).put(advisory)

        event_log = registry.operational.event_log
        for i in range(4):  # > _ADVISORY_MIN_PRESENTATIONS
            pack_id = f"wc-adv-pack-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "intent": "advisory probe",
                    "domain": "wc-test",
                    "advisory_ids": [advisory.advisory_id],
                    "injected_items": [],
                    "injected_item_ids": [],
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
                    "followed_advisory_ids": [advisory.advisory_id],
                },
            )
        return advisory.advisory_id

    @staticmethod
    def _second_writer_after_load(
        monkeypatch: pytest.MonkeyPatch, path: Path
    ) -> list[str]:
        """Make another process append a row between this store's load and save.

        Patched at the store *factory*, not at any guard: the cycle gets a
        real ``AdvisoryStore`` that really loaded the file, and the file
        really moves on underneath it. That is the race, reproduced rather
        than simulated.
        """
        landed: list[str] = []
        original = worker._advisory_store_from_data_dir

        def _racing_factory() -> object:
            store = original()
            theirs = Advisory(
                category=AdvisoryCategory.SCOPE,
                confidence=0.6,
                message="written by the other process",
                evidence=AdvisoryEvidence(
                    sample_size=10,
                    success_rate_with=0.8,
                    success_rate_without=0.4,
                    effect_size=0.4,
                ),
                scope="global",
            )
            AdvisoryStore(path).put(theirs)
            landed.append(theirs.advisory_id)
            return store

        monkeypatch.setattr(worker, "_advisory_store_from_data_dir", _racing_factory)
        return landed

    def test_the_cycle_completes_and_the_other_writer_survives(
        self,
        tmp_path: Path,
        temp_stores: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "data" / "stores" / ADVISORY_FILENAME
        self._seed_scored_advisory(temp_stores, path)
        _seed_promote_signal(temp_stores)
        landed = self._second_writer_after_load(monkeypatch, path)

        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(tmp_path / "review"),
                "--format",
                "json",
            ],
        )

        assert result.exit_code == EXIT_STORE, result.output
        data = json.loads(result.stdout.strip())
        # The refusal is reported, not swallowed — and not as ``degraded``,
        # because nothing is broken and no operator action is required.
        assert data["status"] == "stale"
        assert data["advisory_store_stale"] is not None
        assert data["advisory_store_stale"]["code"] == "STALE_STORE_WRITE"
        assert data["advisory_store_degraded"] is None
        # NOT reported as a skipped stage: the stage really ran, and the
        # fitness loop writes per advisory, so some adjustments may already
        # have landed. ``status`` and ``advisory_store_stale`` carry it.
        assert "advisories" not in data["skipped_stages"]
        # The rest of the cycle still ran: this is a contained refusal, not
        # a crash that takes the learning stage with it.
        assert data["learning_observations"] >= 3
        # And the other process's advisory is still in the file.
        assert AdvisoryStore(path).get(landed[0]) is not None

    def test_the_text_surface_says_so_too(
        self,
        tmp_path: Path,
        temp_stores: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A refusal honest only in ``--format json`` is one nobody reads."""
        path = tmp_path / "data" / "stores" / ADVISORY_FILENAME
        self._seed_scored_advisory(temp_stores, path)
        _seed_promote_signal(temp_stores)
        self._second_writer_after_load(monkeypatch, path)

        result = runner.invoke(
            app, ["worker", "curate", "--output-dir", str(tmp_path / "review")]
        )

        assert result.exit_code == EXIT_STORE, result.output
        assert "ADVISORY WRITE REFUSED" in result.output
        # The refusal's own message rides through, so the operator is told
        # what happened rather than only that something did.
        assert "changed after this process read it" in result.output.replace("\n", "")

    def test_a_clean_cycle_carries_no_stale_marker(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """The negative control: the field must stay ``None`` on the happy path.

        Without it the cycle could report ``stale`` unconditionally and
        every assertion above would still pass.
        """
        path = tmp_path / "data" / "stores" / ADVISORY_FILENAME
        self._seed_scored_advisory(temp_stores, path)
        _seed_promote_signal(temp_stores)

        result = runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(tmp_path / "review"),
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["advisory_store_stale"] is None
        assert data["status"] == "ok"
        assert "advisories" not in data["skipped_stages"]
        assert "ADVISORY WRITE REFUSED" not in plain(result.output)


class TestARefusedNightlyWriteEscalates:
    """#448 — the only *unattended* advisory writer was the silent one.

    ``trellis analyze``'s two advisory commands exit non-zero on a refused
    write — ``EXIT_STORE`` since #489, ``2`` before it — and ``POST
    /advisories/generate`` answers 409. This cron emitted JSON
    and returned 0, and emitted no event, so ``trellis analyze health``
    could not see it either — the two paths where a human is already
    watching shouted, and the one that runs at 03:30 unattended did not.

    Verified about the reference deployment rather than assumed: its
    ``curate-nightly.sh`` logs this command's JSON and reads nothing out of
    it, and the one downstream consumer (``roadmap-nightly.sh``) greps that
    log's tail for ``advisories_generated``. Neither reads ``status``, so
    before this the refusal escalated nowhere at all.

    Two halves, and the event is the load-bearing one. A non-zero exit is
    near-invisible under cron; ``WRITE_REJECTED`` is the repo's existing
    channel for a write that died before it became a Command, is already
    read by ``analyze health``, and counts *recurrence* — which is the only
    thing that separates the transient race (self-heals, needs nobody) from
    the standing two-writer conflict (needs a human).
    """

    @staticmethod
    def _rejections(registry: StoreRegistry) -> WriteHealthReport:
        return summarize_write_health(registry.operational.event_log, days=1)

    @staticmethod
    def _run(tmp_path: Path, *extra: str) -> Result:
        return runner.invoke(
            app,
            [
                "worker",
                "curate",
                "--output-dir",
                str(tmp_path / "review"),
                *extra,
            ],
        )

    def test_a_stale_refusal_is_counted_by_analyze_health(
        self,
        tmp_path: Path,
        temp_stores: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "data" / "stores" / ADVISORY_FILENAME
        TestCurateSurvivesASecondWriter._seed_scored_advisory(temp_stores, path)
        _seed_promote_signal(temp_stores)
        TestCurateSurvivesASecondWriter._second_writer_after_load(monkeypatch, path)

        assert self._run(tmp_path, "--format", "json").exit_code == EXIT_STORE

        report = self._rejections(temp_stores)
        assert report.by_tool[ADVISORY_WRITER_SURFACE].boundary_rejected == 1
        # ``stale_write``, not ``config_unreadable``: the file read fine.
        # The distinction is the operator's response — nothing, versus go
        # look at a broken file — so pooling them would be a wrong signal.
        assert report.boundary_kinds[f"stale_write@{ADVISORY_FILENAME}"] == 1

    def test_a_degraded_store_is_counted_by_analyze_health(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        _seed_promote_signal(temp_stores)
        TestCurateSurvivesADegradedAdvisoryStore._corrupt_advisory_file(tmp_path)

        assert self._run(tmp_path, "--format", "json").exit_code == EXIT_STORE

        report = self._rejections(temp_stores)
        assert report.by_tool[ADVISORY_WRITER_SURFACE].boundary_rejected == 1
        assert report.boundary_kinds[f"config_unreadable@{ADVISORY_FILENAME}"] == 1

    def test_the_refusal_message_rides_the_event(
        self,
        tmp_path: Path,
        temp_stores: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The recovery advice reaches an operator reading the event.

        Without it the count says a write was refused and nothing says
        which file or what to type, so the reader has to re-run the job
        that failed to find out.

        The CLI runs from ``tmp_path`` with a *relative* data dir, so the
        path in the message is one chosen here (#634's shape). The message
        is capped at 500 characters, and built from an absolute
        ``tmp_path`` its ``mv`` command carried pytest's basetemp: under a
        long ``--basetemp`` the cap fell inside the path and the ``mv``
        assertion failed.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TRELLIS_DATA_DIR", "data")
        _seed_promote_signal(temp_stores)
        path = TestCurateSurvivesADegradedAdvisoryStore._corrupt_advisory_file(tmp_path)

        self._run(tmp_path, "--format", "json")

        events = temp_stores.operational.event_log.get_events(
            event_type=EventType.WRITE_REJECTED, limit=10
        )
        assert len(events) == 1
        assert events[0].source == ADVISORY_WRITER_SURFACE
        msg = events[0].payload["rejections"][0]["msg"]
        assert "malformed_json" in msg
        assert f"mv {path.relative_to(tmp_path)}" in msg

    def test_a_clean_cycle_emits_nothing(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """The negative control.

        Emitting unconditionally would pass every assertion above while
        making the count meaningless — and would put a permanent rejection
        on a healthy deployment's write-health report.
        """
        _seed_promote_signal(temp_stores)

        assert self._run(tmp_path, "--format", "json").exit_code == 0

        report = self._rejections(temp_stores)
        assert ADVISORY_WRITER_SURFACE not in report.by_tool
        assert report.boundary_rejected == 0

    def test_recurrence_is_what_reaches_the_operator(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """Three nights of the same refusal is a standing conflict, not a race.

        This is the property the exit code cannot carry — a cron's exit
        code is a fact about one night, and the whole complaint in #448 is
        that "self-heals" and "recurs forever" are indistinguishable from
        outside. ``repeated_collisions`` is where they separate, and it
        only reads correctly because the row carries a named ``kind`` and
        the file as its ``loc`` instead of falling to ``other@``.
        """
        _seed_promote_signal(temp_stores)
        TestCurateSurvivesADegradedAdvisoryStore._corrupt_advisory_file(tmp_path)

        for _ in range(3):
            self._run(tmp_path, "--format", "json")

        report = self._rejections(temp_stores)
        assert report.repeated_collisions == [
            {"kind": "config_unreadable", "loc": ADVISORY_FILENAME, "count": 3}
        ]
        assert report.status == "warn"
        assert any("config_unreadable" in r for r in report.reasons)

    def test_recurrence_does_not_raise_the_capture_banner(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """The same three nights must **not** headline lost experience.

        ``analyze health`` is the right reader for this refusal; the
        capture banner is not. It prepends *"New experience from this
        session is NOT being saved"* to every served pack, and a refused
        advisory write stops no capture path at all — ``save_memory``,
        ``save_experience``, the capture sweep and every ingest path keep
        writing, and the advisory file is a derived artefact the next cycle
        regenerates. Left watched, the exact scenario above pinned a false
        alarm to every retrieval on a healthy deployment, which is #461's
        ``record_feedback`` half (#448).

        Driven through the real command rather than synthetic events: the
        roster guard checks the *label*, and this checks that the label the
        worker actually emits is the one that was classified.
        """
        _seed_promote_signal(temp_stores)
        TestCurateSurvivesADegradedAdvisoryStore._corrupt_advisory_file(tmp_path)

        for _ in range(3):
            self._run(tmp_path, "--format", "json")

        # The rejections are real and counted — this is not a test of an
        # absent signal.
        assert (
            self._rejections(temp_stores)
            .by_tool[ADVISORY_WRITER_SURFACE]
            .boundary_rejected
            == 3
        )
        assert not is_capture_surface(ADVISORY_WRITER_SURFACE)
        assert (
            check_capture_health(temp_stores.operational.event_log, threshold=3) is None
        )

    def test_both_format_surfaces_exit_the_same_way(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """Executed parity, not the AST scan's structural one.

        ``tests/unit/test_format_exit_parity_rule.py`` proves a non-zero
        exit is *reachable* from both arms; it says nothing about whether
        the same input produces it on both. #437's defect was that a
        machine caller doing the documented thing was strictly worse off
        than one scraping human prose, and that is the claim here.
        """
        _seed_promote_signal(temp_stores)
        TestCurateSurvivesADegradedAdvisoryStore._corrupt_advisory_file(tmp_path)

        text = self._run(tmp_path)
        json_run = self._run(tmp_path, "--format", "json")

        assert text.exit_code == json_run.exit_code == EXIT_STORE

    def test_the_loop_path_exits_on_its_last_cycle(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """``--interval`` funnels into the same exit rule as the single shot.

        A second surface inside one command is how the first divergence got
        in, so the loop's result is fed to the same helper rather than
        given a rule of its own.
        """
        _seed_promote_signal(temp_stores)
        TestCurateSurvivesADegradedAdvisoryStore._corrupt_advisory_file(tmp_path)

        result = worker._run_curate_loop(
            interval=1,
            output_dir=tmp_path / "review",
            days=30,
            dry_run=False,
            skip_noise_tags=False,
            skip_advisories=False,
            skip_learning=False,
            no_meta_trace=True,
            output_format="json",
            max_cycles=1,
        )

        assert result is not None
        assert result.status == "degraded"
        with pytest.raises(typer.Exit) as exc:
            worker._exit_if_advisory_write_refused(result)
        assert exc.value.exit_code == EXIT_STORE

    def test_a_clean_loop_cycle_does_not_exit(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        """Negative control for the loop rule, and for ``None``.

        ``_exit_if_advisory_write_refused`` raising unconditionally would
        satisfy every assertion above.
        """
        _seed_promote_signal(temp_stores)

        result = worker._run_curate_loop(
            interval=1,
            output_dir=tmp_path / "review",
            days=30,
            dry_run=False,
            skip_noise_tags=False,
            skip_advisories=False,
            skip_learning=False,
            no_meta_trace=True,
            output_format="json",
            max_cycles=1,
        )

        assert result is not None
        worker._exit_if_advisory_write_refused(result)
        worker._exit_if_advisory_write_refused(None)


class TestWorkerLlmRouting:
    """Each LLM-backed worker command builds its client from its own route."""

    @pytest.mark.parametrize(
        ("command", "consumer"),
        [
            ("enrich", LLMConsumer.ENRICHMENT),
            ("mine-precedents", LLMConsumer.PRECEDENT_MINING),
        ],
    )
    def test_the_command_names_its_consumer(
        self,
        temp_stores: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
        command: str,
        consumer: LLMConsumer,
    ) -> None:
        seen: list[tuple[LLMConsumer, str]] = []

        def _record(consumer_arg: LLMConsumer, *, command: str) -> _StubLLM:
            seen.append((consumer_arg, command))
            return _StubLLM("[]")

        monkeypatch.setattr(worker, "_require_llm_client_or_exit", _record)
        result = runner.invoke(
            app, ["worker", command, "--dry-run", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        assert seen == [(consumer, f"worker {command}")]

    def test_the_consumer_reaches_the_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = MagicMock()
        client = object()
        registry.build_llm_client.return_value = client
        monkeypatch.setattr(worker, "_get_registry", lambda: registry)
        built = worker._require_llm_client_or_exit(
            LLMConsumer.PRECEDENT_MINING, command="worker mine-precedents"
        )
        assert built is client
        registry.build_llm_client.assert_called_once_with(
            consumer=LLMConsumer.PRECEDENT_MINING
        )

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_a_malformed_route_exits_store_on_either_format(
        self, temp_stores: StoreRegistry, tmp_path: Path, fmt: str
    ) -> None:
        """Not the "no LLM configured" exit: there is a config line to fix.

        Runs the real chain — config file, registry, route resolution, the
        root CLI boundary — because ``_require_llm_client_or_exit`` passing
        the error through is the whole behaviour under test.
        """
        _write_config(
            tmp_path / "config",
            "llm:\n"
            "  provider: openai\n"
            "  api_key_env: OPENAI_API_KEY\n"
            "  routes:\n"
            "    enrichment: deep\n",
        )
        _reset_registry()
        result = runner.invoke(app, ["worker", "enrich", "--format", fmt])
        assert result.exit_code == EXIT_STORE, result.output
        if fmt == "json":
            payload = json.loads(result.stdout.strip())
            assert payload["status"] == "error"
            assert payload["error_type"] == "LLMRoutingError"
            assert payload["setting"] == "llm.routes.enrichment"
        else:
            assert "(defined: none)" in plain(result.output)
