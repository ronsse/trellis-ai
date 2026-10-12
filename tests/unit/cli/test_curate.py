"""Tests for curate CLI commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from tests.cli_output import assert_coloured, force_colour, plain
from trellis.learning import submit_learning_promotion
from trellis.learning.candidate_auto_promote import (
    CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_SETTING,
    CANDIDATE_AUTO_PROMOTE_MODE_SETTING,
)
from trellis.mutate.commands import (
    Command,
    CommandResult,
    CommandStatus,
    Operation,
)
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import AUTO_DEMOTE_REQUESTED_BY
from trellis.stores.base.event_log import EventType
from trellis.stores.base.graph import GraphStore
from trellis.stores.settings_store import SettingsStore
from trellis_cli import curate as curate_cli
from trellis_cli.exit_codes import EXIT_VALIDATION
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
        # The handler emits PRECEDENT_PROMOTED without looking the trace up,
        # so a made-up id succeeds.
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

    def test_promote_message_echoes_the_title_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``result.message`` embeds the caller's title, so it is caller text."""
        force_colour(monkeypatch, curate_cli)
        result = runner.invoke(
            app,
            [
                "curate",
                "promote",
                "trace_123",
                "--title",
                "[bold]t[/x]",
                "--description",
                "d",
            ],
        )
        assert result.exit_code == 0, plain(result.output)
        rendered = " ".join(assert_coloured(result.stdout).split())
        assert "Message: Precedent promoted: [bold]t[/x]" in rendered

    def test_message_long_text_prints_as_one_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_execute_command``'s ``Message:`` line (``markup=False``) echoes
        ``result.message`` verbatim before raising on a refusal, in the
        same function as that raise; a long one must not split at the
        console width."""
        monkeypatch.setenv("COLUMNS", "60")
        long_message = "synthetic-rejection-message-" + "0123456789abcdef" * 4
        executor = MagicMock(spec=MutationExecutor)
        executor.execute.return_value = CommandResult(
            command_id="cmd-msg-1",
            status=CommandStatus.FAILED,
            operation=Operation.PRECEDENT_PROMOTE,
            message=long_message,
        )
        monkeypatch.setattr(
            curate_cli, "build_curate_executor", lambda *_a, **_k: executor
        )
        result = runner.invoke(
            app,
            [
                "curate",
                "promote",
                "trace_123",
                "--title",
                "t",
                "--description",
                "d",
            ],
        )
        assert result.exit_code != 0, result.output
        lines = plain(result.stdout).splitlines()
        assert any(long_message in line for line in lines), result.stdout

    def test_warning_long_detail_prints_as_one_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_print_warnings`` prints a policy id and condition, or an audit
        error, of any length; its callers (here ``_execute_command``) exit
        non-zero after it on a refusal, and it must not split one at the
        console width."""
        monkeypatch.setenv("COLUMNS", "60")
        long_detail = "synthetic-policy-detail-" + "0123456789abcdef" * 4
        executor = MagicMock(spec=MutationExecutor)
        executor.execute.return_value = CommandResult(
            command_id="cmd-warn-1",
            status=CommandStatus.REJECTED,
            operation=Operation.PRECEDENT_PROMOTE,
            message="rejected",
            warnings=[long_detail],
        )
        monkeypatch.setattr(
            curate_cli, "build_curate_executor", lambda *_a, **_k: executor
        )
        result = runner.invoke(
            app,
            [
                "curate",
                "promote",
                "trace_123",
                "--title",
                "t",
                "--description",
                "d",
            ],
        )
        assert result.exit_code != 0, result.output
        lines = plain(result.stdout).splitlines()
        assert any(long_detail in line for line in lines), result.stdout


