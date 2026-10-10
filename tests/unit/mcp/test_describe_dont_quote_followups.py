"""Follow-up to trellis-ai#829/#836/#837's "describe, don't quote" sweep
(issue #206): the MCP tool surface still carried two forbidden shapes.

* A pydantic ``ValidationError`` rendered by raw ``str(exc)``/f-string
  interpolation, which embeds the caller's own rejected value
  (``save_experience``'s trace JSON, ``record_observation``'s
  ``Observation`` fields, ``execute_mutation``'s ``Command``
  construction). Fixed by routing each through
  :func:`trellis.core.error_sanitize.describe_validation_error`, which
  calls ``errors(include_input=False)`` and never serializes the
  rejected value at all.
* A raw, unconditional ``CommandResult.message`` reaching an agent reply
  (``server.py``'s ``_result_message`` helper, used by
  ``save_experience``, ``record_observation``, ``execute_mutation``,
  ``_raise_create_failed`` and ``_store_new_memory``;
  ``supersession.py``'s ``_execute``; ``knowledge_links.py``'s
  ``_describe_unsuccessful``). A FAILED/REJECTED result's message can
  carry a store's own rejection detail; fixed by gating each through
  :func:`trellis.core.error_sanitize.sanitize_error_message` on
  FAILED/REJECTED only, matching #836's convention on the REST boundary
  (``_results.py``'s ``_SANITIZED_STATUSES``). SUCCESS and DUPLICATE
  only ever restate the caller's own request, so they pass through
  unsanitized — proven below by a control case per helper.

Every hostile-value test proves both halves: the planted credential-shaped
string is absent from the reply, and the type/location/status that
replaces it is present.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from trellis.core.error_sanitize import SUPPRESSED_MARKER
from trellis.mcp.knowledge_links import _describe_unsuccessful, link_knowledge_node
from trellis.mcp.server import _result_message
from trellis.mcp.supersession import _execute as supersession_execute
from trellis.mutate import Command, CommandResult, CommandStatus, Operation
from trellis.schemas.well_known import CONCEPT, SOFTWARE_APPLICATION

from .conftest import unwrap_tool

if TYPE_CHECKING:
    from trellis.stores.base.graph import GraphStore
    from trellis.stores.registry import StoreRegistry

save_experience = unwrap_tool(server_mod.save_experience)
record_observation = unwrap_tool(server_mod.record_observation)
execute_mutation = unwrap_tool(server_mod.execute_mutation)

#: A credential-shaped planted value (the task brief's own example). Trips
#: the secret-shaped-assignment leak heuristic in
#: ``trellis.core.error_sanitize`` when it reaches ``sanitize_error_message``,
#: and is never serialized at all when it reaches ``describe_validation_error``
#: (``input_value`` is dropped unconditionally, regardless of shape).
HOSTILE = "sk-ant-TESTTOKEN-9f8e7d6c5b4a"


class _StubExecutor:
    """A minimal executor exposing only ``.execute``, returning a fixed
    ``CommandResult`` regardless of the command. The full governed
    pipeline (policy gates, idempotency, real store writes) is
    irrelevant to whether caller-facing text is sanitized — only the
    returned ``CommandResult.status``/``message`` matters to the helpers
    under test.
    """

    def __init__(self, result: CommandResult) -> None:
        self._result = result

    def execute(self, command: Command) -> CommandResult:
        del command
        return self._result


def _result(status: CommandStatus, message: str) -> CommandResult:
    return CommandResult(
        command_id="c1",
        status=status,
        operation=Operation.LINK_CREATE,
        message=message,
    )


def _command() -> Command:
    return Command(
        operation=Operation.ENTITY_UPDATE, target_id="n1", requested_by="test"
    )


# ---------------------------------------------------------------------------
# describe_validation_error sites: a pydantic ValidationError, never str(exc)
# ---------------------------------------------------------------------------


class TestPydanticSitesDescribeNotQuote:
    def test_save_experience_invalid_source_drops_the_value(self) -> None:
        hostile_json = json.dumps(
            {"source": {"leak": HOSTILE}, "intent": "x", "context": {}}
        )
        with pytest.raises(McpError) as excinfo:
            save_experience(hostile_json)
        message = excinfo.value.error.message
        assert HOSTILE not in message
        assert "source" in message
        assert "enum" in message

    def test_record_observation_invalid_confidence_drops_the_value(self) -> None:
        raw = record_observation(
            subject_entity_id="e1",
            subject_entity_type="service",
            observer_agent_id="agent-1",
            content="observed something",
            confidence={"leak": HOSTILE},  # type: ignore[arg-type]
        )
        assert HOSTILE not in raw
        payload = json.loads(raw)
        assert payload["status"] == "error"
        assert "confidence" in payload["message"]
        assert "float_type" in payload["message"]

    def test_execute_mutation_invalid_idempotency_key_drops_the_value(self) -> None:
        with pytest.raises(McpError) as excinfo:
            execute_mutation(
                operation="link.create",
                args={},
                idempotency_key={"leak": HOSTILE},  # type: ignore[arg-type]
            )
        message = excinfo.value.error.message
        assert HOSTILE not in message
        assert "idempotency_key" in message
        assert "string_type" in message


# ---------------------------------------------------------------------------
# server.py's _result_message: gated by status, never unconditional
# ---------------------------------------------------------------------------


class TestResultMessage:
    @pytest.mark.parametrize("status", [CommandStatus.REJECTED, CommandStatus.FAILED])
    def test_failed_and_rejected_are_sanitized(self, status: CommandStatus) -> None:
        result = _result(status, f"constraint violated: api_key={HOSTILE}")
        message = _result_message(result)
        assert HOSTILE not in message
        assert message == SUPPRESSED_MARKER

    def test_duplicate_passes_through_unsanitized(self) -> None:
        # A control: DUPLICATE only restates the caller's own prior
        # request, so the gate is status-based, not content-based — even
        # a hostile-shaped message here is left whole.
        text = f"already recorded: api_key={HOSTILE}"
        result = _result(CommandStatus.DUPLICATE, text)
        assert _result_message(result) == text

    def test_success_passes_through_unsanitized(self) -> None:
        # execute_mutation calls _result_message unconditionally
        # (including on SUCCESS), so SUCCESS must stay whole too.
        text = f"created: api_key={HOSTILE}"
        result = _result(CommandStatus.SUCCESS, text)
        assert _result_message(result) == text

    def test_record_observation_wiring_drops_the_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end through the tool, not just the helper: a rejected
        executor result must not leak into ``record_observation``'s JSON
        reply."""
        hostile_result = _result(
            CommandStatus.REJECTED, f"policy violation: api_key={HOSTILE}"
        )
        monkeypatch.setattr(
            server_mod,
            "build_curate_executor",
            lambda *args, **kwargs: _StubExecutor(hostile_result),
        )
        raw = record_observation(
            subject_entity_id="e1",
            subject_entity_type="service",
            observer_agent_id="agent-1",
            content="observed something",
            confidence=0.5,
        )
        assert HOSTILE not in raw
        payload = json.loads(raw)
        assert payload["status"] == "rejected"
        assert payload["message"] == SUPPRESSED_MARKER


