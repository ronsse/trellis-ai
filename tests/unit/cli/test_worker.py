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
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

from tests.document_recency import fake_document_clock
from trellis.core.vector_metadata import vector_metadata_diverges
from trellis.llm import LLMResponse, Message
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import Outcome, Trace, TraceContext
from trellis.stores.advisory_source import ADVISORY_FILENAME
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli import worker
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
        assert Path(data["decisions_path"]).exists()
        assert data["skipped_stages"] == []

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

    def test_dry_run_mutates_nothing(
        self, tmp_path: Path, temp_stores: StoreRegistry
    ) -> None:
        _seed_promote_signal(temp_stores)
        out_dir = tmp_path / "review"

        # Capture event count before — advisory generation/fitness emit events.
        events_before = temp_stores.operational.event_log.count()

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
        # Advisories are skipped wholesale in dry-run (they mutate the store).
        assert "advisories" in data["skipped_stages"]
        # No review artifacts written on disk.
        assert data["candidates_path"] is None
        assert not out_dir.exists() or not any(out_dir.iterdir())
        # No document mutated to noise.
        doc = temp_stores.knowledge.document_store.get("wc:doc:helpful")
        assert doc is None  # never created — proves no write happened
        # Dry-run emitted no new events.
        assert temp_stores.operational.event_log.count() == events_before

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

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        # The headline, not just the body: a wrapper reading only ``status``
        # would otherwise record a clean nightly run (#393).
        assert data["status"] == "degraded"
        assert data["advisory_store_degraded"] is not None
        assert data["advisory_store_degraded"]["reason"] == "malformed_json"
        assert (
            data["advisory_store_degraded"]["recovery"] == f"mv {path} {path}.corrupt"
        )
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

        assert result.exit_code == 0, result.output
        assert "ADVISORY STORE DEGRADED" in result.output
        assert f"mv {path}" in result.output.replace("\n", "")

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
        assert "ADVISORY STORE DEGRADED" not in result.output

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

        assert result.exit_code == 0, result.output
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
        """
        data_dir = tmp_path / "d [staging]" / "data"
        (data_dir / "stores").mkdir(parents=True)
        (data_dir / "stores" / ADVISORY_FILENAME).write_text(
            '{"advisories": [ torn', encoding="utf-8"
        )
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
        _reset_registry()

        result = runner.invoke(
            app, ["worker", "curate", "--output-dir", str(tmp_path / "review")]
        )

        assert "ADVISORY STORE DEGRADED" in result.output
        assert "[staging]" in result.output, (
            "Rich ate the bracketed path segment, so the recovery command "
            "printed to the operator does not run"
        )


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
            worker, "_require_llm_client_or_exit", lambda: _StubLLM("{}")
        )
        result = runner.invoke(
            app, ["worker", "enrich", "--dry-run", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["dry_run"] is True
        assert "doc-untagged" in data["doc_ids"]
        assert "doc-tagged" not in data["doc_ids"]

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
            worker, "_require_llm_client_or_exit", lambda: _StubLLM(canned)
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

    def test_enrich_mirrors_tags_onto_the_vector_row(
        self, temp_stores: StoreRegistry, monkeypatch
    ) -> None:
        """#338 on a second path, found while fixing #381.

        ``worker enrich`` is a *post-embed* writer: it selects documents
        that are already stored and already embedded, then rewrites exactly
        the two keys ``SYNCED_METADATA_KEYS`` covers. Writing them to the
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
            worker, "_require_llm_client_or_exit", lambda: _StubLLM(canned)
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
            lambda: _StubLLM(json.dumps({"tags": ["a"], "importance": 0.6})),
        )
        result = runner.invoke(app, ["worker", "enrich", "--format", "json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout.strip())["enriched"] == 1
        assert doc_store.get("doc-x")["metadata"]["auto_importance"] == pytest.approx(
            0.6
        )


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
            worker, "_require_llm_client_or_exit", lambda: _StubLLM("[]")
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
            worker, "_require_llm_client_or_exit", lambda: _StubLLM(canned)
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
        assert "worker capture-sessions" in result.output
        assert "memories written: 1" in result.output


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
            worker, "_require_llm_client_or_exit", lambda: _StubLLM(self._CANNED)
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
        """The **second** reader of ``updated_at``, which nothing masks.

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
            worker, "_require_llm_client_or_exit", lambda: _StubLLM(self._CANNED)
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
