"""End-to-end coverage for the dual-loop *promote* half.

Exercises the full chain:
  PACK_ASSEMBLED + FEEDBACK_RECORDED EventLog rows
  -> ``build_learning_observations_from_event_log``
  -> ``analyze_learning_observations``
  -> ``write_learning_review_artifacts`` + operator-approved decisions
  -> ``prepare_learning_promotions`` -> entity / edge payloads.

Closes plan §5.5.2 row 2: pre-2026-04-29, ``learning.scoring`` had no
caller in the source tree (only synthetic unit-test fixtures fed it).
This module proves the EventLog path can drive promotion end-to-end.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.unit.learning._registry_fixture import build_seeded_registry
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import record_feedback
from trellis.learning import (
    analyze_learning_observations,
    build_learning_observations_from_event_log,
    derive_selection_efficiency,
    prepare_learning_promotions,
    write_learning_review_artifacts,
)
from trellis.ops import ParameterRegistry
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.pack import PackItem
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


@pytest.fixture
def learning_registry() -> ParameterRegistry:
    return build_seeded_registry()


def _emit_pack_assembled(
    event_log: SQLiteEventLog,
    *,
    pack_id: str,
    domain: str,
    intent: str,
    items: list[dict[str, str]],
) -> None:
    """Emit a PACK_ASSEMBLED event with a ``injected_items`` payload
    matching what ``PackBuilder._emit_telemetry`` produces in
    production."""
    event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload={
            "intent": intent,
            "domain": domain,
            "items_count": len(items),
            "injected_item_ids": [i["item_id"] for i in items],
            "injected_items": [
                {
                    "item_id": i["item_id"],
                    "item_type": i["item_type"],
                    "rank": rank + 1,
                    "selection_reason": "selected_by_relevance",
                    "score_breakdown": {},
                    "estimated_tokens": 32,
                    "strategy_source": i.get("strategy_source", "keyword"),
                }
                for rank, i in enumerate(items)
            ],
            "strategies_used": ["keyword"],
        },
    )


def _record(
    event_log: SQLiteEventLog,
    log_dir: Path,
    *,
    pack_id: str,
    run_id: str,
    intent: str,
    intent_family: str,
    items_served: list[str],
    items_referenced: list[str],
    outcome: str,
    phase: str = "GENERATE",
    items_unhelpful: list[str] | None = None,
) -> None:
    feedback = PackFeedback(
        run_id=run_id,
        phase=phase,
        intent=intent,
        outcome=outcome,
        items_served=items_served,
        items_referenced=items_referenced,
        unhelpful_item_ids=list(items_unhelpful or []),
        intent_family=intent_family,
    )
    record_feedback(
        feedback,
        log_dir=log_dir,
        event_log=event_log,
        pack_id=pack_id,
    )


# ---------------------------------------------------------------------------
# Bridge unit tests
# ---------------------------------------------------------------------------


class TestBuildLearningObservations:
    def test_empty_event_log_returns_empty(self, event_log) -> None:
        assert build_learning_observations_from_event_log(event_log) == []

    def test_pack_without_feedback_skipped(self, event_log, tmp_path: Path) -> None:
        """A PACK_ASSEMBLED with no matching FEEDBACK_RECORDED has no
        outcome to attribute and must be excluded."""
        _emit_pack_assembled(
            event_log,
            pack_id="pack-1",
            domain="x",
            intent="test",
            items=[{"item_id": "i1", "item_type": "entity"}],
        )
        observations = build_learning_observations_from_event_log(event_log)
        assert observations == []

    def test_feedback_without_pack_skipped(self, event_log, tmp_path: Path) -> None:
        """FEEDBACK_RECORDED whose pack_id has no matching pack event
        must be excluded — the bridge has nothing to attribute the
        outcome to."""
        feedback = PackFeedback(
            run_id="r1",
            phase="GENERATE",
            intent="test",
            outcome="success",
            items_served=["i1"],
            items_referenced=["i1"],
            intent_family="asset_generation",
        )
        record_feedback(feedback, log_dir=tmp_path, event_log=event_log, pack_id="x")
        observations = build_learning_observations_from_event_log(event_log)
        assert observations == []

    def test_governed_path_feedback_joins_when_it_names_a_pack(self, event_log) -> None:
        """The governed ``FEEDBACK_RECORD`` payload reaches the join.

        ``FeedbackRecordHandler`` used to emit exactly three keys and drop
        the ``pack_id`` the caller supplied through ``POST /feedback``, so
        this family of feedback could never join no matter what the caller
        sent. The payload shape is thinner than the ``PackFeedback`` one —
        no per-item citations — so it contributes a joined observation
        with no referenced items rather than nothing at all.
        """
        _emit_pack_assembled(
            event_log,
            pack_id="pack_gov",
            domain="platform",
            intent="ship the thing",
            items=[{"item_id": "i1", "item_type": "precedent"}],
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mutation_executor",
            entity_id="trace_1",
            payload={
                "target_id": "trace_1",
                "rating": 0.9,
                "comment": None,
                "success": True,
                "pack_id": "pack_gov",
            },
        )

        (observation,) = build_learning_observations_from_event_log(event_log)

        assert [item["item_id"] for item in observation["items"]] == ["i1"]
        # ``_join_one`` resolves a missing ``success`` to "failure", so the
        # handler has to emit it: forwarding only the join key would have
        # made a 0.9 grade join as a failed delivery.
        assert observation["outcome"] == "success"

    def test_governed_low_rating_joins_as_a_failure(self, event_log) -> None:
        _emit_pack_assembled(
            event_log,
            pack_id="pack_gov2",
            domain="platform",
            intent="ship the thing",
            items=[{"item_id": "i1", "item_type": "precedent"}],
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mutation_executor",
            entity_id="trace_1",
            payload={
                "target_id": "trace_1",
                "rating": 0.1,
                "comment": None,
                "success": False,
                "pack_id": "pack_gov2",
            },
        )

        (observation,) = build_learning_observations_from_event_log(event_log)

        assert observation["outcome"] == "failure"

    def test_join_produces_expected_observation_shape(
        self, event_log, tmp_path: Path
    ) -> None:
        """Round-trip: emit one pack + one feedback, observe the joined shape."""
        _emit_pack_assembled(
            event_log,
            pack_id="pack-1",
            domain="data",
            intent="generate sql",
            items=[
                {"item_id": "doc:foo", "item_type": "entity"},
                {"item_id": "doc:bar", "item_type": "entity"},
            ],
        )
        _record(
            event_log,
            tmp_path,
            pack_id="pack-1",
            run_id="run-1",
            intent="generate sql",
            intent_family="asset_generation",
            items_served=["doc:foo", "doc:bar"],
            items_referenced=["doc:foo"],
            outcome="success",
        )

        observations = build_learning_observations_from_event_log(event_log)
        assert len(observations) == 1
        obs = observations[0]
        assert obs["run_id"] == "run-1"
        assert obs["intent_family"] == "asset_generation"
        assert obs["outcome"] == "success"
        assert obs["domain"] == "data"
        assert {i["item_id"] for i in obs["items"]} == {"doc:foo", "doc:bar"}
        # The bridge maps PACK_ASSEMBLED's ``strategy_source`` field to
        # learning's ``source_strategy``.
        assert obs["items"][0]["source_strategy"] == "keyword"

    def test_strategy_source_mapping(self, event_log, tmp_path: Path) -> None:
        """``strategy_source`` (PackBuilder telemetry) must map to
        ``source_strategy`` (learning.scoring input). Catches the
        rename if either side drifts."""
        _emit_pack_assembled(
            event_log,
            pack_id="pack-1",
            domain="d",
            intent="i",
            items=[
                {
                    "item_id": "i1",
                    "item_type": "entity",
                    "strategy_source": "graph",
                }
            ],
        )
        _record(
            event_log,
            tmp_path,
            pack_id="pack-1",
            run_id="r1",
            intent="i",
            intent_family="x",
            items_served=["i1"],
            items_referenced=[],
            outcome="failure",
        )
        observations = build_learning_observations_from_event_log(event_log)
        assert observations[0]["items"][0]["source_strategy"] == "graph"

    def test_window_bounds_filter_old_events(self, event_log, tmp_path: Path) -> None:
        """``days`` window must filter; old events outside the window
        are dropped from both PACK_ASSEMBLED + FEEDBACK_RECORDED scans."""
        _emit_pack_assembled(
            event_log,
            pack_id="pack-1",
            domain="d",
            intent="i",
            items=[{"item_id": "i1", "item_type": "entity"}],
        )
        _record(
            event_log,
            tmp_path,
            pack_id="pack-1",
            run_id="r1",
            intent="i",
            intent_family="asset_generation",
            items_served=["i1"],
            items_referenced=["i1"],
            outcome="success",
        )
        # Days = 0 ⇒ since = now ⇒ window excludes events emitted just
        # now (they have occurred_at <= now). The bridge uses ``>=
        # since`` semantics in event_log.get_events; verify behaviour
        # matches expectation by passing a guaranteed-empty window.
        # SQLite ms-resolution timestamps make the boundary tight; a
        # negative-effective window is the safest pin.
        observations = build_learning_observations_from_event_log(event_log, days=-1)
        assert observations == []


# ---------------------------------------------------------------------------
# End-to-end promote chain
# ---------------------------------------------------------------------------


class TestPromoteChain:
    """Drive the complete promote half on EventLog data — mirrors the
    flow scenario 5.4 would use to surface promotion candidates from
    its 30-round corpus."""

    def test_consistent_success_promotes_guidance(
        self, event_log, tmp_path: Path, learning_registry: ParameterRegistry
    ) -> None:
        """An item that's helpful 4/4 times across distinct runs and
        is stamped item_type=guidance must surface a
        ``promote_guidance`` candidate."""
        for n in range(4):
            pack_id = f"pack-{n}"
            _emit_pack_assembled(
                event_log,
                pack_id=pack_id,
                domain="data",
                intent="generate sql",
                items=[
                    {
                        "item_id": "guidance:strong",
                        "item_type": "guidance",
                    }
                ],
            )
            _record(
                event_log,
                tmp_path,
                pack_id=pack_id,
                run_id=f"r{n}",
                intent="generate sql",
                intent_family="asset_generation",
                items_served=["guidance:strong"],
                items_referenced=["guidance:strong"],
                outcome="success",
            )

        observations = build_learning_observations_from_event_log(event_log)
        assert len(observations) == 4

        report = analyze_learning_observations(
            observations=observations,
            registry=learning_registry,
            min_support=2,
        )
        assert report["candidate_count"] == 1
        candidate = report["candidates"][0]
        assert candidate["item_id"] == "guidance:strong"
        assert candidate["recommendation_type"] == "promote_guidance"
        assert candidate["metrics"]["success_rate"] == 1.0

    def test_consistent_success_on_precedent_promotes_precedent(
        self, event_log, tmp_path: Path, learning_registry: ParameterRegistry
    ) -> None:
        """When ``item_type=precedent``, the recommendation flips to
        ``promote_precedent`` even with the same outcome shape — this
        is the production-code branch that 5.5.3's TODO calls out
        as currently unreachable through the standard strategy path."""
        for n in range(4):
            pack_id = f"pack-{n}"
            _emit_pack_assembled(
                event_log,
                pack_id=pack_id,
                domain="data",
                intent="generate sql",
                items=[{"item_id": "prec:winning", "item_type": "precedent"}],
            )
            _record(
                event_log,
                tmp_path,
                pack_id=pack_id,
                run_id=f"r{n}",
                intent="generate sql",
                intent_family="asset_generation",
                items_served=["prec:winning"],
                items_referenced=["prec:winning"],
                outcome="success",
            )

        observations = build_learning_observations_from_event_log(event_log)
        report = analyze_learning_observations(
            observations=observations,
            registry=learning_registry,
            min_support=2,
        )
        assert report["candidates"][0]["recommendation_type"] == "promote_precedent"

    def test_consistent_failure_flags_noise(
        self, event_log, tmp_path: Path, learning_registry: ParameterRegistry
    ) -> None:
        """Items in repeatedly failing packs surface as
        ``investigate_noise`` candidates."""
        for n in range(4):
            pack_id = f"pack-{n}"
            _emit_pack_assembled(
                event_log,
                pack_id=pack_id,
                domain="data",
                intent="generate sql",
                items=[{"item_id": "noisy:item", "item_type": "guidance"}],
            )
            _record(
                event_log,
                tmp_path,
                pack_id=pack_id,
                run_id=f"r{n}",
                intent="generate sql",
                intent_family="asset_generation",
                items_served=["noisy:item"],
                items_referenced=[],
                outcome="failure",
            )

        observations = build_learning_observations_from_event_log(event_log)
        report = analyze_learning_observations(
            observations=observations,
            registry=learning_registry,
            min_support=2,
        )
        assert report["candidates"][0]["recommendation_type"] == "investigate_noise"

    def test_full_promote_chain_round_trips_to_entity_payload(
        self, event_log, tmp_path: Path, learning_registry: ParameterRegistry
    ) -> None:
        """The whole loop: feedback → analyze → write artifacts → an
        operator approves one candidate → ``prepare_learning_promotions``
        emits a precedent entity payload + applies_to edge payload."""
        for n in range(4):
            pack_id = f"pack-{n}"
            _emit_pack_assembled(
                event_log,
                pack_id=pack_id,
                domain="data",
                intent="generate sql for the metrics_table",
                items=[
                    {
                        "item_id": "prec:metrics_table",
                        "item_type": "precedent",
                    }
                ],
            )
            _record(
                event_log,
                tmp_path,
                pack_id=pack_id,
                run_id=f"r{n}",
                intent="generate sql for the metrics_table",
                intent_family="asset_generation",
                items_served=["prec:metrics_table"],
                items_referenced=["prec:metrics_table"],
                outcome="success",
            )

        observations = build_learning_observations_from_event_log(event_log)
        report = analyze_learning_observations(
            observations=observations,
            registry=learning_registry,
            min_support=2,
        )
        assert report["candidate_count"] == 1
        candidate = report["candidates"][0]

        # Step 2: write artifacts to disk for human review.
        artifact_paths = write_learning_review_artifacts(
            report=report, output_dir=tmp_path / "review"
        )
        candidates_path = Path(artifact_paths["candidates_path"])
        decisions_template_path = Path(artifact_paths["decisions_template_path"])
        assert candidates_path.exists()
        assert decisions_template_path.exists()

        # Step 3: operator approves the candidate (rewrite the
        # template with approved=True).
        decisions = json.loads(decisions_template_path.read_text())
        decisions["decisions"][0]["approved"] = True
        decisions["decisions"][0]["rationale"] = "consistent winner across runs"

        # Step 4: prepare promotions → emits entity + edge payloads
        # ready for the governed mutation pipeline.
        promotions = prepare_learning_promotions(
            candidates_payload=json.loads(candidates_path.read_text()),
            decisions_payload=decisions,
        )
        assert promotions["approved_count"] == 1
        result = promotions["results"][0]
        assert result["status"] == "ready"
        entity_payload = result["entity_payload"]
        assert entity_payload["entity_type"] == "precedent"
        # entity_id is built by slugifying the candidate_id —
        # ``re.sub(r"[^a-zA-Z0-9]+", "-", candidate_id).lower()``. So
        # ``asset_generation:abc123`` becomes ``asset-generation-abc123``.
        # Pin the slugified form rather than the raw candidate_id.
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", candidate["candidate_id"]).lower()
        assert slug in entity_payload["entity_id"]

        # Edge payload set may be empty when no target_entity_ids were
        # carried; the candidate here didn't seed any. Either way the
        # contract is honoured: ``edge_payloads`` is a list.
        assert isinstance(result["edge_payloads"], list)


# ---------------------------------------------------------------------------
# Real PackBuilder → join (no hand-written PACK_ASSEMBLED payload)
# ---------------------------------------------------------------------------


class TestRealPackBuilderAttribution:
    """Drive the join off telemetry PackBuilder actually emitted.

    Every other test in this module hand-writes the PACK_ASSEMBLED payload,
    so the join stayed green while production emitted none of the
    attribution it reads: candidates came out with ``category=None`` and
    everything bucketed into ``general_context`` (``run_id`` ``unknown-run``).
    This class assembles real packs and asserts the candidate is described.
    """

    @staticmethod
    def _build_pack(event_log: SQLiteEventLog, *, run_id: str) -> str:
        item = PackItem(
            item_id="doc:deploy-runbook",
            item_type="document",
            excerpt="rollback steps",
            relevance_score=0.9,
            metadata={
                # Shape KeywordSearch produces: its own strategy stamp plus
                # the document's stored metadata.
                "source_strategy": "keyword",
                "title": "Deploy runbook",
                "source_system": "dbt",
                "content_tags": {"content_type": "procedure"},
            },
        )
        strategy = MagicMock(spec=SearchStrategy)
        strategy.name = "keyword"
        strategy.search.return_value = [item]
        builder = PackBuilder(strategies=[strategy], event_log=event_log)
        pack = builder.build(
            "validate the deploy convention", domain="platform", run_id=run_id
        )
        return pack.pack_id

    def test_candidate_carries_category_and_intent_family(
        self, event_log, tmp_path: Path, learning_registry: ParameterRegistry
    ) -> None:
        # Two runs so the candidate clears the default min_support of 2.
        for run in ("run-a", "run-b"):
            pack_id = self._build_pack(event_log, run_id=run)
            # Mirrors the MCP feedback path, which carries neither a
            # run_id nor an intent_family — the pack payload is the only
            # source for both.
            _record(
                event_log,
                tmp_path,
                pack_id=pack_id,
                run_id="",
                intent="validate the deploy convention",
                intent_family="",
                items_served=["doc:deploy-runbook"],
                items_referenced=["doc:deploy-runbook"],
                outcome="success",
            )

        observations = build_learning_observations_from_event_log(event_log)
        assert len(observations) == 2
        assert {obs["run_id"] for obs in observations} == {"run-a", "run-b"}

        report = analyze_learning_observations(
            observations=observations, registry=learning_registry
        )
        assert report["candidate_count"] == 1
        candidate = report["candidates"][0]
        # The payoff: a described candidate in a real bucket, not an
        # opaque item_id in "general_context".
        assert candidate["intent_family"] == "validation_diagnostics"
        assert candidate["category"] == "procedure"
        assert candidate["title"] == "Deploy runbook"
        # Provenance keeps both vocabularies; the domain the pack was
        # actually served for stays separately addressable.
        assert candidate["domain_systems"] == ["dbt", "platform"]
        assert candidate["primary_domain"] == "platform"
        assert candidate["supporting_run_ids"] == ["run-a", "run-b"]
        assert candidate["source_strategies"] == {"keyword": 2}
        assert candidate["precedent_name"] == (
            "Learning: validation_diagnostics :: Deploy runbook"
        )

    def test_feedback_run_id_wins_over_pack_run_id(
        self, event_log, tmp_path: Path
    ) -> None:
        """The pack payload is a fallback, not an override — feedback is
        the closer witness to the run that consumed the pack."""
        pack_id = self._build_pack(event_log, run_id="pack-run")
        _record(
            event_log,
            tmp_path,
            pack_id=pack_id,
            run_id="feedback-run",
            intent="validate the deploy convention",
            intent_family="",
            items_served=["doc:deploy-runbook"],
            items_referenced=["doc:deploy-runbook"],
            outcome="success",
        )
        observations = build_learning_observations_from_event_log(event_log)
        assert observations[0]["run_id"] == "feedback-run"

    def test_promoted_precedent_is_filed_under_the_pack_domain(
        self, event_log, tmp_path: Path, learning_registry: ParameterRegistry
    ) -> None:
        """A per-item ``source_system`` must not hijack the lesson's domain.

        The promotion event's ``domain`` is what ``get_lessons(domain=...)``
        / ``list_precedents(domain=...)`` filter on. Now that items carry
        ``domain_system``, the provenance list holds both ``"dbt"`` and
        ``"platform"`` — and ``dbt`` sorts first, so picking off that list
        would file a platform lesson where no operator would look for it.
        """
        from trellis.learning import (
            build_learning_promotion_payloads,
            submit_learning_promotion,
        )
        from trellis.mutate import build_curate_executor
        from trellis.retrieve.precedents import list_precedents
        from trellis.stores.registry import StoreRegistry

        for run in ("run-a", "run-b"):
            pack_id = self._build_pack(event_log, run_id=run)
            _record(
                event_log,
                tmp_path,
                pack_id=pack_id,
                run_id=run,
                intent="validate the deploy convention",
                intent_family="",
                items_served=["doc:deploy-runbook"],
                items_referenced=["doc:deploy-runbook"],
                outcome="success",
            )
        report = analyze_learning_observations(
            observations=build_learning_observations_from_event_log(event_log),
            registry=learning_registry,
        )
        payloads = build_learning_promotion_payloads(
            candidate=report["candidates"][0],
            promotion_name="Deploy runbook",
            rationale="Consistently precedes clean deploys.",
        )

        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        registry = StoreRegistry(stores_dir=stores_dir)
        outcome = submit_learning_promotion(
            build_curate_executor(registry),
            payloads["entity_payload"],
            payloads["edge_payloads"],
            requested_by="test:promote-learning",
        )
        assert outcome["status"] == "promoted"

        promotion_log = registry.operational.event_log
        assert len(list_precedents(promotion_log, domain="platform")) == 1
        assert list_precedents(promotion_log, domain="dbt") == []


# ---------------------------------------------------------------------------
# The ranking metrics, and which of them anything can answer
# ---------------------------------------------------------------------------


class TestObservableMetrics:
    """Three of the four ranking metrics read keys no producer writes.

    ``analyze_learning_observations`` ranks candidates on ``success_rate``,
    ``retry_rate``, ``injection_rate`` and ``avg_selection_efficiency``.
    Only the first is answerable: ``selection_efficiency`` was read off a
    ``PACK_ASSEMBLED`` key ``_emit_telemetry`` has never written, and
    ``had_retry`` / ``injected`` off ``FEEDBACK_RECORDED`` keys that no
    feedback surface produces — :class:`PackFeedback` carries neither
    field, and its ``metadata`` lands under a nested ``metadata`` key
    rather than flattened, so a caller cannot smuggle one in either.

    Each unanswerable metric then divided by ``times_served`` and reported
    a confident ``0.0``. That is the failure this repo keeps producing —
    a measurement path wired to a constant — and it is worse than a
    missing metric, because ``retry_rate=0.0`` reads as *measured, clean*
    and is the exact value that satisfies the promote conjunct.

    These tests therefore assert **derivation and reachability**, never
    key presence: a red test that only demands a key gets greened by
    someone stamping a constant at emission, which is how the bug arrived.
    """

    @staticmethod
    def _build_pack_with(
        event_log: SQLiteEventLog,
        *,
        run_id: str,
        candidates: int,
        max_items: int,
    ) -> str:
        """Assemble a real pack from ``candidates`` items, admitting
        ``max_items`` of them. The rest are rejected under ``max_items``,
        so the emitted payload carries both sides of the ratio."""
        from trellis.schemas.pack import PackBudget

        items = [
            PackItem(
                item_id=f"doc:runbook-{n}",
                item_type="document",
                excerpt=f"rollback procedure number {n} for the deploy pipeline",
                relevance_score=1.0 - (n / 100),
                metadata={"source_strategy": "keyword", "title": f"Runbook {n}"},
            )
            for n in range(candidates)
        ]
        strategy = MagicMock(spec=SearchStrategy)
        strategy.name = "keyword"
        strategy.search.return_value = items
        builder = PackBuilder(strategies=[strategy], event_log=event_log)
        pack = builder.build(
            "validate the deploy convention",
            domain="platform",
            run_id=run_id,
            budget=PackBudget(max_items=max_items),
        )
        return pack.pack_id

    @staticmethod
    def _pack_payload(event_log: SQLiteEventLog, pack_id: str) -> dict:
        events = event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, entity_id=pack_id, limit=10
        )
        return dict(events[0].payload)

    # -- selection_efficiency --------------------------------------------

    def test_selection_efficiency_is_derived_from_the_two_candidate_counts(
        self, event_log, tmp_path: Path
    ) -> None:
        """Served over seen, computed from the payload's own two lists.

        The number is asserted *and* so is the arithmetic that produced
        it, because a stamped ``"selection_efficiency": 0.3`` would
        satisfy the former alone. Ten candidates with ``max_items=3``
        gives 3 admitted and 7 rejected, so the ratio is exactly 0.3 and
        no other quantity on the row equals it.
        """
        pack_id = self._build_pack_with(
            event_log, run_id="run-a", candidates=10, max_items=3
        )
        payload = self._pack_payload(event_log, pack_id)
        assert len(payload["injected_items"]) == 3
        assert len(payload["rejected_items"]) == 7
        assert {r["reason"] for r in payload["rejected_items"]} == {"max_items"}

        assert derive_selection_efficiency(payload) == pytest.approx(0.3)

        _record(
            event_log,
            tmp_path,
            pack_id=pack_id,
            run_id="run-a",
            intent="validate the deploy convention",
            intent_family="deploy_validation",
            items_served=["doc:runbook-0"],
            items_referenced=["doc:runbook-0"],
            outcome="success",
        )
        observation = build_learning_observations_from_event_log(event_log)[0]
        assert observation["selection_efficiency"] == pytest.approx(0.3)

    def test_rejecting_nothing_is_full_efficiency_not_unobserved(
        self, event_log
    ) -> None:
        """A present-but-empty ``rejected_items`` is a measurement.

        The walk saw two candidates and admitted both: efficiency 1.0.
        Reporting that as unobserved would discard a real observation,
        which is the mirror of the bug being fixed rather than a fix.
        """
        pack_id = self._build_pack_with(
            event_log, run_id="run-a", candidates=2, max_items=10
        )
        payload = self._pack_payload(event_log, pack_id)
        assert payload["rejected_items"] == []
        assert derive_selection_efficiency(payload) == pytest.approx(1.0)

    def test_a_row_with_no_rejected_items_key_is_unobserved_not_zero(
        self, event_log, tmp_path: Path, learning_registry: ParameterRegistry
    ) -> None:
        """An absent key means the row cannot answer, so nothing is
        reported — and the coverage block says how many rows could."""
        for run in ("run-a", "run-b"):
            pack_id = f"pack-{run}"
            _emit_pack_assembled(
                event_log,
                pack_id=pack_id,
                domain="platform",
                intent="validate the deploy convention",
                items=[{"item_id": "doc:runbook-0", "item_type": "document"}],
            )
            _record(
                event_log,
                tmp_path,
                pack_id=pack_id,
                run_id=run,
                intent="validate the deploy convention",
                intent_family="deploy_validation",
                items_served=["doc:runbook-0"],
                items_referenced=["doc:runbook-0"],
                outcome="success",
            )
        observations = build_learning_observations_from_event_log(event_log)
        assert all("selection_efficiency" not in obs for obs in observations)

        report = analyze_learning_observations(
            observations=observations, registry=learning_registry
        )
        candidate = report["candidates"][0]
        assert candidate["metrics"]["avg_selection_efficiency"] is None
        assert candidate["metrics_coverage"]["selection_efficiency_observed"] == 0
        assert candidate["metrics_coverage"]["observations"] == 2

    # -- had_retry / injected --------------------------------------------

    def test_real_feedback_path_reports_retry_and_injection_as_unmeasured(
        self, event_log, tmp_path: Path, learning_registry: ParameterRegistry
    ) -> None:
        """Through the surface production actually uses, both are ``None``.

        Driven off :func:`record_feedback` rather than a hand-written
        payload, so this measures what the deployed feedback family emits.
        It goes red when a real producer for either field is added — which
        is the moment a human should look at the classifier again.
        """
        for run in ("run-a", "run-b"):
            pack_id = self._build_pack_with(
                event_log, run_id=run, candidates=4, max_items=2
            )
            _record(
                event_log,
                tmp_path,
                pack_id=pack_id,
                run_id=run,
                intent="validate the deploy convention",
                intent_family="deploy_validation",
                items_served=["doc:runbook-0"],
                items_referenced=["doc:runbook-0"],
                outcome="success",
            )
        observations = build_learning_observations_from_event_log(event_log)
        assert all("had_retry" not in obs for obs in observations)
        assert all("injected" not in obs for obs in observations)

        report = analyze_learning_observations(
            observations=observations, registry=learning_registry
        )
        candidate = report["candidates"][0]
        assert candidate["metrics"]["retry_rate"] is None
        assert candidate["metrics"]["injection_rate"] is None
        assert candidate["metrics_coverage"]["retry_observed"] == 0
        assert candidate["metrics_coverage"]["injected_observed"] == 0
        # The one metric that *is* answerable keeps its value, so the
        # candidate is still rankable — this is a reporting fix, not a
        # withdrawal of the signal.
        assert candidate["metrics"]["success_rate"] == pytest.approx(1.0)
        assert candidate["metrics"]["avg_selection_efficiency"] == pytest.approx(0.5)

    def test_the_retry_conjunct_is_reachable_and_flips_the_verdict(
        self, event_log, tmp_path: Path, learning_registry: ParameterRegistry
    ) -> None:
        """Anti-vacuity: prove the rule still has two variables.

        Dropping an unmeasured conjunct is only safe if the conjunct comes
        back when something measures it. The same four servings are scored
        twice — once as the real feedback path emits them, once with a
        hypothetical producer stamping ``had_retry`` on two of the four.
        Success is 4/4 either way, so ``success_rate`` alone cannot
        explain the difference: the first run promotes, the second is
        ``investigate_noise`` on the retry disjunct alone (0.5 >= the 0.5
        noise threshold, while success 1.0 is far above the 0.4 one).

        The ``had_retry`` arm is synthesized directly onto the event
        because no surface produces it. That is the point of the test: it
        is the contract a producer would have to meet, pinned before one
        exists rather than after.
        """

        def _score(*, with_retry: bool) -> str:
            log = SQLiteEventLog(tmp_path / f"retry-{with_retry}.db")
            try:
                for n in range(4):
                    pack_id = self._build_pack_with(
                        log, run_id=f"run-{n}", candidates=4, max_items=2
                    )
                    payload = {
                        "feedback_id": f"fb-{n}",
                        "run_id": f"run-{n}",
                        "pack_id": pack_id,
                        "intent_family": "deploy_validation",
                        "outcome": "success",
                        "success": True,
                        "helpful_item_ids": ["doc:runbook-0"],
                    }
                    if with_retry:
                        payload["had_retry"] = n < 2
                    log.emit(
                        EventType.FEEDBACK_RECORDED,
                        source="test:hypothetical-producer",
                        entity_id=pack_id,
                        entity_type="pack",
                        payload=payload,
                    )
                observations = build_learning_observations_from_event_log(log)
                assert len(observations) == 4
                report = analyze_learning_observations(
                    observations=observations, registry=learning_registry
                )
                candidate = report["candidates"][0]
                assert candidate["metrics"]["success_rate"] == pytest.approx(1.0)
                if with_retry:
                    assert candidate["metrics"]["retry_rate"] == pytest.approx(0.5)
                    assert candidate["metrics_coverage"]["retry_observed"] == 4
                else:
                    assert candidate["metrics"]["retry_rate"] is None
                return str(candidate["recommendation_type"])
            finally:
                log.close()

        assert _score(with_retry=False) == "promote_guidance"
        assert _score(with_retry=True) == "investigate_noise"

    def test_promotion_description_states_retry_as_unmeasured(
        self, event_log, tmp_path: Path, learning_registry: ParameterRegistry
    ) -> None:
        """A promotion mints a durable node read back as context.

        Its description must not assert ``retry_rate=0.0`` for a quantity
        nothing measured — that would persist a fabricated measurement
        into the graph, where no later reader can tell it from a real one.
        """
        from trellis.learning import build_learning_promotion_payloads

        for run in ("run-a", "run-b"):
            pack_id = self._build_pack_with(
                event_log, run_id=run, candidates=4, max_items=2
            )
            _record(
                event_log,
                tmp_path,
                pack_id=pack_id,
                run_id=run,
                intent="validate the deploy convention",
                intent_family="deploy_validation",
                items_served=["doc:runbook-0"],
                items_referenced=["doc:runbook-0"],
                outcome="success",
            )
        report = analyze_learning_observations(
            observations=build_learning_observations_from_event_log(event_log),
            registry=learning_registry,
        )
        payloads = build_learning_promotion_payloads(
            candidate=report["candidates"][0],
            promotion_name="Deploy runbook",
            rationale="Consistently precedes clean deploys.",
        )
        description = payloads["entity_payload"]["properties"]["description"]
        assert "retry_rate not measured" in description
        assert "retry_rate=0.0" not in description
        assert payloads["entity_payload"]["properties"]["retry_rate"] is None


class TestCitationAttributionAtTheJoin:
    """Per-item verdicts must survive the PACK ⋈ FEEDBACK join.

    Before this, the join carried ``helpful_item_ids`` no further than the
    pack-level ``outcome``: every item of a graded pack inherited the same
    verdict, so the scorer could not tell an item the grader *named* from
    one that merely rode along. #336's rule needs the distinction.
    """

    def _pack(self, event_log: SQLiteEventLog, pack_id: str) -> None:
        _emit_pack_assembled(
            event_log,
            pack_id=pack_id,
            domain="deploy",
            intent="ship the thing",
            items=[
                {"item_id": "doc:a", "item_type": "document"},
                {"item_id": "doc:b", "item_type": "document"},
                {"item_id": "doc:c", "item_type": "document"},
            ],
        )

    def test_citations_stamp_only_the_items_they_name(
        self, event_log: SQLiteEventLog, tmp_path: Path
    ) -> None:
        self._pack(event_log, "pack-1")
        _record(
            event_log,
            tmp_path,
            pack_id="pack-1",
            run_id="run-1",
            intent="ship the thing",
            intent_family="deploy",
            items_served=["doc:a", "doc:b", "doc:c"],
            items_referenced=["doc:a"],
            items_unhelpful=["doc:b"],
            outcome="success",
        )
        (observation,) = build_learning_observations_from_event_log(event_log)
        verdicts = {
            item["item_id"]: (item["cited_helpful"], item["cited_unhelpful"])
            for item in observation["items"]
        }
        assert verdicts == {
            "doc:a": (True, False),
            "doc:b": (False, True),
            # Served and ungraded — the majority case, and the one #336
            # showed must never be read as a negative verdict.
            "doc:c": (False, False),
        }
        assert observation["citation_attributed"] is True

    def test_an_ungraded_pack_is_not_attributed(
        self, event_log: SQLiteEventLog, tmp_path: Path
    ) -> None:
        self._pack(event_log, "pack-2")
        _record(
            event_log,
            tmp_path,
            pack_id="pack-2",
            run_id="run-2",
            intent="ship the thing",
            intent_family="deploy",
            items_served=["doc:a", "doc:b", "doc:c"],
            items_referenced=[],
            outcome="success",
        )
        (observation,) = build_learning_observations_from_event_log(event_log)
        assert observation["citation_attributed"] is False
        assert not any(
            item["cited_helpful"] or item["cited_unhelpful"]
            for item in observation["items"]
        )

    def test_an_id_that_matches_nothing_still_counts_as_attributed(
        self, event_log: SQLiteEventLog, tmp_path: Path
    ) -> None:
        """Coverage is about the grading surface, not about resolution.

        A grader naming ids that miss every served item is still a grader
        that graded — which is the #309 signal ``citation_attributed``
        feeds. Resolving it to nothing is a separate (and quieter) defect.
        """
        self._pack(event_log, "pack-3")
        _record(
            event_log,
            tmp_path,
            pack_id="pack-3",
            run_id="run-3",
            intent="ship the thing",
            intent_family="deploy",
            items_served=["doc:a"],
            items_referenced=["doc:nonexistent"],
            outcome="success",
        )
        (observation,) = build_learning_observations_from_event_log(event_log)
        assert observation["citation_attributed"] is True
        assert not any(item["cited_helpful"] for item in observation["items"])

    def test_a_bare_string_is_not_iterated_into_characters(
        self, event_log: SQLiteEventLog
    ) -> None:
        """``"a"`` is a string, not the one-element list ``["a"]``.

        Iterating it would manufacture a citation for every single-character
        item id in the pack, which is a wrong verdict rather than a missing
        one. Emitted raw because no writer in the tree produces this shape —
        only a hand-edited or third-party event would.
        """
        _emit_pack_assembled(
            event_log,
            pack_id="pack-4",
            domain="deploy",
            intent="ship the thing",
            items=[{"item_id": "a", "item_type": "document"}],
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id="fb-4",
            entity_type="feedback",
            payload={
                "pack_id": "pack-4",
                "intent_family": "deploy",
                "success": True,
                "helpful_item_ids": "a",
            },
        )
        (observation,) = build_learning_observations_from_event_log(event_log)
        assert observation["items"][0]["cited_helpful"] is False
        assert observation["citation_attributed"] is False

    def test_blank_and_null_ids_are_dropped(self, event_log: SQLiteEventLog) -> None:
        _emit_pack_assembled(
            event_log,
            pack_id="pack-5",
            domain="deploy",
            intent="ship the thing",
            items=[{"item_id": "doc:a", "item_type": "document"}],
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id="fb-5",
            entity_type="feedback",
            payload={
                "pack_id": "pack-5",
                "intent_family": "deploy",
                "success": True,
                "helpful_item_ids": ["  ", None],
                "unhelpful_item_ids": [" doc:a "],
            },
        )
        (observation,) = build_learning_observations_from_event_log(event_log)
        # Whitespace is stripped on both sides, so a padded id still lands.
        assert observation["items"][0]["cited_unhelpful"] is True
        assert observation["items"][0]["cited_helpful"] is False
        assert observation["citation_attributed"] is True