class TestCurateEntity:
    def test_text_echoes_type_name_and_properties_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The echo runs after the node is written, so markup cost the exit.

        ``[/x]`` in any of the three raised ``MarkupError`` with the entity
        already created -- exit 1 on text, 0 on ``--format json``.
        """
        force_colour(monkeypatch, curate_cli)
        result = runner.invoke(
            app,
            [
                "curate",
                "entity",
                "[bold]t[/x]",
                "[bold]n[/y]",
                "--properties",
                '{"k": "[bold]v[/z]"}',
            ],
        )
        assert result.exit_code == 0, plain(result.output)
        rendered = " ".join(assert_coloured(result.stdout).split())
        assert "Type: [bold]t[/x]" in rendered
        assert "Name: [bold]n[/y]" in rendered
        assert "Properties: {'k': '[bold]v[/z]'}" in rendered


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
        assert result.exit_code == 2

    def test_link_missing_source_json(self) -> None:
        """A dangling endpoint is input to fix, reported like any refusal."""
        result = runner.invoke(
            app,
            ["curate", "link", "nonexistent", "also_nope", "--format", "json"],
        )
        assert result.exit_code == 2
        data = json.loads(result.stdout.strip())
        assert data["status"] == "rejected"
        assert data["command_id"]


class TestCurateLabel:
    def _create_node(self) -> str:
        created = runner.invoke(
            app, ["curate", "entity", "person", "Plain", "--format", "json"]
        )
        node_id: str = json.loads(created.stdout)["node_id"]
        return node_id

    def test_label(self) -> None:
        node_id = self._create_node()
        result = runner.invoke(app, ["curate", "label", node_id, "important"])
        assert result.exit_code == 0

    def test_label_json(self) -> None:
        node_id = self._create_node()
        result = runner.invoke(
            app,
            [
                "curate",
                "label",
                node_id,
                "critical",
                "--format",
                "json",
            ],
        )
        data = json.loads(result.stdout.strip())
        assert data["operation"] == "label.add"
        assert data["status"] == "success"
        assert data["created_id"] == node_id

    def test_label_missing_node_json(self) -> None:
        """A node id that names no node is input to fix, and nothing is written."""
        result = runner.invoke(
            app, ["curate", "label", "ghost", "critical", "--format", "json"]
        )
        assert result.exit_code == 2, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "rejected"
        assert data["message"] == "Node not found: ghost"
        assert data["created_id"] is None

    def test_label_missing_node_text(self) -> None:
        result = runner.invoke(app, ["curate", "label", "ghost", "critical"])
        assert result.exit_code == 2, result.output
        output = plain(result.output)
        assert "Command rejected" in output
        assert "Node not found: ghost" in output

    def test_label_message_echoes_the_label_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The label is applied before its message renders, as for promote."""
        node_id = self._create_node()
        force_colour(monkeypatch, curate_cli)
        result = runner.invoke(app, ["curate", "label", node_id, "[bold]l[/x]"])
        assert result.exit_code == 0, plain(result.output)
        rendered = " ".join(assert_coloured(result.stdout).split())
        assert "Message: Label '[bold]l[/x]' added to" in rendered


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

    def test_redact_missing_target_json(self) -> None:
        """A target id that names no node is input to fix: REJECTED, exit 2.

        The JSON keeps ``command_id`` so the refused attempt joins its
        MUTATION_REJECTED audit event.
        """
        result = runner.invoke(
            app,
            ["curate", "redact", "ghost", "--yes", "--reason", "r", "--format", "json"],
        )
        assert result.exit_code == 2, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "rejected"
        assert data["message"] == "Node not found: ghost"
        assert data["command_id"]

    def test_redact_missing_target_text(self) -> None:
        result = runner.invoke(
            app, ["curate", "redact", "ghost", "--yes", "--reason", "r"]
        )
        assert result.exit_code == 2, result.output
        assert "Node not found: ghost" in plain(result.output)

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
        assert result.exit_code == 2
        # 1. Colour really happened.
        rendered = assert_coloured(result.stdout)
        # 2. The markup token survived rendering byte-for-byte.
        assert "ghost[/x]" in rendered, (
            "Rich read the target id as markup and ate it, so the operator "
            "cannot see which id failed"
        )
        # 3. And the error was printed rather than lost to a MarkupError
        #    somewhere upstream of it.
        assert "Node not found" in rendered


