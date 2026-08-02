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
) -> None:
    feedback = PackFeedback(
        run_id=run_id,
        phase=phase,
        intent=intent,
        outcome=outcome,
        items_served=items_served,
        items_referenced=items_referenced,
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
