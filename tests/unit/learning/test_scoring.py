"""Unit tests for trellis.learning.scoring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.learning._registry_fixture import build_seeded_registry
from trellis.learning.evidence_gate import (
    MIN_ATTRIBUTED_OBSERVATIONS,
    NOT_SCREENED,
    REFUSED_CONTESTED,
    REFUSED_NO_UNHELPFUL,
    REFUSED_THIN_CORPUS,
)
from trellis.learning.scoring import (
    LEARNING_NOISE_SUCCESS_KEY,
    LEARNING_PROMOTE_SUCCESS_KEY,
    analyze_learning_observations,
    build_learning_promotion_payloads,
    normalize_intent_family,
    prepare_learning_promotions,
    write_learning_review_artifacts,
)
from trellis.ops import ParameterRegistry


@pytest.fixture
def learning_registry() -> ParameterRegistry:
    """Default registry seeded with the historical learning thresholds."""
    return build_seeded_registry()


# ---------------------------------------------------------------------------
# normalize_intent_family
# ---------------------------------------------------------------------------


class TestNormalizeIntentFamily:
    def test_phase_map_takes_priority(self) -> None:
        result = normalize_intent_family(
            phase="GENERATE_ASSETS",
            intent="something unrelated",
            phase_family_map={"GENERATE_ASSETS": "asset_generation"},
        )
        assert result == "asset_generation"

    def test_phase_map_missing_key_falls_through_to_keyword(self) -> None:
        result = normalize_intent_family(
            phase="UNKNOWN_PHASE",
            intent="generate sql for the pipeline",
            phase_family_map={"GENERATE_ASSETS": "asset_generation"},
        )
        assert result == "asset_generation"

    def test_keyword_match_analyze(self) -> None:
        assert (
            normalize_intent_family(intent="analyze the source table")
            == "source_analysis"
        )

    def test_keyword_match_discover(self) -> None:
        assert (
            normalize_intent_family(intent="discover schema details")
            == "source_discovery"
        )

    def test_keyword_match_plan(self) -> None:
        assert (
            normalize_intent_family(intent="plan the pipeline design")
            == "pipeline_planning"
        )

    def test_keyword_match_generate(self) -> None:
        assert (
            normalize_intent_family(intent="generate pyspark code")
            == "asset_generation"
        )

    def test_keyword_match_validate(self) -> None:
        assert (
            normalize_intent_family(intent="validate pii quality")
            == "validation_diagnostics"
        )

    def test_keyword_match_eda(self) -> None:
        assert (
            normalize_intent_family(intent="eda drift detection") == "eda_investigation"
        )

    def test_fallback_no_match(self) -> None:
        assert (
            normalize_intent_family(intent="something completely unrelated")
            == "general_context"
        )

    def test_no_args_returns_general_context(self) -> None:
        assert normalize_intent_family() == "general_context"

    def test_phase_map_none_uses_keyword(self) -> None:
        result = normalize_intent_family(
            phase="PLAN_SOLUTION", phase_family_map=None, intent="plan the lineage"
        )
        assert result == "pipeline_planning"


# ---------------------------------------------------------------------------
# analyze_learning_observations
# ---------------------------------------------------------------------------


def _make_observation(
    *,
    run_id: str = "run-1",
    intent_family: str = "asset_generation",
    outcome: str = "success",
    had_retry: bool = False,
    injected: bool = False,
    phase: str = "GENERATE",
    items: list | None = None,
    seed_entity_ids: list | None = None,
    selection_efficiency: float | None = None,
    citation_attributed: bool = False,
) -> dict:
    obs: dict = {
        "run_id": run_id,
        "intent_family": intent_family,
        "outcome": outcome,
        "had_retry": had_retry,
        "injected": injected,
        "phase": phase,
        "items": items or [],
        "seed_entity_ids": seed_entity_ids or [],
    }
    if selection_efficiency is not None:
        obs["selection_efficiency"] = selection_efficiency
    if citation_attributed:
        obs["citation_attributed"] = True
    return obs


def _make_item(
    item_id: str = "item-abc",
    item_type: str = "guidance",
    title: str = "Some Guidance",
    source_strategy: str = "keyword",
    *,
    cited_helpful: bool = False,
    cited_unhelpful: bool = False,
) -> dict:
    return {
        "item_id": item_id,
        "item_type": item_type,
        "title": title,
        "source_strategy": source_strategy,
        "cited_helpful": cited_helpful,
        "cited_unhelpful": cited_unhelpful,
    }


class TestAnalyzeLearningObservations:
    def test_empty_observations(self, learning_registry: ParameterRegistry) -> None:
        result = analyze_learning_observations(
            observations=[], registry=learning_registry
        )
        assert result["observation_count"] == 0
        assert result["candidate_count"] == 0
        assert result["candidates"] == []

    def test_min_support_filters_single_observation(
        self, learning_registry: ParameterRegistry
    ) -> None:
        obs = _make_observation(items=[_make_item()])
        result = analyze_learning_observations(
            observations=[obs], registry=learning_registry, min_support=2
        )
        assert result["candidate_count"] == 0

    def test_min_support_passes_with_enough_observations(
        self, learning_registry: ParameterRegistry
    ) -> None:
        item = _make_item()
        obs1 = _make_observation(run_id="run-1", items=[item])
        obs2 = _make_observation(run_id="run-2", items=[item])
        result = analyze_learning_observations(
            observations=[obs1, obs2], registry=learning_registry, min_support=2
        )
        assert result["candidate_count"] == 1

    def test_success_rate_computed_correctly(
        self, learning_registry: ParameterRegistry
    ) -> None:
        # 4 successes out of 4 => success_rate=1.0, which crosses the 0.75 promote
        # threshold so a candidate is produced for inspection.
        item = _make_item(item_id="x")
        obs1 = _make_observation(run_id="r1", outcome="success", items=[item])
        obs2 = _make_observation(run_id="r2", outcome="success", items=[item])
        obs3 = _make_observation(run_id="r3", outcome="success", items=[item])
        obs4 = _make_observation(run_id="r4", outcome="success", items=[item])
        result = analyze_learning_observations(
            observations=[obs1, obs2, obs3, obs4],
            registry=learning_registry,
            min_support=1,
        )
        candidate = result["candidates"][0]
        assert candidate["metrics"]["success_rate"] == pytest.approx(1.0, rel=1e-3)
        assert candidate["metrics"]["times_served"] == 4

    def test_promote_guidance_recommendation_for_high_success_rate(
        self, learning_registry: ParameterRegistry
    ) -> None:
        item = _make_item(item_type="guidance")
        obs1 = _make_observation(run_id="r1", outcome="success", items=[item])
        obs2 = _make_observation(run_id="r2", outcome="success", items=[item])
        obs3 = _make_observation(run_id="r3", outcome="success", items=[item])
        result = analyze_learning_observations(
            observations=[obs1, obs2, obs3],
            registry=learning_registry,
            min_support=1,
        )
        assert result["candidates"][0]["recommendation_type"] == "promote_guidance"

    def test_promote_precedent_recommendation_for_precedent_type(
        self, learning_registry: ParameterRegistry
    ) -> None:
        item = _make_item(item_type="precedent")
        obs1 = _make_observation(run_id="r1", outcome="success", items=[item])
        obs2 = _make_observation(run_id="r2", outcome="success", items=[item])
        obs3 = _make_observation(run_id="r3", outcome="success", items=[item])
        result = analyze_learning_observations(
            observations=[obs1, obs2, obs3],
            registry=learning_registry,
            min_support=1,
        )
        assert result["candidates"][0]["recommendation_type"] == "promote_precedent"

    def test_investigate_noise_for_low_success_rate(
        self, learning_registry: ParameterRegistry
    ) -> None:
        item = _make_item()
        obs1 = _make_observation(run_id="r1", outcome="failure", items=[item])
        obs2 = _make_observation(run_id="r2", outcome="failure", items=[item])
        obs3 = _make_observation(run_id="r3", outcome="failure", items=[item])
        result = analyze_learning_observations(
            observations=[obs1, obs2, obs3],
            registry=learning_registry,
            min_support=1,
        )
        assert result["candidates"][0]["recommendation_type"] == "investigate_noise"

    def test_mid_range_success_rate_excluded(
        self, learning_registry: ParameterRegistry
    ) -> None:
        # success_rate ~0.6, retry_rate 0 — no action taken, candidate excluded
        item = _make_item()
        obss = [
            _make_observation(
                run_id=f"r{i}", outcome="success" if i < 3 else "failure", items=[item]
            )
            for i in range(5)
        ]
        result = analyze_learning_observations(
            observations=obss, registry=learning_registry, min_support=1
        )
        # 3/5 = 0.6 success, retry_rate=0 => no action (between thresholds)
        assert result["candidate_count"] == 0

    def test_artifacts_root_stored(self, learning_registry: ParameterRegistry) -> None:
        result = analyze_learning_observations(
            observations=[],
            registry=learning_registry,
            artifacts_root="/var/data/artifacts",
        )
        assert result["artifacts_root"] == "/var/data/artifacts"

    def test_artifacts_root_none_by_default(
        self, learning_registry: ParameterRegistry
    ) -> None:
        result = analyze_learning_observations(
            observations=[], registry=learning_registry
        )
        assert result["artifacts_root"] is None

    def test_selection_efficiency_averaged(
        self, learning_registry: ParameterRegistry
    ) -> None:
        item = _make_item()
        obs1 = _make_observation(
            run_id="r1", outcome="success", items=[item], selection_efficiency=0.8
        )
        obs2 = _make_observation(
            run_id="r2", outcome="success", items=[item], selection_efficiency=0.6
        )
        obs3 = _make_observation(
            run_id="r3", outcome="success", items=[item], selection_efficiency=0.4
        )
        result = analyze_learning_observations(
            observations=[obs1, obs2, obs3],
            registry=learning_registry,
            min_support=1,
        )
        assert result["candidates"][0]["metrics"][
            "avg_selection_efficiency"
        ] == pytest.approx(0.6, rel=1e-3)

    def test_supporting_run_ids_aggregated(
        self, learning_registry: ParameterRegistry
    ) -> None:
        item = _make_item()
        obs1 = _make_observation(run_id="run-alpha", outcome="success", items=[item])
        obs2 = _make_observation(run_id="run-beta", outcome="success", items=[item])
        obs3 = _make_observation(run_id="run-gamma", outcome="success", items=[item])
        result = analyze_learning_observations(
            observations=[obs1, obs2, obs3],
            registry=learning_registry,
            min_support=1,
        )
        run_ids = result["candidates"][0]["supporting_run_ids"]
        assert sorted(run_ids) == ["run-alpha", "run-beta", "run-gamma"]


class TestRegistryContract:
    """POC directive: scoring must raise on missing registry / keys.

    Plan §6 in docs/design/plan-parameter-registry-wiring.md requires three
    scoring-layer assertions: missing-kwarg TypeError, missing-key KeyError,
    and override responsiveness. The CLI WARN/config tests live in
    tests/unit/cli/test_analyze.py.
    """

    def test_analyze_learning_observations_requires_registry(self) -> None:
        # Per POC directive (plan-self-improvement-program.md §2 "loud on
        # misuse"), omitting the required registry kwarg must raise rather
        # than silently substitute hard-coded defaults.
        with pytest.raises(TypeError, match="registry"):
            analyze_learning_observations(observations=[])  # type: ignore[call-arg]

    def test_analyze_learning_observations_raises_on_missing_key(self) -> None:
        # A registry whose snapshot lacks a required key must surface
        # KeyError naming the missing key — never a silent fallback.
        empty_registry = build_seeded_registry(replace=True)
        item = _make_item(item_type="guidance")
        obs1 = _make_observation(run_id="r1", outcome="success", items=[item])
        obs2 = _make_observation(run_id="r2", outcome="success", items=[item])
        obs3 = _make_observation(run_id="r3", outcome="success", items=[item])
        with pytest.raises(KeyError) as exc_info:
            analyze_learning_observations(
                observations=[obs1, obs2, obs3],
                registry=empty_registry,
                min_support=1,
            )
        # The missing-key message must name the offending key so the
        # operator knows which value to seed.
        assert LEARNING_PROMOTE_SUCCESS_KEY in str(exc_info.value)

    def test_recommendation_uses_registry_threshold(self) -> None:
        # Verify the registry value drives the cut-off: pick a
        # success_rate of 0.6 — above the default noise threshold (0.4)
        # so it lands in the neutral band, but below an override of 0.99
        # so it must be classified as noise.
        mid_item = _make_item(item_id="mid", item_type="guidance")
        mid_obss = [
            _make_observation(
                run_id=f"r{i}",
                outcome="success" if i < 3 else "failure",
                items=[mid_item],
            )
            for i in range(5)
        ]

        # Under defaults: 0.6 success, retry_rate=0 ⇒ neutral, no candidate.
        default_run = analyze_learning_observations(
            observations=mid_obss,
            registry=build_seeded_registry(),
            min_support=1,
        )
        assert default_run["candidate_count"] == 0

        # With noise_success_threshold=0.99: 0.6 <= 0.99 ⇒ investigate_noise.
        flipped_run = analyze_learning_observations(
            observations=mid_obss,
            registry=build_seeded_registry(
                overrides={LEARNING_NOISE_SUCCESS_KEY: 0.99},
            ),
            min_support=1,
        )
        assert (
            flipped_run["candidates"][0]["recommendation_type"] == "investigate_noise"
        )


# ---------------------------------------------------------------------------
# prepare_learning_promotions
# ---------------------------------------------------------------------------


def _make_promotable_candidate(
    candidate_id: str = "asset_generation:abc123",
    recommendation_type: str = "promote_guidance",
    intent_family: str = "asset_generation",
) -> dict:
    return {
        "candidate_id": candidate_id,
        "intent_family": intent_family,
        "recommendation_type": recommendation_type,
        "item_id": "item-x",
        "item_type": "guidance",
        "title": "Test Guidance",
        "precedent_name": f"Learning: {intent_family} :: Test Guidance",
        "precedent_properties": {
            "category": "retrieval_guidance",
            "intent_family": intent_family,
            "source_item_id": "item-x",
            "source_item_type": "guidance",
            "success_rate": 0.9,
            "retry_rate": 0.1,
            "support_count": 5,
            "source_of_truth": "reviewed_promotion",
        },
        "target_entity_ids": ["entity://foo"],
        "supporting_run_ids": ["run-1"],
        "phases": ["GENERATE"],
        "metrics": {
            "times_served": 5,
            "success_rate": 0.9,
            "retry_rate": 0.1,
            "injection_rate": 0.0,
            "avg_selection_efficiency": None,
        },
    }


class TestPrepareLearningPromotions:
    def test_unapproved_decisions_skipped(self) -> None:
        candidate = _make_promotable_candidate()
        candidates_payload = {"candidates": [candidate]}
        decisions_payload = {
            "decisions": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "approved": False,
                    "promotion_name": "Test",
                    "rationale": "",
                }
            ]
        }
        result = prepare_learning_promotions(
            candidates_payload=candidates_payload,
            decisions_payload=decisions_payload,
        )
        assert result["approved_count"] == 0
        assert result["results"] == []

    def test_approved_decision_produces_ready_result(self) -> None:
        candidate = _make_promotable_candidate()
        candidates_payload = {"candidates": [candidate]}
        decisions_payload = {
            "decisions": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "approved": True,
                    "promotion_name": "My Promotion",
                    "rationale": "Consistently high success.",
                }
            ]
        }
        result = prepare_learning_promotions(
            candidates_payload=candidates_payload,
            decisions_payload=decisions_payload,
        )
        assert result["approved_count"] == 1
        assert len(result["results"]) == 1
        assert result["results"][0]["status"] == "ready"

    def test_missing_candidate_flagged(self) -> None:
        candidates_payload = {"candidates": []}
        decisions_payload = {
            "decisions": [
                {
                    "candidate_id": "nonexistent:000",
                    "approved": True,
                    "promotion_name": "",
                    "rationale": "",
                }
            ]
        }
        result = prepare_learning_promotions(
            candidates_payload=candidates_payload,
            decisions_payload=decisions_payload,
        )
        assert result["results"][0]["status"] == "missing_candidate"

    def test_non_promotable_recommendation_type_skipped(self) -> None:
        candidate = _make_promotable_candidate(recommendation_type="investigate_noise")
        candidates_payload = {"candidates": [candidate]}
        decisions_payload = {
            "decisions": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "approved": True,
                    "promotion_name": "",
                    "rationale": "",
                }
            ]
        }
        result = prepare_learning_promotions(
            candidates_payload=candidates_payload,
            decisions_payload=decisions_payload,
        )
        assert result["results"][0]["status"] == "skipped_non_promotable"

    def test_empty_decisions(self) -> None:
        result = prepare_learning_promotions(
            candidates_payload={"candidates": []},
            decisions_payload={"decisions": []},
        )
        assert result["approved_count"] == 0
        assert result["results"] == []


# ---------------------------------------------------------------------------
# build_learning_promotion_payloads
# ---------------------------------------------------------------------------


class TestBuildLearningPromotionPayloads:
    def test_entity_id_uses_learning_prefix(self) -> None:
        candidate = _make_promotable_candidate(
            candidate_id="asset_generation:deadbeef1234"
        )
        result = build_learning_promotion_payloads(
            candidate=candidate,
            promotion_name="My Promotion",
            rationale="Good item.",
        )
        assert result["entity_id"].startswith("precedent://learning/")

    def test_entity_payload_type(self) -> None:
        candidate = _make_promotable_candidate()
        result = build_learning_promotion_payloads(
            candidate=candidate,
            promotion_name="Test",
            rationale="",
        )
        assert result["entity_payload"]["entity_type"] == "precedent"

    def test_edge_payloads_generated_for_each_target(self) -> None:
        candidate = _make_promotable_candidate()
        candidate["target_entity_ids"] = ["entity://a", "entity://b"]
        result = build_learning_promotion_payloads(
            candidate=candidate,
            promotion_name="Test",
            rationale="",
        )
        assert len(result["edge_payloads"]) == 2
        edge_kinds = {e["edge_kind"] for e in result["edge_payloads"]}
        assert edge_kinds == {"precedent_applies_to"}

    def test_linked_entity_ids_matches_target_entity_ids(self) -> None:
        candidate = _make_promotable_candidate()
        candidate["target_entity_ids"] = ["entity://foo", "entity://bar"]
        result = build_learning_promotion_payloads(
            candidate=candidate,
            promotion_name="",
            rationale="",
        )
        assert sorted(result["linked_entity_ids"]) == ["entity://bar", "entity://foo"]

    def test_promotion_name_used_as_entity_name(self) -> None:
        candidate = _make_promotable_candidate()
        result = build_learning_promotion_payloads(
            candidate=candidate,
            promotion_name="Custom Name",
            rationale="",
        )
        assert result["entity_payload"]["name"] == "Custom Name"

    def test_rationale_stored_in_properties(self) -> None:
        candidate = _make_promotable_candidate()
        result = build_learning_promotion_payloads(
            candidate=candidate,
            promotion_name="Test",
            rationale="This is the rationale.",
        )
        assert (
            result["entity_payload"]["properties"]["approved_rationale"]
            == "This is the rationale."
        )

    def test_empty_target_entity_ids_produces_no_edges(self) -> None:
        candidate = _make_promotable_candidate()
        candidate["target_entity_ids"] = []
        result = build_learning_promotion_payloads(
            candidate=candidate,
            promotion_name="Test",
            rationale="",
        )
        assert result["edge_payloads"] == []
        assert result["linked_entity_ids"] == []


# ---------------------------------------------------------------------------
# write_learning_review_artifacts
# ---------------------------------------------------------------------------


class TestWriteLearningReviewArtifacts:
    def test_creates_both_files(self, tmp_path: Path) -> None:
        report = {
            "artifact_version": "1.0",
            "generated_at_utc": "2024-01-01T00:00:00.000Z",
            "artifacts_root": None,
            "min_support": 2,
            "observation_count": 0,
            "candidate_count": 0,
            "candidates": [],
        }
        paths = write_learning_review_artifacts(report=report, output_dir=tmp_path)
        assert Path(paths["candidates_path"]).exists()
        assert Path(paths["decisions_template_path"]).exists()

    def test_candidates_file_is_valid_json(self, tmp_path: Path) -> None:
        report = {
            "artifact_version": "1.0",
            "generated_at_utc": "2024-01-01T00:00:00.000Z",
            "artifacts_root": None,
            "min_support": 2,
            "observation_count": 0,
            "candidate_count": 0,
            "candidates": [],
        }
        paths = write_learning_review_artifacts(report=report, output_dir=tmp_path)
        data = json.loads(Path(paths["candidates_path"]).read_text())
        assert data["artifact_version"] == "1.0"

    def test_decisions_template_has_one_entry_per_candidate(
        self, tmp_path: Path
    ) -> None:
        candidate = _make_promotable_candidate()
        report = {
            "artifact_version": "1.0",
            "generated_at_utc": "2024-01-01T00:00:00.000Z",
            "artifacts_root": None,
            "min_support": 2,
            "observation_count": 3,
            "candidate_count": 1,
            "candidates": [candidate],
        }
        paths = write_learning_review_artifacts(report=report, output_dir=tmp_path)
        template = json.loads(Path(paths["decisions_template_path"]).read_text())
        assert len(template["decisions"]) == 1
        assert template["decisions"][0]["candidate_id"] == candidate["candidate_id"]
        assert template["decisions"][0]["approved"] is False

    def test_creates_output_dir_if_not_exists(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c"
        report = {
            "artifact_version": "1.0",
            "generated_at_utc": "2024-01-01T00:00:00.000Z",
            "artifacts_root": None,
            "min_support": 2,
            "observation_count": 0,
            "candidate_count": 0,
            "candidates": [],
        }
        paths = write_learning_review_artifacts(report=report, output_dir=nested)
        assert Path(paths["candidates_path"]).exists()


# ---------------------------------------------------------------------------
# Noise evidence screen (the #336 rule, ported into the learning layer)
# ---------------------------------------------------------------------------


class TestNoiseEvidenceScreen:
    """The screen must report a verdict without narrowing the proposal.

    #336's shape: ``noise_candidates`` (what the rule proposed) and
    ``demotion_screen.admitted`` (what survived evidence) are reported
    *separately*, because a proposal that shrinks by 80% at the gate is a
    fact about the proposal rule and collapsing them would hide it.
    """

    def _failing(
        self,
        *,
        run: str,
        cited_unhelpful: bool = False,
        cited_helpful: bool = False,
        attributed: bool = True,
        item_id: str = "item-noise",
    ) -> dict:
        return _make_observation(
            run_id=run,
            outcome="failure",
            citation_attributed=attributed,
            items=[
                _make_item(
                    item_id=item_id,
                    cited_unhelpful=cited_unhelpful,
                    cited_helpful=cited_helpful,
                )
            ],
        )

    def _window(self, *extra: dict) -> list[dict]:
        """Pad to the coverage floor with observations about other items.

        The floor is a property of the *window*, so the padding must not
        touch the candidate under test — otherwise a test meaning to
        exercise the per-item rule would be exercising the floor.
        """
        padding = [
            _make_observation(
                run_id=f"pad-{n}",
                outcome="success",
                citation_attributed=True,
                items=[_make_item(item_id=f"item-pad-{n}", cited_helpful=True)],
            )
            for n in range(MIN_ATTRIBUTED_OBSERVATIONS)
        ]
        return [*padding, *extra]

    def _candidate(self, report: dict, item_id: str) -> dict:
        matches = [c for c in report["candidates"] if c["item_id"] == item_id]
        assert len(matches) == 1, f"{item_id}: {len(matches)} candidates"
        return matches[0]

    def test_repeatedly_cited_unhelpful_is_admitted(
        self, learning_registry: ParameterRegistry
    ) -> None:
        report = analyze_learning_observations(
            observations=self._window(
                self._failing(run="r1", cited_unhelpful=True),
                self._failing(run="r2", cited_unhelpful=True),
            ),
            registry=learning_registry,
        )
        candidate = self._candidate(report, "item-noise")
        assert candidate["recommendation_type"] == "investigate_noise"
        assert candidate["evidence_verdict"] == "admitted"
        assert candidate["citation_evidence"] == {
            "appearances": 2,
            "helpful_count": 0,
            "unhelpful_count": 2,
        }
        screen = report["noise_screen"]
        assert screen["proposed"] == 1
        assert screen["admitted"] == 1
        assert screen["admitted_candidate_ids"] == [candidate["candidate_id"]]
        assert screen["suppressed"] is False

    def test_an_uncited_noise_candidate_is_refused_but_not_dropped(
        self, learning_registry: ParameterRegistry
    ) -> None:
        """#336's rule: absence of helpful citation is not evidence of noise.

        The candidate stays in ``candidates`` — the screen is a verdict
        on the proposal, not a filter over it, so a reviewer can still see
        what the rule proposed and why it was refused.
        """
        report = analyze_learning_observations(
            observations=self._window(
                self._failing(run="r1"),
                self._failing(run="r2"),
            ),
            registry=learning_registry,
        )
        candidate = self._candidate(report, "item-noise")
        assert candidate["recommendation_type"] == "investigate_noise"
        assert candidate["evidence_verdict"] == REFUSED_NO_UNHELPFUL
        screen = report["noise_screen"]
        assert screen["proposed"] == 1
        assert screen["admitted"] == 0
        assert screen["refused_by_reason"] == {REFUSED_NO_UNHELPFUL: 1}

    def test_a_single_unhelpful_citation_is_not_enough(
        self, learning_registry: ParameterRegistry
    ) -> None:
        report = analyze_learning_observations(
            observations=self._window(
                self._failing(run="r1", cited_unhelpful=True),
                self._failing(run="r2"),
            ),
            registry=learning_registry,
        )
        assert self._candidate(report, "item-noise")["evidence_verdict"] == (
            "insufficient_unhelpful_citations"
        )

    def test_a_contested_candidate_is_refused(
        self, learning_registry: ParameterRegistry
    ) -> None:
        """Never exercised by the live corpus — covered only here.

        Measured on the reference deployment: of 265 proposals, **zero**
        reached the contested branch (every candidate with two unhelpful
        citations had strictly more unhelpful than helpful). A mutant
        deleting the branch survives real data entirely.
        """
        report = analyze_learning_observations(
            observations=self._window(
                self._failing(run="r1", cited_unhelpful=True),
                self._failing(run="r2", cited_unhelpful=True),
                self._failing(run="r3", cited_helpful=True),
                self._failing(run="r4", cited_helpful=True),
            ),
            registry=learning_registry,
        )
        candidate = self._candidate(report, "item-noise")
        assert candidate["citation_evidence"]["helpful_count"] == 2
        assert candidate["citation_evidence"]["unhelpful_count"] == 2
        assert candidate["evidence_verdict"] == REFUSED_CONTESTED
        assert report["noise_screen"]["admitted"] == 0

    def test_promote_candidates_are_never_screened(
        self, learning_registry: ParameterRegistry
    ) -> None:
        """The promote arm was built, measured and refused.

        Blocking promotion on absence of a helpful citation would repeat
        #336's error mirrored — with ``P(cited helpful | served) ≈ 0.10``,
        "never cited helpful" is the *expected* state of a good item. So a
        promote candidate carries ``not_screened``, not a verdict.
        """
        report = analyze_learning_observations(
            observations=self._window(
                _make_observation(
                    run_id="p1",
                    outcome="success",
                    citation_attributed=True,
                    items=[_make_item(item_id="item-good")],
                ),
                _make_observation(
                    run_id="p2",
                    outcome="success",
                    citation_attributed=True,
                    items=[_make_item(item_id="item-good")],
                ),
            ),
            registry=learning_registry,
        )
        candidate = self._candidate(report, "item-good")
        assert candidate["recommendation_type"] == "promote_guidance"
        assert candidate["evidence_verdict"] == NOT_SCREENED
        screen = report["noise_screen"]
        assert candidate["candidate_id"] not in screen["admitted_candidate_ids"]
        assert screen["proposed"] == 0

    def test_a_thin_window_suppresses_every_admission(
        self, learning_registry: ParameterRegistry
    ) -> None:
        """The coverage floor guards against a grading surface going dark.

        Strong per-item evidence is not enough on its own: if almost
        nothing in the window carries a verdict, the few that do are not a
        sample worth acting on (#309).
        """
        report = analyze_learning_observations(
            observations=[
                self._failing(run="r1", cited_unhelpful=True),
                self._failing(run="r2", cited_unhelpful=True),
                self._failing(run="r3", cited_unhelpful=True),
            ],
            registry=learning_registry,
        )
        candidate = self._candidate(report, "item-noise")
        assert candidate["citation_evidence"]["unhelpful_count"] == 3
        assert candidate["evidence_verdict"] == REFUSED_THIN_CORPUS
        screen = report["noise_screen"]
        assert screen["suppressed"] is True
        assert screen["suppressed_reason"] == REFUSED_THIN_CORPUS
        assert screen["admitted"] == 0
        assert screen["attributed_observations"] == 3
        assert screen["min_attributed_observations"] == MIN_ATTRIBUTED_OBSERVATIONS

    def test_unattributed_observations_do_not_count_toward_coverage(
        self, learning_registry: ParameterRegistry
    ) -> None:
        """Serving a pack is not grading it.

        An observation with no per-item verdict at all contributes to the
        success rate but cannot make the window look graded, which is the
        whole point of a coverage floor.
        """
        report = analyze_learning_observations(
            observations=[
                *[
                    _make_observation(
                        run_id=f"quiet-{n}",
                        outcome="success",
                        items=[_make_item(item_id=f"item-pad-{n}")],
                    )
                    for n in range(MIN_ATTRIBUTED_OBSERVATIONS + 3)
                ],
                self._failing(run="r1", cited_unhelpful=True),
                self._failing(run="r2", cited_unhelpful=True),
            ],
            registry=learning_registry,
        )
        assert report["attributed_observation_count"] == 2
        assert report["noise_screen"]["suppressed"] is True

    def test_citation_counts_reach_the_promoted_node(
        self, learning_registry: ParameterRegistry
    ) -> None:
        """``precedent_properties`` lands verbatim on the durable node.

        A reviewer approving a promotion months later has the grader
        evidence on the node itself, not only in a report that rolled out
        of the window.
        """
        report = analyze_learning_observations(
            observations=self._window(
                _make_observation(
                    run_id="p1",
                    outcome="success",
                    citation_attributed=True,
                    items=[_make_item(item_id="item-good", cited_helpful=True)],
                ),
                _make_observation(
                    run_id="p2",
                    outcome="success",
                    citation_attributed=True,
                    items=[_make_item(item_id="item-good", cited_helpful=True)],
                ),
            ),
            registry=learning_registry,
        )
        candidate = self._candidate(report, "item-good")
        assert candidate["precedent_properties"]["helpful_citations"] == 2
        assert candidate["precedent_properties"]["unhelpful_citations"] == 0
        payloads = build_learning_promotion_payloads(
            candidate=candidate,
            promotion_name="Good guidance",
            rationale="Cited by graders.",
        )
        properties = payloads["entity_payload"]["properties"]
        assert properties["helpful_citations"] == 2
        assert "Graders cited it helpful 2 time(s)" in properties["description"]

    def test_description_says_so_when_no_grader_cited_the_item(
        self, learning_registry: ParameterRegistry
    ) -> None:
        report = analyze_learning_observations(
            observations=self._window(
                _make_observation(
                    run_id="p1",
                    outcome="success",
                    citation_attributed=True,
                    items=[_make_item(item_id="item-good")],
                ),
                _make_observation(
                    run_id="p2",
                    outcome="success",
                    citation_attributed=True,
                    items=[_make_item(item_id="item-good")],
                ),
            ),
            registry=learning_registry,
        )
        payloads = build_learning_promotion_payloads(
            candidate=self._candidate(report, "item-good"),
            promotion_name="Uncited guidance",
            rationale="High success rate.",
        )
        description = payloads["entity_payload"]["properties"]["description"]
        assert "No grader cited this item either way." in description

    def test_citations_are_counted_per_serving_not_per_item(
        self, learning_registry: ParameterRegistry
    ) -> None:
        """Two unhelpful citations must mean two *packs*, not one pack twice.

        ``unhelpful >= 2`` is a claim about repeated judgement. Counting
        per distinct item would let a single bad pack satisfy it.
        """
        report = analyze_learning_observations(
            observations=self._window(
                _make_observation(
                    run_id="r1",
                    outcome="failure",
                    citation_attributed=True,
                    items=[
                        _make_item(item_id="item-noise", cited_unhelpful=True),
                        _make_item(item_id="item-other", item_type="precedent"),
                    ],
                ),
                self._failing(run="r2"),
            ),
            registry=learning_registry,
        )
        candidate = self._candidate(report, "item-noise")
        assert candidate["citation_evidence"] == {
            "appearances": 2,
            "helpful_count": 0,
            "unhelpful_count": 1,
        }
        assert candidate["evidence_verdict"] == "insufficient_unhelpful_citations"
