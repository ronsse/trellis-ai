"""Tests for curate CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from tests.cli_output import assert_coloured, force_colour
from trellis.learning import submit_learning_promotion
from trellis.mutate.commands import (
    Command,
    CommandResult,
    CommandStatus,
    Operation,
)
from trellis.mutate.executor import MutationExecutor
from trellis_cli import curate as curate_cli
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


class TestCurateRedact:
    def _create_node(self) -> str:
        r = runner.invoke(
            app,
            ["curate", "entity", "person", "Junk Mint", "--format", "json"],
        )
        node_id: str = json.loads(r.stdout.strip())["node_id"]
        return node_id

    def test_redact_purges_entity(self) -> None:
        node_id = self._create_node()
        result = runner.invoke(
            app,
            [
                "curate",
                "redact",
                node_id,
                "--yes",
                "--reason",
                "defect-minted entity",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "success"
        assert data["target_id"] == node_id
        assert data["command_id"]

    def test_redact_without_yes_aborts_on_decline(self) -> None:
        node_id = self._create_node()
        declined = runner.invoke(
            app,
            ["curate", "redact", node_id, "--reason", "r"],
            input="n\n",
        )
        assert declined.exit_code != 0
        # Declining must leave the entity intact — a subsequent confirmed
        # redaction still finds it.
        confirmed = runner.invoke(
            app,
            ["curate", "redact", node_id, "--yes", "--reason", "r", "--format", "json"],
        )
        assert confirmed.exit_code == 0

    def test_redact_missing_target_exits_store_code(self) -> None:
        # NotFoundError is a StoreError -> FAILED -> EXIT_STORE (5), per
        # the exit_codes map; the JSON keeps command_id so the failed
        # attempt joins its MUTATION_REJECTED/EXECUTED audit event.
        result = runner.invoke(
            app,
            ["curate", "redact", "ghost", "--yes", "--reason", "r", "--format", "json"],
        )
        assert result.exit_code == 5
        data = json.loads(result.stdout.strip())
        assert data["status"] == "failed"
        assert data["command_id"]

    def test_redact_blank_reason_exits_validation_code(self) -> None:
        node_id = self._create_node()
        result = runner.invoke(
            app,
            [
                "curate",
                "redact",
                node_id,
                "--yes",
                "--reason",
                "  ",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 2
        data = json.loads(result.stdout.strip())
        assert data["status"] == "rejected"
        assert data["command_id"]

    def test_redact_markup_in_target_id_is_escaped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raw target id containing rich markup must not crash the error
        print (``MarkupError``) or be silently swallowed.

        Colour is forced (#495): the same ``console.print`` that could eat
        ``[/x]`` also wraps the message in SGR runs, so reading raw
        ``stdout`` made this test fail on a coloured build for a reason
        unrelated to markup. Stripping alone would fix that and leave the
        test passing on a build where Rich never coloured — hence the
        explicit "colour happened" assertion first.
        """
        force_colour(monkeypatch, curate_cli)
        result = runner.invoke(
            app,
            ["curate", "redact", "ghost[/x]", "--yes", "--reason", "r"],
        )
        assert result.exit_code == 5
        # 1. Colour really happened.
        rendered = assert_coloured(result.stdout)
        # 2. The markup token survived rendering byte-for-byte.
        assert "ghost[/x]" in rendered, (
            "Rich read the target id as markup and ate it, so the operator "
            "cannot see which id failed"
        )
        # 3. And the error was printed rather than lost to a MarkupError
        #    somewhere upstream of it.
        assert "Execution failed" in rendered


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

    def test_feedback_with_pack_id(self) -> None:
        """The CLI can name a pack — without one the event cannot join."""
        result = runner.invoke(
            app,
            [
                "curate",
                "feedback",
                "trace_1",
                "0.8",
                "--pack-id",
                "pack_1",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
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
        assert data["ready_count"] == 1
        assert data["promoted_count"] == 1
        assert len(data["results"]) == 1
        result_entry = data["results"][0]
        assert result_entry["status"] == "promoted"
        assert result_entry["node_id"]
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
        assert data["ready_count"] == 0
        assert data["promoted_count"] == 0
        assert data["results"] == []


class TestSubmitPromotion:
    """Direct unit tests for the shared submit_learning_promotion helper.

    These exercise branches that are awkward to reach through the CLI
    runner (entity create rejected, edge create after a successful
    entity) without standing up the full mutation pipeline.
    """

    @staticmethod
    def _entity_payload() -> dict:
        return {
            "entity_type": "precedent",
            "entity_id": "precedent://learning/test",
            "name": "Test precedent",
            "properties": {"description": "test"},
        }

    @staticmethod
    def _edge_payload(target_id: str = "doc:target-1") -> dict:
        return {
            "source_id": "precedent://learning/test",
            "target_id": target_id,
            "edge_kind": "precedent_applies_to",
            "properties": {"source_of_truth": "reviewed_promotion"},
        }

    def test_entity_failure_short_circuits_edges(self) -> None:
        executor = MagicMock(spec=MutationExecutor)
        executor.execute.return_value = CommandResult(
            command_id="cmd-1",
            status=CommandStatus.REJECTED,
            operation=Operation.ENTITY_CREATE,
            message="entity_type 'precedent' not registered",
        )

        outcome = submit_learning_promotion(
            executor,
            self._entity_payload(),
            [self._edge_payload("doc:t1"), self._edge_payload("doc:t2")],
            requested_by="cli:promote-learning",
        )

        assert outcome["status"] == "entity_failed"
        assert outcome["entity_status"] == CommandStatus.REJECTED.value
        assert outcome["message"] == "entity_type 'precedent' not registered"
        # Single execute() call — edges must NOT have been attempted.
        assert executor.execute.call_count == 1
        submitted_op = executor.execute.call_args.args[0].operation
        assert submitted_op == Operation.ENTITY_CREATE

    def test_entity_success_then_edges_submitted(self) -> None:
        executor = MagicMock(spec=MutationExecutor)
        executor.execute.side_effect = [
            CommandResult(
                command_id="cmd-1",
                status=CommandStatus.SUCCESS,
                operation=Operation.ENTITY_CREATE,
                created_id="precedent://learning/test",
            ),
            CommandResult(
                command_id="cmd-2",
                status=CommandStatus.SUCCESS,
                operation=Operation.LINK_CREATE,
            ),
            CommandResult(
                command_id="cmd-3",
                status=CommandStatus.FAILED,
                operation=Operation.LINK_CREATE,
                message="target not found",
            ),
            CommandResult(
                command_id="cmd-4",
                status=CommandStatus.SUCCESS,
                operation=Operation.PRECEDENT_PROMOTE,
                created_id="evt-1",
            ),
        ]

        outcome = submit_learning_promotion(
            executor,
            self._entity_payload(),
            [self._edge_payload("doc:t1"), self._edge_payload("doc:t2")],
            requested_by="cli:promote-learning",
        )

        assert outcome["status"] == "promoted"
        assert outcome["node_id"] == "precedent://learning/test"
        assert [e["target_id"] for e in outcome["edges"]] == ["doc:t1", "doc:t2"]
        assert outcome["edges"][0]["status"] == CommandStatus.SUCCESS.value
        assert outcome["edges"][1]["status"] == CommandStatus.FAILED.value
        # A PRECEDENT_PROMOTE is submitted after the edges so the precedent
        # reaches get_lessons; its status is surfaced on the outcome.
        assert outcome["precedent_event_status"] == CommandStatus.SUCCESS.value
        assert outcome["precedent_event_id"] == "evt-1"
        assert executor.execute.call_count == 4
        promote_cmd = executor.execute.call_args_list[3].args[0]
        assert promote_cmd.operation == Operation.PRECEDENT_PROMOTE
        assert promote_cmd.args["entity_type"] == "precedent"
        assert promote_cmd.target_id == "precedent://learning/test"

    def test_passes_payload_fields_into_command_args(self) -> None:
        executor = MagicMock(spec=MutationExecutor)
        executor.execute.return_value = CommandResult(
            command_id="cmd-1",
            status=CommandStatus.SUCCESS,
            operation=Operation.ENTITY_CREATE,
            created_id="precedent://learning/test",
        )

        submit_learning_promotion(
            executor,
            self._entity_payload(),
            [],
            requested_by="cli:promote-learning",
        )

        # First submitted command is the ENTITY_CREATE (a trailing
        # PRECEDENT_PROMOTE now follows, so index explicitly rather than
        # reading the last call).
        submitted: Command = executor.execute.call_args_list[0].args[0]
        assert submitted.operation == Operation.ENTITY_CREATE
        assert submitted.requested_by == "cli:promote-learning"
        assert submitted.args["entity_type"] == "precedent"
        assert submitted.args["entity_id"] == "precedent://learning/test"
        # properties are copied, not aliased — caller mutation can't leak in.
        properties = submitted.args["properties"]
        assert properties == {"description": "test"}
        assert properties is not self._entity_payload()["properties"]


class TestPromotionVisibleToGetLessons:
    """End-to-end guarantee (P9): a promoted learning candidate must reach
    the lessons read-path. Before the PRECEDENT_PROMOTE emit was added,
    ``submit_learning_promotion`` created the ``precedent://learning/...``
    entity but ``list_precedents`` (which get_lessons calls) queries only
    ``PRECEDENT_PROMOTED`` events, so the precedent stayed invisible.
    """

    def test_promoted_learning_precedent_listed_by_get_lessons(
        self, tmp_path: Path
    ) -> None:
        from trellis.learning import build_learning_promotion_payloads
        from trellis.mutate import build_curate_executor
        from trellis.retrieve.precedents import list_precedents
        from trellis.stores.registry import StoreRegistry

        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        registry = StoreRegistry(stores_dir=stores_dir)
        executor = build_curate_executor(registry)

        candidate = {
            "candidate_id": "source_analysis:abc123",
            "intent_family": "source_analysis",
            "recommendation_type": "promote_precedent",
            "item_id": "doc:evidence-1",
            "item_type": "precedent",
            "title": "Profile-before-plan wins",
            "domain_systems": ["billing"],
            "target_entity_ids": [],
            "supporting_run_ids": ["run-1", "run-2"],
            "phases": ["analyze"],
            "metrics": {"success_rate": 1.0, "retry_rate": 0.0},
            "precedent_properties": {
                "category": "retrieval_precedent",
                "intent_family": "source_analysis",
                "source_item_id": "doc:evidence-1",
                "source_of_truth": "reviewed_promotion",
            },
        }
        payloads = build_learning_promotion_payloads(
            candidate=candidate,
            promotion_name="Profile-before-plan wins",
            rationale="Consistently precedes clean plans.",
        )

        outcome = submit_learning_promotion(
            executor,
            payloads["entity_payload"],
            payloads["edge_payloads"],
            requested_by="test:promote-learning",
        )
        assert outcome["status"] == "promoted"
        assert outcome["precedent_event_status"] == CommandStatus.SUCCESS.value

        # The load-bearing assertion: get_lessons' read-path now sees it.
        lessons = list_precedents(registry.operational.event_log)
        assert len(lessons) == 1
        assert lessons[0]["title"] == "Profile-before-plan wins"
        assert lessons[0]["entity_id"] == payloads["entity_id"]

        # And the domain filter get_lessons(domain=...) uses resolves too.
        assert (
            len(list_precedents(registry.operational.event_log, domain="billing")) == 1
        )
        assert (
            len(list_precedents(registry.operational.event_log, domain="shipping")) == 0
        )


class TestCurateHelp:
    def test_help(self) -> None:
        result = runner.invoke(app, ["curate", "--help"])
        assert result.exit_code == 0
        for cmd in ["promote", "link", "label", "feedback", "promote-learning"]:
            assert cmd in result.stdout