class TestCurateDemotePrecedent:
    def _create_precedent(self) -> str:
        r = runner.invoke(
            app,
            ["curate", "entity", "precedent", "Pooling pattern", "--format", "json"],
        )
        node_id: str = json.loads(r.stdout.strip())["node_id"]
        return node_id

    def test_demote_stamps_the_precedent(self) -> None:
        node_id = self._create_precedent()
        result = runner.invoke(
            app,
            [
                "curate",
                "demote-precedent",
                node_id,
                "--reason",
                "superseded by a broader pattern",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "success"
        assert data["operation"] == "precedent.demote"
        assert data["created_id"] == node_id
        assert data["command_id"]

    def test_demote_non_precedent_target_is_refused(self) -> None:
        """Reuses ``TestCurateRedact``'s node (``entity person``, not
        ``precedent``): the handler's ``node_type`` check refuses it."""
        r = runner.invoke(
            app,
            ["curate", "entity", "person", "Junk Mint", "--format", "json"],
        )
        node_id = json.loads(r.stdout.strip())["node_id"]

        result = runner.invoke(
            app,
            [
                "curate",
                "demote-precedent",
                node_id,
                "--reason",
                "r",
                "--format",
                "json",
            ],
        )
        assert result.exit_code != 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "rejected"

    def test_demote_missing_target_json(self) -> None:
        result = runner.invoke(
            app,
            [
                "curate",
                "demote-precedent",
                "ghost",
                "--reason",
                "r",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 2, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "rejected"
        assert "Node not found: ghost" in data["message"]

    def test_demote_blank_reason_rejected(self) -> None:
        node_id = self._create_precedent()
        result = runner.invoke(
            app,
            [
                "curate",
                "demote-precedent",
                node_id,
                "--reason",
                "  ",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 2
        data = json.loads(result.stdout.strip())
        assert data["status"] == "rejected"

    def test_demote_default_requested_by_is_cli(self) -> None:
        """A human-run CLI invocation must carry a non-automatic
        ``requested_by``, so a later human demote of an auto-promoted
        precedent is never mistaken for the automatic path."""
        node_id = self._create_precedent()
        result = runner.invoke(
            app,
            [
                "curate",
                "demote-precedent",
                node_id,
                "--reason",
                "r",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        events = [
            e
            for e in curate_cli._get_registry().operational.event_log.get_events(
                event_type=EventType.PRECEDENT_DEMOTED
            )
            if e.payload.get("target_id") == node_id
        ]
        assert len(events) == 1
        assert events[0].payload["requested_by"] == "cli:demote-precedent"


class TestCurateReinstatePrecedent:
    def _create_precedent(self) -> str:
        r = runner.invoke(
            app,
            ["curate", "entity", "precedent", "Pooling pattern", "--format", "json"],
        )
        node_id: str = json.loads(r.stdout.strip())["node_id"]
        return node_id

    def _demote(self, node_id: str, *, requested_by: str = "cli:demote-precedent") -> None:
        result = runner.invoke(
            app,
            [
                "curate",
                "demote-precedent",
                node_id,
                "--reason",
                "superseded",
                "--by",
                requested_by,
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output

    def test_reinstate_stamps_the_precedent(self) -> None:
        node_id = self._create_precedent()
        self._demote(node_id)

        result = runner.invoke(
            app,
            [
                "curate",
                "reinstate-precedent",
                node_id,
                "--reason",
                "demotion was a mistake",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "success"
        assert data["operation"] == "precedent.reinstate"
        assert data["created_id"] == node_id
        assert data["command_id"]

        graph = curate_cli._get_registry().knowledge.graph_store
        node = graph.get_node(node_id)
        assert node is not None
        assert node["properties"]["lifecycle"]["state"] == "current"

    def test_reinstate_non_demoted_target_is_refused(self) -> None:
        """A freshly-created precedent is ``"current"``, not ``"demoted"``;
        reinstating it must be refused rather than silently accepted, since
        there is no demotion to reverse."""
        node_id = self._create_precedent()

        result = runner.invoke(
            app,
            [
                "curate",
                "reinstate-precedent",
                node_id,
                "--reason",
                "r",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "rejected"
        assert "not 'demoted'" in data["message"]

    def test_reinstate_missing_target_json(self) -> None:
        result = runner.invoke(
            app,
            [
                "curate",
                "reinstate-precedent",
                "ghost",
                "--reason",
                "r",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "rejected"
        assert "Node not found: ghost" in data["message"]

    def test_reinstate_blank_reason_rejected(self) -> None:
        node_id = self._create_precedent()
        self._demote(node_id)

        result = runner.invoke(
            app,
            [
                "curate",
                "reinstate-precedent",
                node_id,
                "--reason",
                "  ",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == EXIT_VALIDATION
        data = json.loads(result.stdout.strip())
        assert data["status"] == "rejected"

    def test_reinstate_automatic_requester_is_refused(self) -> None:
        """Reinstatement has no automatic path at all: the automatic
        demotion policy's own identity must be refused outright, narrowing
        or not -- unlike demotion, which the policy may perform on what it
        itself promoted."""
        node_id = self._create_precedent()
        self._demote(node_id)

        result = runner.invoke(
            app,
            [
                "curate",
                "reinstate-precedent",
                node_id,
                "--reason",
                "r",
                "--by",
                AUTO_DEMOTE_REQUESTED_BY,
                "--format",
                "json",
            ],
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "rejected"
        assert "human-only" in data["message"]

        graph = curate_cli._get_registry().knowledge.graph_store
        node = graph.get_node(node_id)
        assert node is not None
        # Refused, not quietly no-op'd: the precedent is still demoted.
        assert node["properties"]["lifecycle"]["state"] == "demoted"

    def test_reinstate_default_requested_by_is_cli(self) -> None:
        """A human-run CLI invocation must carry a non-automatic
        ``requested_by``, matching ``demote-precedent``'s own guarantee."""
        node_id = self._create_precedent()
        self._demote(node_id)

        result = runner.invoke(
            app,
            [
                "curate",
                "reinstate-precedent",
                node_id,
                "--reason",
                "r",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        events = [
            e
            for e in curate_cli._get_registry().operational.event_log.get_events(
                event_type=EventType.PRECEDENT_REINSTATED
            )
            if e.payload.get("target_id") == node_id
        ]
        assert len(events) == 1
        assert events[0].payload["requested_by"] == "cli:reinstate-precedent"


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

    def test_edge_summary_survives_markup_under_colour(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The table must not parse edge result text as Rich markup (#522)."""
        candidates_path, decisions_path = _write_review_pair(
            tmp_path, target_entity_ids=["doc:target"]
        )
        monkeypatch.setattr(
            curate_cli,
            "submit_learning_promotion",
            lambda *_args, **_kwargs: {
                "status": "promoted",
                "node_id": "precedent:test",
                "edges": [
                    {
                        "edge_kind": "s-[k]",
                        "status": "ok-[v]",
                    }
                ],
            },
        )
        force_colour(monkeypatch, curate_cli)

        result = runner.invoke(
            app,
            [
                "curate",
                "promote-learning",
                "--candidates",
                str(candidates_path),
                "--decisions",
                str(decisions_path),
            ],
        )

        assert result.exit_code == 0, result.output
        rendered = assert_coloured(result.stdout)
        assert "s-[k]:ok-[v]" in rendered


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

    @staticmethod
    def _graph_store_with_no_existing_node() -> MagicMock:
        """A fresh first-time promotion: nothing by this entity_id exists
        yet, so the demoted-precedent refusal check in
        ``submit_learning_promotion`` must not fire."""
        store = MagicMock(spec=GraphStore)
        store.get_node.return_value = None
        return store

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
            graph_store=self._graph_store_with_no_existing_node(),
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
            graph_store=self._graph_store_with_no_existing_node(),
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
            graph_store=self._graph_store_with_no_existing_node(),
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

    def test_refuses_to_reapprove_a_demoted_precedent(self) -> None:
        """A demoted precedent's learning candidate must not be silently
        un-demoted by re-approving it: ``ENTITY_CREATE`` overwrites the
        whole ``properties`` dict, including ``lifecycle``, so without this
        check a re-approval would read the node back as current with no
        reason recorded. The refusal must name ``precedent.reinstate`` and
        must not issue the ``ENTITY_CREATE`` at all — neither a silent
        overwrite nor a silent no-op."""
        executor = MagicMock(spec=MutationExecutor)
        graph_store = MagicMock(spec=GraphStore)
        graph_store.get_node.return_value = {
            "node_id": "precedent://learning/test",
            "node_type": "precedent",
            "properties": {"lifecycle": {"state": "demoted"}},
        }

        outcome = submit_learning_promotion(
            executor,
            self._entity_payload(),
            [self._edge_payload("doc:t1")],
            requested_by="cli:promote-learning",
            graph_store=graph_store,
        )

        assert outcome["status"] == "refused_demoted"
        assert "precedent.reinstate" in outcome["message"]
        executor.execute.assert_not_called()

    def test_does_not_refuse_a_current_precedent(self) -> None:
        """Mutant proof's control case: an existing *current* precedent
        (e.g. a second promotion run touching the same target entity_id)
        must still reach ``ENTITY_CREATE`` — the refusal is specific to
        ``lifecycle.state == "demoted"``, not to "the node already
        exists"."""
        executor = MagicMock(spec=MutationExecutor)
        executor.execute.return_value = CommandResult(
            command_id="cmd-1",
            status=CommandStatus.SUCCESS,
            operation=Operation.ENTITY_CREATE,
            created_id="precedent://learning/test",
        )
        graph_store = MagicMock(spec=GraphStore)
        graph_store.get_node.return_value = {
            "node_id": "precedent://learning/test",
            "node_type": "precedent",
            "properties": {"lifecycle": {"state": "current"}},
        }

        outcome = submit_learning_promotion(
            executor,
            self._entity_payload(),
            [],
            requested_by="cli:promote-learning",
            graph_store=graph_store,
        )

        assert outcome["status"] != "refused_demoted"
        executor.execute.assert_called()


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
            graph_store=registry.knowledge.graph_store,
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


def _auto_promote_candidate(
    candidate_id: str = "cand-1",
    *,
    helpful_count: int = 5,
    distinct_helpful_sessions: int = 3,
    unhelpful_count: int = 0,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "recommendation_type": "promote_precedent",
        "precedent_name": f"Precedent for {candidate_id}",
        "citation_evidence": {
            "helpful_count": helpful_count,
            "distinct_helpful_sessions": distinct_helpful_sessions,
            "unhelpful_count": unhelpful_count,
        },
        "target_entity_ids": [],
    }


class TestCurateAutoPromoteCandidates:
    """CLI-level coverage for ``curate auto-promote-candidates``."""

    def _write_candidates(self, tmp_path: Path, candidates: list[dict]) -> Path:
        path = tmp_path / "candidates.json"
        path.write_text(json.dumps({"candidates": candidates}))
        return path

    def test_dry_run_overrides_a_stored_commit_mode(self, tmp_path: Path) -> None:
        """Without --dry-run a stored mode=commit refuses the whole run
        with a configuration error (exit 5) before any candidate is
        evaluated; --dry-run must still work in that case, running
        report-only and writing nothing — the policy must be forced to
        report_only *before* it is constructed, not after.
        """
        stores_dir = Path(os.environ["TRELLIS_DATA_DIR"]) / "stores"
        SettingsStore(stores_dir / "settings.json").set(
            CANDIDATE_AUTO_PROMOTE_MODE_SETTING, "commit"
        )
        candidates_path = self._write_candidates(tmp_path, [_auto_promote_candidate()])

        # Without --dry-run: the stored commit mode refuses construction.
        refused = runner.invoke(
            app,
            [
                "curate",
                "auto-promote-candidates",
                "--candidates",
                str(candidates_path),
                "--format",
                "json",
            ],
        )
        assert refused.exit_code == 5, refused.output

        # With --dry-run: forced to report_only before the policy is built.
        result = runner.invoke(
            app,
            [
                "curate",
                "auto-promote-candidates",
                "--candidates",
                str(candidates_path),
                "--dry-run",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["mode"] == "report_only"
        assert data["promoted_count"] == 0

        event_log = curate_cli._get_registry().operational.event_log
        mutation_events = event_log.get_events(event_type=EventType.MUTATION_EXECUTED)
        assert mutation_events == []
        # Both invocations emit an event: the refused run records its own
        # refusal (exit 5, below), and the dry-run that followed records
        # its completed evaluation -- every exit path writes a run event.
        evaluated_events = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
        )
        assert len(evaluated_events) == 2
        refused_event, ok_event = evaluated_events
        assert refused_event.payload["outcome"] == "refused"
        assert refused_event.payload["configured_mode"] == "commit"
        assert ok_event.payload["outcome"] == "ok"
        assert ok_event.payload["configured_mode"] == "commit"

    def test_cli_resolves_the_policy_from_settings_not_a_hardcoded_default(
        self, tmp_path: Path
    ) -> None:
        """The CLI must build its policy from settings/env, not a
        hardcoded default: a stored min_helpful_count floor above the
        candidate's helpful_count must flip its disposition.
        """
        stores_dir = Path(os.environ["TRELLIS_DATA_DIR"]) / "stores"
        SettingsStore(stores_dir / "settings.json").set(
            CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_SETTING, 100
        )
        candidates_path = self._write_candidates(
            tmp_path, [_auto_promote_candidate(helpful_count=5)]
        )

        result = runner.invoke(
            app,
            [
                "curate",
                "auto-promote-candidates",
                "--candidates",
                str(candidates_path),
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["ineligible_count"] == 1
        assert data["would_promote_count"] == 0
        assert (
            data["outcomes"][0]["disposition"]
            == "ineligible:insufficient_helpful_count"
        )

    def test_malformed_candidates_file_exits_nonzero_and_emits_a_failed_event(
        self, tmp_path: Path
    ) -> None:
        """A ``--candidates`` file that isn't valid JSON must still leave
        a trace in the event stream, not just a CLI error with no record
        for ``loop_health.py`` to see."""
        bad_path = tmp_path / "bad.json"
        bad_path.write_text("{not valid json")

        result = runner.invoke(
            app,
            [
                "curate",
                "auto-promote-candidates",
                "--candidates",
                str(bad_path),
                "--format",
                "json",
            ],
        )
        assert result.exit_code != 0

        event_log = curate_cli._get_registry().operational.event_log
        evaluated_events = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
        )
        assert len(evaluated_events) == 1
        assert evaluated_events[0].payload["outcome"] == "failed"
        assert evaluated_events[0].payload["error"]["type"] == "BadParameter"

    def test_wrong_top_level_json_shape_exits_nonzero_and_emits_a_failed_event(
        self, tmp_path: Path
    ) -> None:
        """A candidates file whose top level isn't a JSON object (e.g. a
        bare array) must be refused and recorded the same way a malformed
        file is -- it is not "zero candidates", it is a shape error."""
        arr_path = tmp_path / "arr.json"
        arr_path.write_text(json.dumps([1, 2, 3]))

        result = runner.invoke(
            app,
            [
                "curate",
                "auto-promote-candidates",
                "--candidates",
                str(arr_path),
                "--format",
                "json",
            ],
        )
        assert result.exit_code != 0

        event_log = curate_cli._get_registry().operational.event_log
        evaluated_events = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
        )
        assert len(evaluated_events) == 1
        assert evaluated_events[0].payload["outcome"] == "failed"
        assert evaluated_events[0].payload["error"]["type"] == "BadParameter"

    def test_non_finite_env_override_exits_nonzero_and_emits_a_failed_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-finite env override (``inf``/``nan``/``1e999``) for one of
        this command's int knobs must be refused and recorded, not crash
        with an uncaught traceback and no event."""
        monkeypatch.setenv("TRELLIS_CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT", "inf")
        candidates_path = self._write_candidates(tmp_path, [])

        result = runner.invoke(
            app,
            [
                "curate",
                "auto-promote-candidates",
                "--candidates",
                str(candidates_path),
                "--format",
                "json",
            ],
        )
        assert result.exit_code != 0

        event_log = curate_cli._get_registry().operational.event_log
        evaluated_events = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
        )
        assert len(evaluated_events) == 1
        assert evaluated_events[0].payload["outcome"] == "failed"
        assert evaluated_events[0].payload["error"]["type"] == "ValueError"

    def test_non_finite_env_override_message_is_value_free(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The non-finite env override above is itself a resolver
        ``ValueError`` whose ``str()`` embeds the raw rejected value —
        plant a secret-shaped one and assert it reaches neither stdout
        nor stderr nor the recorded event, in either output format, and
        that the clean validation exit code is used (not an uncaught
        traceback's exit 1)."""
        secret = "SEKRETtb11aa"  # noqa: S105 - synthetic test placeholder, not a credential
        monkeypatch.setenv("TRELLIS_CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT", secret)
        for run_number, output_format in enumerate(("json", "text"), start=1):
            candidates_path = self._write_candidates(tmp_path, [])

            result = runner.invoke(
                app,
                [
                    "curate",
                    "auto-promote-candidates",
                    "--candidates",
                    str(candidates_path),
                    "--format",
                    output_format,
                ],
            )
            assert result.exit_code == EXIT_VALIDATION, result.output
            assert secret not in result.output

            event_log = curate_cli._get_registry().operational.event_log
            evaluated_events = event_log.get_events(
                event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
            )
            assert len(evaluated_events) == run_number
            latest = evaluated_events[-1]
            assert latest.payload["outcome"] == "failed"
            assert latest.payload["error"]["type"] == "ValueError"
            assert secret not in json.dumps(latest.payload)

    def test_invalid_env_mode_rejected_with_clean_exit_and_value_free_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrecognized ``TRELLIS_CANDIDATE_AUTO_PROMOTE_MODE`` must be
        refused at resolve time with a clean, value-free exit — not
        silently accepted (an invalid env mode plus ``--dry-run`` used to
        exit 0 with ``outcome: ok``) and not a traceback embedding the
        raw value."""
        secret = "SEKRETmode4b2e1d"  # noqa: S105 - synthetic test placeholder, not a credential
        monkeypatch.setenv("TRELLIS_CANDIDATE_AUTO_PROMOTE_MODE", secret)
        candidates_path = self._write_candidates(tmp_path, [])

        for run_number, extra_args in enumerate(([], ["--dry-run"]), start=1):
            result = runner.invoke(
                app,
                [
                    "curate",
                    "auto-promote-candidates",
                    "--candidates",
                    str(candidates_path),
                    "--format",
                    "json",
                    *extra_args,
                ],
            )
            assert result.exit_code == EXIT_VALIDATION, result.output
            assert secret not in result.output

            event_log = curate_cli._get_registry().operational.event_log
            evaluated_events = event_log.get_events(
                event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
            )
            assert len(evaluated_events) == run_number
            latest = evaluated_events[-1]
            assert latest.payload["outcome"] == "failed"
            assert latest.payload["error"]["type"] == "ValueError"
            assert latest.payload["configured_mode"] is None
            assert secret not in json.dumps(latest.payload)


class TestCurateHelp:
    def test_help(self) -> None:
        result = runner.invoke(app, ["curate", "--help"])
        assert result.exit_code == 0
        for cmd in ["promote", "link", "label", "feedback", "promote-learning"]:
            assert cmd in result.stdout
