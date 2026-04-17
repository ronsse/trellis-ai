"""Unit tests for trellis.learning.scoring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trellis.learning.scoring import (
    analyze_learning_observations,
    build_learning_promotion_payloads,
    normalize_intent_family,
    prepare_learning_promotions,
    write_learning_review_artifacts,
)

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
    return obs


def _make_item(
    item_id: str = "item-abc",
    item_type: str = "guidance",
    title: str = "Some Guidance",
    source_strategy: str = "keyword",
) -> dict:
    return {
        "item_id": item_id,
        "item_type": item_type,
        "title": title,
        "source_strategy": source_strategy,
    }


class TestAnalyzeLearningObservations:
    def test_empty_observations(self) -> None:
        result = analyze_learning_observations(observations=[])
        assert result["observation_count"] == 0
        assert result["candidate_count"] == 0
        assert result["candidates"] == []

    def test_min_support_filters_single_observation(self) -> None:
        obs = _make_observation(items=[_make_item()])
        result = analyze_learning_observations(observations=[obs], min_support=2)
        assert result["candidate_count"] == 0

    def test_min_support_passes_with_enough_observations(self) -> None:
        item = _make_item()
        obs1 = _make_observation(run_id="run-1", items=[item])
        obs2 = _make_observation(run_id="run-2", items=[item])
        result = analyze_learning_observations(observations=[obs1, obs2], min_support=2)
        assert result["candidate_count"] == 1

    def test_success_rate_computed_correctly(self) -> None:
        # 4 successes out of 4 => success_rate=1.0, which crosses the 0.75 promote
        # threshold so a candidate is produced for inspection.
        item = _make_item(item_id="x")
        obs1 = _make_observation(run_id="r1", outcome="success", items=[item])
        obs2 = _make_observation(run_id="r2", outcome="success", items=[item])
        obs3 = _make_observation(run_id="r3", outcome="success", items=[item])
        obs4 = _make_observation(run_id="r4", outcome="success", items=[item])
        result = analyze_learning_observations(
            observations=[obs1, obs2, obs3, obs4], min_support=1
        )
        candidate = result["candidates"][0]
        assert candidate["metrics"]["success_rate"] == pytest.approx(1.0, rel=1e-3)
        assert candidate["metrics"]["times_served"] == 4

    def test_promote_guidance_recommendation_for_high_success_rate(self) -> None:
        item = _make_item(item_type="guidance")
        obs1 = _make_observation(run_id="r1", outcome="success", items=[item])
        obs2 = _make_observation(run_id="r2", outcome="success", items=[item])
        obs3 = _make_observation(run_id="r3", outcome="success", items=[item])
        result = analyze_learning_observations(
            observations=[obs1, obs2, obs3], min_support=1
        )
        assert result["candidates"][0]["recommendation_type"] == "promote_guidance"

    def test_promote_precedent_recommendation_for_precedent_type(self) -> None:
        item = _make_item(item_type="precedent")
        obs1 = _make_observation(run_id="r1", outcome="success", items=[item])
        obs2 = _make_observation(run_id="r2", outcome="success", items=[item])
        obs3 = _make_observation(run_id="r3", outcome="success", items=[item])
        result = analyze_learning_observations(
            observations=[obs1, obs2, obs3], min_support=1
        )
        assert result["candidates"][0]["recommendation_type"] == "promote_precedent"

    def test_investigate_noise_for_low_success_rate(self) -> None:
        item = _make_item()
        obs1 = _make_observation(run_id="r1", outcome="failure", items=[item])
        obs2 = _make_observation(run_id="r2", outcome="failure", items=[item])
        obs3 = _make_observation(run_id="r3", outcome="failure", items=[item])
        result = analyze_learning_observations(
            observations=[obs1, obs2, obs3], min_support=1
        )
        assert result["candidates"][0]["recommendation_type"] == "investigate_noise"

    def test_mid_range_success_rate_excluded(self) -> None:
        # success_rate ~0.6, retry_rate 0 — no action taken, candidate excluded
        item = _make_item()
        obss = [
            _make_observation(
                run_id=f"r{i}", outcome="success" if i < 3 else "failure", items=[item]
            )
            for i in range(5)
        ]
        result = analyze_learning_observations(observations=obss, min_support=1)
        # 3/5 = 0.6 success, retry_rate=0 => no action (between thresholds)
        assert result["candidate_count"] == 0

    def test_artifacts_root_stored(self) -> None:
        result = analyze_learning_observations(
            observations=[], artifacts_root="/var/data/artifacts"
        )
        assert result["artifacts_root"] == "/var/data/artifacts"

    def test_artifacts_root_none_by_default(self) -> None:
        result = analyze_learning_observations(observations=[])
        assert result["artifacts_root"] is None

    def test_selection_efficiency_averaged(self) -> None:
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
            observations=[obs1, obs2, obs3], min_support=1
        )
        assert result["candidates"][0]["metrics"][
            "avg_selection_efficiency"
        ] == pytest.approx(0.6, rel=1e-3)

    def test_supporting_run_ids_aggregated(self) -> None:
        item = _make_item()
        obs1 = _make_observation(run_id="run-alpha", outcome="success", items=[item])
        obs2 = _make_observation(run_id="run-beta", outcome="success", items=[item])
        obs3 = _make_observation(run_id="run-gamma", outcome="success", items=[item])
        result = analyze_learning_observations(
            observations=[obs1, obs2, obs3], min_support=1
        )
        run_ids = result["candidates"][0]["supporting_run_ids"]
        assert sorted(run_ids) == ["run-alpha", "run-beta", "run-gamma"]


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