# ---------------------------------------------------------------------------
# supersession.py's _execute: same status gate
# ---------------------------------------------------------------------------


class TestSupersessionExecute:
    @pytest.mark.parametrize("status", [CommandStatus.REJECTED, CommandStatus.FAILED])
    def test_failed_and_rejected_are_sanitized(self, status: CommandStatus) -> None:
        result = _result(status, f"constraint violated: api_key={HOSTILE}")
        text = supersession_execute(_StubExecutor(result), _command())
        assert text is not None
        assert HOSTILE not in text
        assert SUPPRESSED_MARKER in text
        assert text.startswith(f"{status.value}: ")

    def test_duplicate_passes_through_unsanitized(self) -> None:
        text = f"already applied: api_key={HOSTILE}"
        result = _result(CommandStatus.DUPLICATE, text)
        assert supersession_execute(_StubExecutor(result), _command()) == (
            f"duplicate: {text}"
        )

    def test_success_returns_none(self) -> None:
        result = _result(CommandStatus.SUCCESS, "irrelevant")
        assert supersession_execute(_StubExecutor(result), _command()) is None


# ---------------------------------------------------------------------------
# knowledge_links.py's _describe_unsuccessful, pure function + wiring
# ---------------------------------------------------------------------------


class TestDescribeUnsuccessful:
    @pytest.mark.parametrize("status", [CommandStatus.REJECTED, CommandStatus.FAILED])
    def test_failed_and_rejected_are_sanitized(self, status: CommandStatus) -> None:
        result = _result(status, f"constraint violated: api_key={HOSTILE}")
        text = _describe_unsuccessful(result)
        assert HOSTILE not in text
        assert text == SUPPRESSED_MARKER

    def test_duplicate_passes_through_unsanitized(self) -> None:
        text = f"already linked: api_key={HOSTILE}"
        result = _result(CommandStatus.DUPLICATE, text)
        assert _describe_unsuccessful(result) == text


@pytest.fixture
def graph(temp_registry: StoreRegistry) -> GraphStore:
    store = temp_registry.knowledge.graph_store
    store.upsert_node("01NOTE", CONCEPT, {"name": "note"})
    store.upsert_node("tool:bash", SOFTWARE_APPLICATION, {"name": "Bash"})
    return store


class TestLinkKnowledgeNodeWiring:
    def test_relates_to_rejection_drops_the_value(self, graph: GraphStore) -> None:
        """End to end through ``link_knowledge_node``: a rejected
        LINK_CREATE must not leak its message into the response line."""
        hostile_result = _result(
            CommandStatus.REJECTED, f"policy violation: api_key={HOSTILE}"
        )
        lines = link_knowledge_node(
            _StubExecutor(hostile_result),
            graph,
            node_id="01NOTE",
            properties={},
            relates_to="Bash",
            edge_kind="entity_related_to",
        )
        joined = "\n".join(lines)
        assert HOSTILE not in joined
        assert SUPPRESSED_MARKER in joined
        assert "edge not created" in joined
