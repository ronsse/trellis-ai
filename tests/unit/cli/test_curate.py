"""Tests for curate CLI commands."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from tests.cli_output import assert_coloured, force_colour, plain
from trellis.core.text_safety import replace_nul_and_lone_surrogates
from trellis.learning import submit_learning_promotion
from trellis.learning.candidate_auto_promote import (
    AUTO_PROMOTE_REQUESTED_BY,
    CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_SETTING,
    CANDIDATE_AUTO_PROMOTE_MODE_SETTING,
)
from trellis.learning.precedent_auto_demote import (
    PRECEDENT_AUTO_DEMOTE_ENABLED_SETTING,
    PRECEDENT_AUTO_DEMOTE_MODE_ENV,
    PRECEDENT_AUTO_DEMOTE_MODE_SETTING,
)
from trellis.mutate.commands import (
    Command,
    CommandResult,
    CommandStatus,
    Operation,
)
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import AUTO_DEMOTE_REQUESTED_BY
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.base.graph import GraphStore
from trellis.stores.settings_store import SettingsStore
from trellis_cli import curate as curate_cli
from trellis_cli.exit_codes import EXIT_STORE, EXIT_VALIDATION
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

    def test_redact_purges_a_stored_nul_id(self) -> None:
        """A row an id-minting bug stored with a raw NUL byte, before the
        Stage 1 hazard refusal existed, must still be purgeable through the
        CLI -- cleaning up exactly that kind of row is the main reason to
        redact one. Inserted directly into SQLite (never through a governed
        write) so the fixture reaches the state a minting bug would have
        left, not the state ``entity.create`` would refuse to write today.

        The audit event's own ``target_id`` is asserted scrubbed, not raw:
        REDACTION_APPLIED is emitted by the handler directly, outside
        ``MutationExecutor._emit_event``'s scrub, but ``EventLog.append``
        scrubs ``entity_id``/``payload`` on every ``emit()`` call -- this
        one included -- before any backend sees them. SQLite would accept
        a raw NUL in a JSON string silently where Postgres jsonb refuses
        it (see ``core/text_safety.py``'s module docstring), so asserting
        the raw byte here would pass on this backend and fail in
        production.
        """
        hazard_id = "junk\x00mint"
        graph = curate_cli._get_registry().knowledge.graph_store
        now = datetime.now(UTC).isoformat()
        graph._conn.execute(
            "INSERT INTO nodes (version_id, node_id, node_type, node_role, "
            "generation_spec_json, document_ids_json, properties_json, "
            "created_at, updated_at, valid_from, valid_to) "
            "VALUES (?, ?, 'person', 'semantic', NULL, NULL, '{}', ?, ?, ?, NULL)",
            (f"{hazard_id}-v1", hazard_id, now, now, now),
        )
        graph._conn.commit()
        assert graph.get_node(hazard_id) is not None

        result = runner.invoke(
            app,
            [
                "curate",
                "redact",
                hazard_id,
                "--yes",
                "--reason",
                "defect-minted NUL id",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "success"
        assert graph.get_node(hazard_id) is None

        events = [
            e
            for e in curate_cli._get_registry().operational.event_log.get_events(
                event_type=EventType.REDACTION_APPLIED
            )
            if e.payload.get("command_id") == data["command_id"]
        ]
        assert len(events) == 1
        assert events[0].payload["reason"] == "defect-minted NUL id"
        assert events[0].payload["target_id"] == replace_nul_and_lone_surrogates(
            hazard_id
        )
        assert "\x00" not in events[0].payload["target_id"]
        assert "\x00" not in events[0].entity_id


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

    def _demote(
        self, node_id: str, *, requested_by: str = "cli:demote-precedent"
    ) -> None:
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
        itself promoted.

        ``auto:learning_candidate_demote`` is rostered in
        ``UNATTENDED_WRITERS`` for exactly ``precedent.demote`` (#693-ish
        hardening), so ``MutationExecutor``'s Stage 1 roster check now
        refuses this before ``PrecedentReinstateHandler`` ever runs --
        its own ``reinstate_requires_human`` check is defense in depth,
        unreachable for this identity now, and not exercised by this
        test."""
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
        assert "unattended_writer_operation_not_allowed" in data["message"]
        assert AUTO_DEMOTE_REQUESTED_BY in data["message"]
        assert "precedent.reinstate" in data["message"]

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

    def test_promotion_embeds_an_excerpt_of_the_source_document(
        self, tmp_path: Path
    ) -> None:
        candidates_path, decisions_path = _write_review_pair(tmp_path)
        seed = curate_cli._get_registry()
        try:
            seed.knowledge.document_store.put(
                "lc:doc:helpful", "Check the retry budget before escalating.", {}
            )
        finally:
            seed.close()
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
        node_id = json.loads(result.stdout.strip())["results"][0]["node_id"]
        reader = curate_cli._get_registry()
        try:
            node = reader.knowledge.graph_store.get_node(node_id)
        finally:
            reader.close()
        assert node is not None
        assert "Check the retry budget" in node["properties"]["description"]
        assert node["properties"]["source_excerpt_status"] == "included"

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

        # The stub's edge answered ``ok-[v]``, not ``success``: a half-landed
        # promotion, which the verdict reads as a failed write (#930).
        assert result.exit_code == EXIT_STORE, result.output
        rendered = assert_coloured(result.stdout)
        assert "s-[k]:ok-[v]" in rendered


class TestSubmitPromotion:
    """Direct unit tests for the shared submit_learning_promotion helper.

    These exercise branches that are awkward to reach through the CLI
    runner (entity create rejected, edge create after a successful
    entity) without standing up the full mutation pipeline.
    """

    @staticmethod
    def _entity_payload(entity_id: str = "precedent://learning/test") -> dict:
        return {
            "entity_type": "precedent",
            "entity_id": entity_id,
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

    def test_refuses_to_reapprove_an_archived_precedent(self) -> None:
        """An archived precedent must refuse re-approval the same way a
        demoted one does, naming ``retention.restore`` as the way back —
        the demoted-only check leaves an archive-then-reapprove hole that
        silently reads the node back as current with no reason recorded."""
        executor = MagicMock(spec=MutationExecutor)
        graph_store = MagicMock(spec=GraphStore)
        graph_store.get_node.return_value = {
            "node_id": "precedent://learning/test",
            "node_type": "precedent",
            "properties": {"lifecycle": {"state": "archived"}},
        }

        outcome = submit_learning_promotion(
            executor,
            self._entity_payload(),
            [self._edge_payload("doc:t1")],
            requested_by="cli:promote-learning",
            graph_store=graph_store,
        )

        assert outcome["status"] == "refused_archived"
        assert "retention.restore" in outcome["message"]
        executor.execute.assert_not_called()

    def test_refuses_to_reapprove_a_superseded_precedent(self) -> None:
        """A superseded precedent names its successor, not a verb — there
        is no "un-supersede" operation, only a new promotion naming the
        replacement."""
        executor = MagicMock(spec=MutationExecutor)
        graph_store = MagicMock(spec=GraphStore)
        graph_store.get_node.return_value = {
            "node_id": "precedent://learning/test",
            "node_type": "precedent",
            "properties": {
                "lifecycle": {
                    "state": "superseded",
                    "superseded_by": "precedent://learning/replacement",
                }
            },
        }

        outcome = submit_learning_promotion(
            executor,
            self._entity_payload(),
            [self._edge_payload("doc:t1")],
            requested_by="cli:promote-learning",
            graph_store=graph_store,
        )

        assert outcome["status"] == "refused_not_current"
        assert "precedent://learning/replacement" in outcome["message"]
        executor.execute.assert_not_called()

    def test_demote_prune_reapprove_refuses(self, tmp_path: Path) -> None:
        """End to end against real stores: demote a promoted precedent,
        prune (archive) it, then try to re-approve its learning candidate
        again. Must refuse — not silently restore it to current — because
        the archival sits on top of an unresolved demotion."""
        from trellis.mutate import build_curate_executor
        from trellis.mutate.commands import Command, Operation
        from trellis.stores.registry import StoreRegistry

        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        registry = StoreRegistry(stores_dir=stores_dir)
        executor = build_curate_executor(registry, clearance="restricted")
        graph_store = registry.knowledge.graph_store
        precedent_id = graph_store.upsert_node(
            node_id=None, node_type="precedent", properties={"name": "reapprove test"}
        )

        demote = executor.execute(
            Command(
                operation=Operation.PRECEDENT_DEMOTE,
                args={"target_id": precedent_id, "reason": "no longer applicable"},
                target_id=precedent_id,
                target_type="precedent",
                requested_by="test",
            )
        )
        assert demote.status == CommandStatus.SUCCESS, demote.message

        prune = executor.execute(
            Command(
                operation=Operation.RETENTION_PRUNE,
                args={
                    "criteria": {"lifecycle_states": ["demoted"], "older_than_days": 0},
                    "reason": "sweeping demoted precedents",
                    "dry_run": False,
                },
                requested_by="test",
            )
        )
        assert prune.status == CommandStatus.SUCCESS, prune.message
        node = graph_store.get_node(precedent_id)
        assert node is not None
        assert node["properties"]["lifecycle"]["state"] == "archived"

        outcome = submit_learning_promotion(
            executor,
            self._entity_payload(entity_id=precedent_id),
            [],
            requested_by="cli:promote-learning",
            graph_store=graph_store,
        )

        assert outcome["status"] == "refused_archived"
        assert "retention.restore" in outcome["message"]

    def test_archived_never_demoted_reapprove_refuses(self, tmp_path: Path) -> None:
        """A precedent archived directly (no demotion first) must refuse
        re-approval too — the refusal is keyed on the current lifecycle
        state alone, not on having passed through ``precedent.demote``."""
        from trellis.mutate import build_curate_executor
        from trellis.mutate.commands import Command, Operation
        from trellis.stores.registry import StoreRegistry

        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        registry = StoreRegistry(stores_dir=stores_dir)
        executor = build_curate_executor(registry, clearance="restricted")
        graph_store = registry.knowledge.graph_store
        # Deprecated, not current: "current" and "archived" are both
        # unselectable by retention.prune's lifecycle_states criterion (one
        # is a no-op, the other an assertion the item belongs in service),
        # so a precedent reaching "archived" with no demotion in its past
        # has to start from some other selectable state.
        precedent_id = graph_store.upsert_node(
            node_id=None,
            node_type="precedent",
            properties={
                "name": "never demoted test",
                "lifecycle": {"state": "deprecated"},
            },
        )

        prune = executor.execute(
            Command(
                operation=Operation.RETENTION_PRUNE,
                args={
                    "criteria": {
                        "lifecycle_states": ["deprecated"],
                        "older_than_days": 0,
                    },
                    "reason": "archived without ever being demoted",
                    "dry_run": False,
                },
                requested_by="test",
            )
        )
        assert prune.status == CommandStatus.SUCCESS, prune.message
        node = graph_store.get_node(precedent_id)
        assert node is not None
        assert node["properties"]["lifecycle"]["state"] == "archived"

        outcome = submit_learning_promotion(
            executor,
            self._entity_payload(entity_id=precedent_id),
            [],
            requested_by="cli:promote-learning",
            graph_store=graph_store,
        )

        assert outcome["status"] == "refused_archived"
        assert "retention.restore" in outcome["message"]

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

    def test_reapprove_current_identical_payload_promotes(self, tmp_path: Path) -> None:
        """End to end against real stores (gate-876b R1): re-approving a
        learning candidate whose precedent is already *current* must
        reach the write and come back ``"promoted"``, adding one version
        and one ``PRECEDENT_PROMOTED`` event, same as a fresh first
        approval. ``_precedent_reapproval_refusal`` already lets a current
        precedent's re-approval through before ``ENTITY_CREATE`` is issued;
        it is ``EntityCreateHandler``'s own guard that must not then refuse
        the write a second time just because the id already names a
        precedent.

        This call passes no ``event_log``, so the submission cannot tell
        whether the promotion event landed and writes, as it always did.
        With one, an identical re-submission is ``already_promoted`` and
        writes nothing (#983;
        ``tests/unit/learning/test_submit_promotion_rerun.py``): the CLI
        and REST surfaces all pass it, so what they do on a re-run is pinned
        there and not here.

        Defective subject: an ``EntityCreateHandler`` guard that refuses
        ``entity.create`` over *any* existing precedent regardless of
        lifecycle state (the pre-fix shape) makes the second
        ``submit_learning_promotion`` return ``"entity_failed"`` instead
        of ``"promoted"``, and the version/event counts never move.
        """
        from trellis.mutate import build_curate_executor
        from trellis.stores.registry import StoreRegistry

        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        registry = StoreRegistry(stores_dir=stores_dir)
        executor = build_curate_executor(registry, clearance="restricted")
        graph_store = registry.knowledge.graph_store
        event_log = registry.operational.event_log
        entity_id = "precedent://learning/reapprove-identical"
        payload = self._entity_payload(entity_id=entity_id)

        first = submit_learning_promotion(
            executor,
            payload,
            [],
            requested_by="cli:promote-learning",
            graph_store=graph_store,
        )
        assert first["status"] == "promoted", first
        versions_before = len(graph_store.get_node_history(entity_id))
        promoted_before = len(
            event_log.get_events(
                event_type=EventType.PRECEDENT_PROMOTED, entity_id=entity_id
            )
        )

        second = submit_learning_promotion(
            executor,
            payload,
            [],
            requested_by="cli:promote-learning",
            graph_store=graph_store,
        )

        assert second["status"] == "promoted", second
        assert len(graph_store.get_node_history(entity_id)) == versions_before + 1
        assert (
            len(
                event_log.get_events(
                    event_type=EventType.PRECEDENT_PROMOTED, entity_id=entity_id
                )
            )
            == promoted_before + 1
        )

    def test_reapprove_current_changed_payload_promotes_and_applies_change(
        self, tmp_path: Path
    ) -> None:
        """Same shape as the identical-payload case, but the candidate
        re-scored between runs (e.g. a changed description) -- the
        change must land on the node, not just the status."""
        from trellis.mutate import build_curate_executor
        from trellis.stores.registry import StoreRegistry

        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        registry = StoreRegistry(stores_dir=stores_dir)
        executor = build_curate_executor(registry, clearance="restricted")
        graph_store = registry.knowledge.graph_store
        event_log = registry.operational.event_log
        entity_id = "precedent://learning/reapprove-changed"

        first = submit_learning_promotion(
            executor,
            self._entity_payload(entity_id=entity_id),
            [],
            requested_by="cli:promote-learning",
            graph_store=graph_store,
        )
        assert first["status"] == "promoted", first
        versions_before = len(graph_store.get_node_history(entity_id))
        promoted_before = len(
            event_log.get_events(
                event_type=EventType.PRECEDENT_PROMOTED, entity_id=entity_id
            )
        )

        changed_payload = self._entity_payload(entity_id=entity_id)
        changed_payload["properties"] = {"description": "re-scored description"}
        second = submit_learning_promotion(
            executor,
            changed_payload,
            [],
            requested_by="cli:promote-learning",
            graph_store=graph_store,
        )

        assert second["status"] == "promoted", second
        node = graph_store.get_node(entity_id)
        assert node is not None
        assert node["properties"]["description"] == "re-scored description"
        assert len(graph_store.get_node_history(entity_id)) == versions_before + 1
        assert (
            len(
                event_log.get_events(
                    event_type=EventType.PRECEDENT_PROMOTED, entity_id=entity_id
                )
            )
            == promoted_before + 1
        )

    def test_reapprove_after_reinstate_promotes(self, tmp_path: Path) -> None:
        """A precedent demoted and then legitimately reinstated
        (``precedent.reinstate``) is current again -- re-approving its
        learning candidate after that must succeed exactly like the
        never-demoted case, not stay refused as if it were still
        demoted."""
        from trellis.mutate import build_curate_executor
        from trellis.stores.registry import StoreRegistry

        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        registry = StoreRegistry(stores_dir=stores_dir)
        executor = build_curate_executor(registry, clearance="restricted")
        graph_store = registry.knowledge.graph_store
        entity_id = "precedent://learning/reapprove-reinstated"
        payload = self._entity_payload(entity_id=entity_id)

        first = submit_learning_promotion(
            executor,
            payload,
            [],
            requested_by="cli:promote-learning",
            graph_store=graph_store,
        )
        assert first["status"] == "promoted", first

        demote_result = executor.execute(
            Command(
                operation=Operation.PRECEDENT_DEMOTE,
                args={"target_id": entity_id, "reason": "no longer applicable"},
                target_id=entity_id,
                target_type="precedent",
                requested_by="test",
            )
        )
        assert demote_result.status == CommandStatus.SUCCESS, demote_result.message

        reinstate_result = executor.execute(
            Command(
                operation=Operation.PRECEDENT_REINSTATE,
                args={"target_id": entity_id, "reason": "false positive"},
                target_id=entity_id,
                target_type="precedent",
                requested_by="test",
            )
        )
        assert reinstate_result.status == CommandStatus.SUCCESS, (
            reinstate_result.message
        )

        second = submit_learning_promotion(
            executor,
            payload,
            [],
            requested_by="cli:promote-learning",
            graph_store=graph_store,
        )

        assert second["status"] == "promoted", second
        node = graph_store.get_node(entity_id)
        assert node is not None
        # ENTITY_CREATE replaces ``properties`` wholesale, so a
        # re-approval of an already-current precedent drops any explicit
        # ``lifecycle`` key entirely rather than writing "current" back --
        # absent reads as current by the fail-open convention
        # ``_current_lifecycle_state`` shares with ``retrieve.lifecycle``.
        lifecycle = node["properties"].get("lifecycle") or {}
        assert lifecycle.get("state", "current") == "current"


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
        executor = build_curate_executor(registry, clearance="restricted")

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

    def test_the_run_is_given_the_document_store_for_source_excerpts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = curate_cli.run_candidate_auto_promotion
        seen: dict[str, object] = {}

        def spy(**kwargs: object) -> object:
            seen.update(kwargs)
            return real(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(curate_cli, "run_candidate_auto_promotion", spy)
        candidates_path = self._write_candidates(tmp_path, [_auto_promote_candidate()])
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
        assert seen["document_store"] is not None

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
        # The refusal must name both the stored mode ("commit") and the
        # setting an operator would go edit — not a generic failure --
        # in the JSON message (gate-879 follow-up #6).
        refused_payload = json.loads(refused.stdout.strip())
        assert refused_payload["setting"] == CANDIDATE_AUTO_PROMOTE_MODE_SETTING
        assert CANDIDATE_AUTO_PROMOTE_MODE_SETTING in refused_payload["message"]
        assert "commit" in refused_payload["message"]

        # Same scenario rendered as text (the CLI's default format) must
        # name both too -- this is the asymmetry gate-879 follow-up #6
        # flagged: the JSON branch already carried `setting` as a
        # separate field, but the rendered text previously had no way
        # for a reader to learn the setting's dotted path at all.
        refused_text = runner.invoke(
            app,
            [
                "curate",
                "auto-promote-candidates",
                "--candidates",
                str(candidates_path),
            ],
        )
        assert refused_text.exit_code == 5, refused_text.output
        refused_text_plain = plain(refused_text.stdout)
        assert CANDIDATE_AUTO_PROMOTE_MODE_SETTING in refused_text_plain
        assert "commit" in refused_text_plain

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
        # All three invocations emit an event: the two refused runs above
        # (JSON then text format) each record their own refusal, and the
        # dry-run that followed records its completed evaluation -- every
        # exit path writes a run event.
        evaluated_events = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
        )
        assert len(evaluated_events) == 3
        refused_event, refused_text_event, ok_event = evaluated_events
        assert refused_event.payload["outcome"] == "refused"
        assert refused_event.payload["configured_mode"] == "commit"
        assert refused_text_event.payload["outcome"] == "refused"
        assert refused_text_event.payload["configured_mode"] == "commit"
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

    def test_bogus_demote_mode_is_reported_under_its_own_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bogus ``TRELLIS_PRECEDENT_AUTO_DEMOTE_MODE`` is the *sibling*
        policy's own configuration error -- it must not fail this command
        as a candidate auto-promote problem (gate-879 follow-up #6).
        ``--format json`` must also answer with a JSON payload carrying
        ``status``, and the exit code must match the text surface
        (tests/unit/test_format_exit_parity_rule.py)."""
        secret = "SEKRETdemote91c2"  # noqa: S105 - synthetic test placeholder
        monkeypatch.setenv(PRECEDENT_AUTO_DEMOTE_MODE_ENV, secret)
        exit_codes: set[int] = set()
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
            exit_codes.add(result.exit_code)
            assert result.exit_code == EXIT_VALIDATION, result.output
            assert secret not in result.output
            assert "candidate auto-promote" not in result.output
            assert "precedent auto-demote" in result.output
            if output_format == "json":
                data = json.loads(result.stdout.strip())
                assert data["status"] == "error"
                assert data["error_type"] == "ValueError"

            event_log = curate_cli._get_registry().operational.event_log
            promote_events = event_log.get_events(
                event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
            )
            demote_events = event_log.get_events(
                event_type=EventType.LEARNING_AUTO_DEMOTE_EVALUATED
            )
            # The command's own run also ended without outcomes, so it
            # records one promote event per run as well.
            assert len(promote_events) == run_number
            assert promote_events[-1].payload["outcome"] == "failed"
            assert secret not in json.dumps(promote_events[-1].payload)
            assert len(demote_events) == run_number
            latest = demote_events[-1]
            assert latest.payload["outcome"] == "failed"
            assert latest.payload["error"]["type"] == "ValueError"
            assert secret not in json.dumps(latest.payload)
        assert exit_codes == {EXIT_VALIDATION}

    def test_hand_edited_demote_commit_is_reported_under_its_own_name(
        self, tmp_path: Path
    ) -> None:
        """A hand-edited ``settings.json`` that sets the *sibling*
        demote policy's mode to ``commit`` is a precedent auto-demote
        refusal, not a candidate auto-promote one -- the failure event
        this command emits must name the policy that actually failed."""
        stores_dir = Path(os.environ["TRELLIS_DATA_DIR"]) / "stores"
        SettingsStore(stores_dir / "settings.json").set(
            PRECEDENT_AUTO_DEMOTE_MODE_SETTING, "commit"
        )
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
        assert result.exit_code == 5, result.output

        event_log = curate_cli._get_registry().operational.event_log
        promote_events = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
        )
        demote_events = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_DEMOTE_EVALUATED
        )
        assert len(promote_events) == 1
        assert promote_events[0].payload["outcome"] == "refused"
        assert len(demote_events) == 1
        assert demote_events[0].payload["outcome"] == "refused"
        assert demote_events[0].payload["error"]["type"] == "ConfigError"


class TestCurateEvaluatePrecedentDemotion:
    """CLI-level coverage for ``curate evaluate-precedent-demotion``.

    The gate decision and the PACK_ASSEMBLED/FEEDBACK_RECORDED join are
    both already covered exhaustively at the unit level in
    ``test_precedent_evidence_gate.py``; commit mode, rate-limiting and
    the default-off posture have their own exhaustive unit coverage in
    ``test_precedent_auto_demote.py``. This class only pins the CLI
    surface: option wiring, output shape in both formats, and that the
    attribution caveat actually reaches a caller.
    """

    def _emit_promote(
        self,
        entity_id: str,
        *,
        at: datetime,
        requested_by: str | None = AUTO_PROMOTE_REQUESTED_BY,
    ) -> None:
        """Default ``requested_by`` to an auto-promoted provenance.

        This class pins the CLI's option wiring and output shape, not
        provenance filtering (that is covered exhaustively in
        ``test_precedent_auto_demote.py`` and
        ``test_precedent_evidence_gate.py``), so a precedent built here
        must clear the evidence gate's own ``_was_auto_promoted``
        pre-filter by default to reach the gate decision these tests
        actually assert on. Pass ``requested_by=None`` explicitly for a
        human-promoted precedent.
        """
        payload: dict[str, object] = {
            "operation": Operation.PRECEDENT_PROMOTE.value,
            "status": "success",
        }
        if requested_by is not None:
            payload["requested_by"] = requested_by
        curate_cli._get_registry().operational.event_log.append(
            Event(
                event_type=EventType.MUTATION_EXECUTED,
                source="test",
                entity_id=entity_id,
                payload=payload,
                occurred_at=at,
            )
        )

    def _upsert_graph_backed_precedent(self, entity_id: str) -> None:
        """A promoted precedent only reaches the citation gate at all
        when it has a graph node -- a trace-/miner-sourced promotion
        (never given one) is skipped as ``not_graph_backed`` instead.
        Most of this class's scenarios are about the gate decision
        itself, so they need a node to get there."""
        curate_cli._get_registry().knowledge.graph_store.upsert_node(
            node_id=entity_id, node_type="precedent", properties={}
        )

    def _emit_pack_assembled(
        self, *, pack_id: str, item_ids: list[str], at: datetime
    ) -> None:
        curate_cli._get_registry().operational.event_log.append(
            Event(
                event_type=EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "intent": "test",
                    "domain": "x",
                    "items_count": len(item_ids),
                    "injected_items": [
                        {"item_id": item_id, "item_type": "precedent", "rank": 1}
                        for item_id in item_ids
                    ],
                },
                occurred_at=at,
            )
        )

    def _emit_feedback(
        self,
        *,
        pack_id: str,
        at: datetime,
        helpful: list[str] | None = None,
        unhelpful: list[str] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "pack_id": pack_id,
            "rating": 0.5,
            "success": True,
        }
        if helpful:
            payload["helpful_item_ids"] = list(helpful)
        if unhelpful:
            payload["unhelpful_item_ids"] = list(unhelpful)
        curate_cli._get_registry().operational.event_log.append(
            Event(
                event_type=EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=f"fb-{pack_id}",
                payload=payload,
                occurred_at=at,
            )
        )

    def test_empty_log_reports_zero_counts_and_emits_one_event(self) -> None:
        result = runner.invoke(
            app, ["curate", "evaluate-precedent-demotion", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["enabled"] is False
        assert data["mode"] == "report_only"
        assert data["would_demote_count"] == 0
        assert data["demoted_count"] == 0
        assert data["skipped_rate_limited_count"] == 0
        assert data["refused_count"] == 0
        assert data["already_demoted_count"] == 0
        assert data["ineligible_count"] == 0
        assert data["outcomes"] == []
        assert data["attribution_caveat"]

        event_log = curate_cli._get_registry().operational.event_log
        evaluated = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_DEMOTE_EVALUATED
        )
        assert len(evaluated) == 1

    def test_promoted_precedent_with_no_citations_is_refused(self) -> None:
        self._upsert_graph_backed_precedent("prec-1")
        self._emit_promote("prec-1", at=datetime(2026, 1, 1, tzinfo=UTC))

        result = runner.invoke(
            app, ["curate", "evaluate-precedent-demotion", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["would_demote_count"] == 0
        assert data["ineligible_count"] == 1
        outcome = data["outcomes"][0]
        assert outcome["entity_id"] == "prec-1"
        assert outcome["disposition"] == "ineligible:no_evidence_supplied"

    def test_would_demote_a_precedent_cited_unhelpful_at_the_floor(self) -> None:
        self._upsert_graph_backed_precedent("prec-1")
        promoted_at = datetime(2026, 1, 1, tzinfo=UTC)
        cited_at = promoted_at + timedelta(days=1)
        self._emit_promote("prec-1", at=promoted_at)
        for n in range(2):
            pack_id = f"pack-{n}"
            self._emit_pack_assembled(pack_id=pack_id, item_ids=["prec-1"], at=cited_at)
            self._emit_feedback(pack_id=pack_id, at=cited_at, unhelpful=["prec-1"])

        result = runner.invoke(
            app, ["curate", "evaluate-precedent-demotion", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["would_demote_count"] == 1
        outcome = next(o for o in data["outcomes"] if o["entity_id"] == "prec-1")
        assert outcome["disposition"] == "would_demote"
        assert outcome["unhelpful_count"] == 2

    def test_min_unhelpful_citations_option_raises_the_floor(self) -> None:
        """The same two unhelpful citations that clear the default floor
        (2) are refused once ``--min-unhelpful-citations`` raises it to
        3 -- proving the option actually reaches the gate rather than
        just being accepted and ignored."""
        self._upsert_graph_backed_precedent("prec-1")
        promoted_at = datetime(2026, 1, 1, tzinfo=UTC)
        cited_at = promoted_at + timedelta(days=1)
        self._emit_promote("prec-1", at=promoted_at)
        for n in range(2):
            pack_id = f"pack-{n}"
            self._emit_pack_assembled(pack_id=pack_id, item_ids=["prec-1"], at=cited_at)
            self._emit_feedback(pack_id=pack_id, at=cited_at, unhelpful=["prec-1"])

        result = runner.invoke(
            app,
            [
                "curate",
                "evaluate-precedent-demotion",
                "--min-unhelpful-citations",
                "3",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["would_demote_count"] == 0
        outcome = next(o for o in data["outcomes"] if o["entity_id"] == "prec-1")
        assert outcome["disposition"] == "ineligible:insufficient_unhelpful_citations"

    def test_contested_citations_are_refused_not_demoted(self) -> None:
        """Helpful tying unhelpful refuses even once unhelpful alone
        would clear the floor -- the rule this module exists for,
        pinned once more at the CLI surface."""
        self._upsert_graph_backed_precedent("prec-1")
        promoted_at = datetime(2026, 1, 1, tzinfo=UTC)
        cited_at = promoted_at + timedelta(days=1)
        self._emit_promote("prec-1", at=promoted_at)
        for n, kind in enumerate(["unhelpful", "unhelpful", "helpful", "helpful"]):
            pack_id = f"pack-{n}"
            self._emit_pack_assembled(pack_id=pack_id, item_ids=["prec-1"], at=cited_at)
            if kind == "unhelpful":
                self._emit_feedback(pack_id=pack_id, at=cited_at, unhelpful=["prec-1"])
            else:
                self._emit_feedback(pack_id=pack_id, at=cited_at, helpful=["prec-1"])

        result = runner.invoke(
            app, ["curate", "evaluate-precedent-demotion", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["would_demote_count"] == 0
        outcome = next(o for o in data["outcomes"] if o["entity_id"] == "prec-1")
        assert outcome["disposition"] == "ineligible:contested_by_helpful_citations"
        assert outcome["helpful_count"] == 2
        assert outcome["unhelpful_count"] == 2

    def test_text_format_states_the_attribution_caveat(self) -> None:
        result = runner.invoke(app, ["curate", "evaluate-precedent-demotion"])
        assert result.exit_code == 0, result.output
        output = plain(result.stdout)
        assert "sectioned" in output
        assert "get_context" in output
        assert "would_demote: 0" in output

    def test_json_format_surfaces_the_not_graph_backed_skip_reason(self) -> None:
        """Fix 1 at the CLI surface: a precedent with a successful
        ``precedent.promote`` event but no graph node (the trace-/miner-
        sourced path) is skipped with its own slug, not judged as if the
        demote handler could ever act on it."""
        self._emit_promote("prec-1", at=datetime(2026, 1, 1, tzinfo=UTC))

        result = runner.invoke(
            app, ["curate", "evaluate-precedent-demotion", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["ineligible_count"] == 1
        outcome = data["outcomes"][0]
        assert outcome["entity_id"] == "prec-1"
        assert outcome["disposition"] == "ineligible:not_graph_backed"
        assert outcome["reason"] == "not_graph_backed"

    def test_text_format_surfaces_the_not_graph_backed_skip_reason(self) -> None:
        """Same as above, text format -- the existing per-outcome loop
        already prints ``reason``, so this only pins that the new slug
        actually reaches it rather than silently reading ``would_demote``
        or some other disposition."""
        self._emit_promote("prec-1", at=datetime(2026, 1, 1, tzinfo=UTC))

        result = runner.invoke(app, ["curate", "evaluate-precedent-demotion"])
        assert result.exit_code == 0, result.output
        output = plain(result.stdout)
        assert "ineligible: 1" in output
        assert "not_graph_backed" in output

    def test_untruncated_scan_prints_no_window_note(self) -> None:
        self._emit_promote("prec-1", at=datetime(2026, 1, 1, tzinfo=UTC))

        result = runner.invoke(app, ["curate", "evaluate-precedent-demotion"])
        assert result.exit_code == 0, result.output
        assert "window" not in plain(result.stdout)

    def test_truncated_discovery_scan_prints_the_window_note(self) -> None:
        """Fix 2 at the CLI surface: a capped discovery scan can hide an
        older precedent entirely, not merely under-count one it already
        knows about. Mirrors ``TestTruncationReachesTheOperator`` in
        ``test_analyze.py`` -- drive the real CLI and read stdout rather
        than asserting the note only lands on the report object."""
        promoted_at = datetime(2026, 1, 1, tzinfo=UTC)
        for n in range(501):  # one past the 500-event discovery-scan cap
            self._emit_promote("flood", at=promoted_at + timedelta(seconds=n))

        result = runner.invoke(app, ["curate", "evaluate-precedent-demotion"])
        assert result.exit_code == 0, result.output
        rendered = plain(result.stdout)
        assert "TRUNCATED" in rendered
        assert "window" in rendered

    def test_truncated_discovery_scan_sets_the_json_scan_field(self) -> None:
        promoted_at = datetime(2026, 1, 1, tzinfo=UTC)
        for n in range(501):
            self._emit_promote("flood", at=promoted_at + timedelta(seconds=n))

        result = runner.invoke(
            app, ["curate", "evaluate-precedent-demotion", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["scan"]["truncated"] is True
        assert data["notes"]

    def test_default_off_leaves_a_would_demote_precedent_unwritten(self) -> None:
        """``enabled`` defaults ``False``, so a precedent that otherwise
        clears the demotion floor is reported ``would_demote`` and
        nothing is written -- the same posture
        ``TestCurateAutoPromoteCandidates`` pins for its own policy."""
        self._upsert_graph_backed_precedent("prec-1")
        promoted_at = datetime(2026, 1, 1, tzinfo=UTC)
        cited_at = promoted_at + timedelta(days=1)
        self._emit_promote("prec-1", at=promoted_at)
        for n in range(2):
            pack_id = f"pack-{n}"
            self._emit_pack_assembled(pack_id=pack_id, item_ids=["prec-1"], at=cited_at)
            self._emit_feedback(pack_id=pack_id, at=cited_at, unhelpful=["prec-1"])

        result = runner.invoke(
            app, ["curate", "evaluate-precedent-demotion", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["enabled"] is False
        assert data["mode"] == "report_only"
        assert data["would_demote_count"] == 1
        assert data["demoted_count"] == 0

        event_log = curate_cli._get_registry().operational.event_log
        demote_events = [
            e
            for e in event_log.get_events(event_type=EventType.MUTATION_EXECUTED)
            if e.payload.get("operation") == Operation.PRECEDENT_DEMOTE.value
        ]
        assert demote_events == []
        graph_store = curate_cli._get_registry().knowledge.graph_store
        node = graph_store.get_node("prec-1")
        assert node is not None
        assert "lifecycle" not in node["properties"]

    def test_dry_run_overrides_a_stored_commit_mode(self) -> None:
        """A hand-written stored ``mode=commit`` refuses a real run (exit
        5) and writes nothing; ``--dry-run`` writes nothing anyway, so it
        still runs report-only, and its event names the stored mode."""
        stores_dir = Path(os.environ["TRELLIS_DATA_DIR"]) / "stores"
        store = SettingsStore(stores_dir / "settings.json")
        store.set(PRECEDENT_AUTO_DEMOTE_ENABLED_SETTING, True)
        store.set(PRECEDENT_AUTO_DEMOTE_MODE_SETTING, "commit")
        self._upsert_graph_backed_precedent("prec-1")
        promoted_at = datetime(2026, 1, 1, tzinfo=UTC)
        cited_at = promoted_at + timedelta(days=1)
        self._emit_promote("prec-1", at=promoted_at)
        for n in range(2):
            pack_id = f"pack-{n}"
            self._emit_pack_assembled(pack_id=pack_id, item_ids=["prec-1"], at=cited_at)
            self._emit_feedback(pack_id=pack_id, at=cited_at, unhelpful=["prec-1"])

        refused = runner.invoke(
            app, ["curate", "evaluate-precedent-demotion", "--format", "json"]
        )
        assert refused.exit_code == 5, refused.output

        result = runner.invoke(
            app,
            ["curate", "evaluate-precedent-demotion", "--dry-run", "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["mode"] == "report_only"
        assert data["would_demote_count"] == 1
        assert data["demoted_count"] == 0

        registry = curate_cli._get_registry()
        node = registry.knowledge.graph_store.get_node("prec-1")
        assert node is not None
        assert "lifecycle" not in node["properties"]
        evaluated = registry.operational.event_log.get_events(
            event_type=EventType.LEARNING_AUTO_DEMOTE_EVALUATED
        )
        assert [e.payload["outcome"] for e in evaluated] == ["refused", "ok"]
        assert all(e.payload["configured_mode"] == "commit" for e in evaluated)

    def test_commit_mode_demotes_a_would_demote_precedent_through_the_executor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enabling the policy through a stored settings row for
        ``enabled``, and mode through
        :data:`~trellis.learning.precedent_auto_demote.PRECEDENT_AUTO_DEMOTE_MODE_ENV`
        -- a *stored* ``mode="commit"`` is refused at read time
        regardless of how it reached the settings file (see
        ``learning.precedent_auto_demote.mode``'s ``refused_values``,
        and ``TestResolveOverridesPrecedence`` in
        ``test_precedent_auto_demote.py`` for the unit-level coverage of
        that refusal), so the environment variable is the only path
        left to commit mode -- demotes a precedent whose citations
        clear the floor through the governed ``precedent.demote``
        path."""
        stores_dir = Path(os.environ["TRELLIS_DATA_DIR"]) / "stores"
        settings_store = SettingsStore(stores_dir / "settings.json")
        settings_store.set(PRECEDENT_AUTO_DEMOTE_ENABLED_SETTING, True)
        monkeypatch.setenv(PRECEDENT_AUTO_DEMOTE_MODE_ENV, "commit")

        self._upsert_graph_backed_precedent("prec-1")
        promoted_at = datetime(2026, 1, 1, tzinfo=UTC)
        cited_at = promoted_at + timedelta(days=1)
        # requested_by must mark this as an auto-promotion: the demote
        # handler's own provenance check refuses an auto-requested
        # demotion of any precedent it cannot trace back to an
        # auto-promote event (``AUTO_DEMOTE_REQUESTED_BY`` in
        # ``trellis.mutate.handlers``), so a human-shaped promote event
        # here would make this precedent ineligible, not demoted.
        self._emit_promote(
            "prec-1", at=promoted_at, requested_by=AUTO_PROMOTE_REQUESTED_BY
        )
        for n in range(2):
            pack_id = f"pack-{n}"
            self._emit_pack_assembled(pack_id=pack_id, item_ids=["prec-1"], at=cited_at)
            self._emit_feedback(pack_id=pack_id, at=cited_at, unhelpful=["prec-1"])

        result = runner.invoke(
            app, ["curate", "evaluate-precedent-demotion", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["enabled"] is True
        assert data["mode"] == "commit"
        assert data["demoted_count"] == 1
        outcome = next(o for o in data["outcomes"] if o["entity_id"] == "prec-1")
        assert outcome["disposition"] == "demoted"

        event_log = curate_cli._get_registry().operational.event_log
        demote_events = event_log.get_events(event_type=EventType.MUTATION_EXECUTED)
        demote_events = [
            e
            for e in demote_events
            if e.payload.get("operation") == Operation.PRECEDENT_DEMOTE.value
        ]
        assert len(demote_events) == 1
        assert demote_events[0].payload["status"] == "success"
        assert demote_events[0].payload["requested_by"] == AUTO_DEMOTE_REQUESTED_BY

        graph_store = curate_cli._get_registry().knowledge.graph_store
        node = graph_store.get_node("prec-1")
        assert node is not None
        assert node["properties"]["lifecycle"]["state"] == "demoted"


class TestCurateHelp:
    def test_help(self) -> None:
        result = runner.invoke(app, ["curate", "--help"])
        assert result.exit_code == 0
        for cmd in ["promote", "link", "label", "feedback", "promote-learning"]:
            assert cmd in result.stdout


class TestCurateClassifySensitivity:
    """``trellis curate classify-sensitivity`` relabels a stored document (#194)."""

    @staticmethod
    def _seed(doc_id: str = "doc-1") -> None:
        from trellis_cli.stores import _get_registry

        registry = _get_registry()
        registry.knowledge.document_store.put(doc_id, "deploy checklist", {"t": "x"})

    @staticmethod
    def _label(doc_id: str = "doc-1") -> str:
        from trellis.schemas.classification import sensitivity_of
        from trellis_cli.stores import _get_registry

        row = _get_registry().knowledge.document_store.get(doc_id)
        assert row is not None
        return sensitivity_of(row["metadata"])

    def test_relabels_and_reports_json(self) -> None:
        self._seed()
        result = runner.invoke(
            app,
            [
                "curate",
                "classify-sensitivity",
                "doc-1",
                "--sensitivity",
                "restricted",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout.strip())
        assert data["status"] == "success"
        assert data["operation"] == "classification.set"
        assert self._label() == "restricted"

    def test_a_non_public_relabel_of_an_unlabelled_document_claims_no_rewrite(
        self,
    ) -> None:
        self._seed()
        result = runner.invoke(
            app,
            [
                "curate",
                "classify-sensitivity",
                "doc-1",
                "--sensitivity",
                "internal",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Classification set: doc-1 internal -> internal" in result.stdout
        assert "rewritten" not in result.stdout

    def test_a_refusal_exits_non_zero_in_both_formats(self) -> None:
        self._seed()
        codes = []
        for fmt in ("json", "text"):
            result = runner.invoke(
                app,
                [
                    "curate",
                    "classify-sensitivity",
                    "doc-1",
                    "--sensitivity",
                    "top-secret",
                    "--format",
                    fmt,
                ],
            )
            codes.append(result.exit_code)
        assert codes[0] == codes[1] != 0
        assert self._label() == "internal"

    @staticmethod
    def _stored_record(doc_id: str = "doc-1") -> dict[str, object]:
        from trellis.schemas.classification import DATA_CLASSIFICATION_KEY
        from trellis_cli.stores import _get_registry

        row = _get_registry().knowledge.document_store.get(doc_id)
        assert row is not None
        return row["metadata"][DATA_CLASSIFICATION_KEY]

    @staticmethod
    def _seed_record(record: dict[str, object], doc_id: str = "doc-1") -> None:
        from trellis.schemas.classification import DATA_CLASSIFICATION_KEY
        from trellis_cli.stores import _get_registry

        _get_registry().knowledge.document_store.put(
            doc_id, "deploy checklist", {DATA_CLASSIFICATION_KEY: record}
        )

    def test_public_rewrites_a_bare_record_to_the_exact_one(self) -> None:
        """Corpus frontmatter stores a bare record; this is the repair."""
        from trellis.schemas.classification import public_label_record

        self._seed_record({"sensitivity": "public"})
        result = runner.invoke(
            app,
            [
                "curate",
                "classify-sensitivity",
                "doc-1",
                "--sensitivity",
                "public",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        assert self._stored_record() == public_label_record()

    def test_public_over_a_record_with_tags_is_refused_in_both_formats(self) -> None:
        record = {
            "sensitivity": "public",
            "regulatory_tags": ["gdpr"],
            "jurisdiction": [],
            "classified_by": ["caller"],
            "classification_version": "1",
        }
        self._seed_record(record)
        codes = []
        for fmt in ("json", "text"):
            result = runner.invoke(
                app,
                [
                    "curate",
                    "classify-sensitivity",
                    "doc-1",
                    "--sensitivity",
                    "public",
                    "--format",
                    fmt,
                ],
            )
            codes.append(result.exit_code)
        assert codes[0] == codes[1] != 0
        assert self._stored_record() == record

    def test_a_missing_document_is_refused(self) -> None:
        result = runner.invoke(
            app,
            [
                "curate",
                "classify-sensitivity",
                "ghost",
                "--sensitivity",
                "restricted",
                "--format",
                "json",
            ],
        )
        assert result.exit_code != 0
        assert json.loads(result.stdout.strip())["status"] == "rejected"
