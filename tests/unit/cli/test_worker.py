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

import pytest
import typer
from typer.testing import CliRunner

from trellis.llm import LLMResponse, Message
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import Outcome, Trace, TraceContext
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli import worker
from trellis_cli.main import app, worker_app
from trellis_cli.stores import _reset_registry

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
    def test_worker_app_exposes_new_subcommands(self) -> None:
        names = {
            cmd.name or cmd.callback.__name__ for cmd in worker_app.registered_commands
        }
        assert {"curate", "enrich", "mine-precedents"} <= names

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


# ===========================================================================
# worker curate --interval — loop mode (WP3)
# ===========================================================================


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
        # Seed a fully-enriched document (high confidence) — must be excluded.
        doc_store.put(
            "doc-tagged",
            "other content",
            {"content_tags": {"tag_confidence": 0.95, "tags": ["x"]}},
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

    def test_selection_predicate_low_confidence(
        self, temp_stores: StoreRegistry
    ) -> None:
        doc_store = temp_stores.knowledge.document_store
        doc_store.put(
            "doc-lowconf",
            "c",
            {"content_tags": {"tag_confidence": 0.2, "tags": ["x"]}},
        )
        candidates = worker._select_enrichment_candidates(
            doc_store, limit=50, confidence_threshold=0.5
        )
        ids = {c["doc_id"] for c in candidates}
        assert "doc-lowconf" in ids

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
        tags = doc["metadata"]["content_tags"]
        assert tags["tags"] == ["alpha", "beta"]
        assert tags["tag_confidence"] == pytest.approx(0.9)
        assert "classified_at" in tags


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
