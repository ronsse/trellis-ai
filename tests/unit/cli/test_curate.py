"""Tests for curate CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


class TestCuratePromote:
    def test_promote(self) -> None:
        result = runner.invoke(
            app,
            [
                "curate",
                "promote",
                "trace_123",
                "--title",
                "Always check locks",
                "--description",
                "Learned from incident",
            ],
        )
        assert result.exit_code == 0
        # No handler registered, so command fails with "No handler registered"
        assert "Command" in result.stdout

    def test_promote_json(self) -> None:
        result = runner.invoke(
            app,
            [
                "curate",
                "promote",
                "trace_123",
                "--title",
                "T",
                "--description",
                "D",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["operation"] == "precedent.promote"


class TestCurateLink:
    def _create_nodes(self) -> tuple[str, str]:
        """Create two entities and return their IDs."""
        r1 = runner.invoke(
            app,
            [
                "curate",
                "entity",
                "concept",
                "Source",
                "--format",
                "json",
            ],
        )
        r2 = runner.invoke(
            app,
            [
                "curate",
                "entity",
                "concept",
                "Target",
                "--format",
                "json",
            ],
        )
        id1 = json.loads(r1.stdout.strip())["node_id"]
        id2 = json.loads(r2.stdout.strip())["node_id"]
        return id1, id2

    def test_link(self) -> None:
        id1, id2 = self._create_nodes()
        result = runner.invoke(app, ["curate", "link", id1, id2])
        assert result.exit_code == 0
        assert "Link created" in result.stdout

    def test_link_with_kind(self) -> None:
        id1, id2 = self._create_nodes()
        result = runner.invoke(
            app,
            [
                "curate",
                "link",
                id1,
                id2,
                "--kind",
                "entity_depends_on",
                "--format",
                "json",
            ],
        )
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        assert data["edge_kind"] == "entity_depends_on"

    def test_link_missing_source(self) -> None:
        result = runner.invoke(app, ["curate", "link", "nonexistent", "also_nope"])
        assert result.exit_code == 1


class TestCurateLabel:
    def test_label(self) -> None:
        result = runner.invoke(app, ["curate", "label", "ent_1", "important"])
        assert result.exit_code == 0

    def test_label_json(self) -> None:
        result = runner.invoke(
            app,
            [
                "curate",
                "label",
                "ent_1",
                "critical",
                "--format",
                "json",
            ],
        )
        data = json.loads(result.stdout.strip())
        assert data["operation"] == "label.add"


class TestCurateFeedback:
    def test_feedback(self) -> None:
        result = runner.invoke(app, ["curate", "feedback", "trace_1", "0.9"])
        assert result.exit_code == 0

    def test_feedback_with_comment(self) -> None:
        result = runner.invoke(
            app,
            [
                "curate",
                "feedback",
                "trace_1",
                "0.8",
                "--comment",
                "Good approach",
                "--format",
                "json",
            ],
        )
        data = json.loads(result.stdout.strip())
        assert data["operation"] == "feedback.record"


def _candidate_payload(
    *,
    candidate_id: str = "general_context:abc123",
    item_id: str = "lc:doc:helpful",
    target_entity_ids: list[str] | None = None,
) -> dict:
    """Minimal candidates JSON shape that ``prepare_learning_promotions`` accepts."""
    return {
        "artifact_version": "1.0",
        "candidate_count": 1,
        "candidates": [
            {
                "candidate_id": candidate_id,
                "intent_family": "general_context",
                "recommendation_type": "promote_guidance",
                "item_id": item_id,
                "item_type": "document",
                "title": "Test helpful doc",
                "category": None,
                "domain_systems": [],
                "phases": [],
                "target_entity_ids": target_entity_ids or [],
                "supporting_run_ids": ["test-run-1"],
                "source_strategies": {"document": 1},
                "metrics": {
                    "times_served": 3,
                    "success_rate": 1.0,
                    "retry_rate": 0.0,
                    "injection_rate": 0.0,
                    "avg_selection_efficiency": None,
                },
                "evidence_refs": [],
                "precedent_name": "Learning: general_context :: Test",
                "precedent_properties": {
                    "category": "retrieval_guidance",
                    "intent_family": "general_context",
                    "source_item_id": item_id,
                    "source_item_type": "document",
                    "success_rate": 1.0,
                    "retry_rate": 0.0,
                    "support_count": 3,
                    "source_of_truth": "reviewed_promotion",
                },
            }
        ],
    }


def _decisions_payload(
    *,
    candidate_id: str = "general_context:abc123",
    approved: bool = True,
) -> dict:
    return {
        "artifact_version": "1.0",
        "generated_from": "test",
        "decisions": [
            {
                "candidate_id": candidate_id,
                "approved": approved,
                "promotion_name": "Test promoted precedent",
                "rationale": "unit test",
            }
        ],
    }


def _write_review_pair(
    tmp_path: Path,
    *,
    candidate_id: str = "general_context:abc123",
    approved: bool = True,
    target_entity_ids: list[str] | None = None,
) -> tuple[Path, Path]:
    candidates_path = tmp_path / "candidates.json"
    decisions_path = tmp_path / "decisions.json"
    candidates_path.write_text(
        json.dumps(
            _candidate_payload(
                candidate_id=candidate_id, target_entity_ids=target_entity_ids
            )
        ),
        encoding="utf-8",
    )
    decisions_path.write_text(
        json.dumps(_decisions_payload(candidate_id=candidate_id, approved=approved)),
        encoding="utf-8",
    )
    return candidates_path, decisions_path


class TestCuratePromoteLearning:
    def test_dry_run_describes_plan_without_mutating(self, tmp_path: Path) -> None:
        candidates_path, decisions_path = _write_review_pair(tmp_path)
        result = runner.invoke(
            app,
            [
                "curate",
                "promote-learning",
                "--candidates",
                str(candidates_path),
                "--decisions",
                str(decisions_path),
                "--dry-run",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["dry_run"] is True
        assert data["approved_count"] == 1
        assert data["ready_count"] == 1
        # Dry run reports the prepared payloads but never reaches the
        # mutation pipeline; "promoted_count" is intentionally absent.
        assert "promoted_count" not in data

    def test_promotes_approved_candidate(self, tmp_path: Path) -> None:
        candidates_path, decisions_path = _write_review_pair(tmp_path)
        result = runner.invoke(
            app,
            [
                "curate",
                "promote-learning",
                "--candidates",
                str(candidates_path),
                "--decisions",
                str(decisions_path),
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["dry_run"] is False
        assert data["approved_count"] == 1
        assert data["promoted_count"] == 1
        assert len(data["results"]) == 1
        result_entry = data["results"][0]
        assert result_entry["status"] == "promoted"
        assert result_entry["node_id"]
        # Document-only loops produce empty edge lists; assert the
        # field exists even when empty so callers can rely on shape.
        assert result_entry["edges"] == []

    def test_no_approvals_is_a_no_op(self, tmp_path: Path) -> None:
        candidates_path, decisions_path = _write_review_pair(tmp_path, approved=False)
        result = runner.invoke(
            app,
            [
                "curate",
                "promote-learning",
                "--candidates",
                str(candidates_path),
                "--decisions",
                str(decisions_path),
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["approved_count"] == 0
        assert data["promoted_count"] == 0
        assert data["results"] == []


class TestCurateHelp:
    def test_help(self) -> None:
        result = runner.invoke(app, ["curate", "--help"])
        assert result.exit_code == 0
        for cmd in ["promote", "link", "label", "feedback", "promote-learning"]:
            assert cmd in result.stdout
