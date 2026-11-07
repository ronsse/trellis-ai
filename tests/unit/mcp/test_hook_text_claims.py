"""What the packaged hooks say and read is checked against the server they talk to.

``trellis_cli.hooks`` recognises Trellis tool calls in a transcript by name and
reads the pack id out of a reply by its header. ``trellis_wire.loop_text`` tells
an agent which call to make and which fields to fill. A name or field that
drifted from the server would make a hook blind (it would never see a retrieval)
or make an agent send an argument the server rejects, and neither failure
announces itself. Each claim is read from the thing it describes:

* a tool a hook treats as pack-returning is a real MCP tool, and the set the
  hook keeps is the set of tools the server tags as pack-returning;
* the set session capture keeps is the tagged set less the one tool it leaves out
  on purpose, and a tool whose reply prints a pack id is tagged, found by calling
  every registered tool rather than by reading the tags;
* a keyword in a call a hook prints is a parameter of that tool's signature;
* a field that counts as attribution is a field of ``PackFeedbackRequest``;
* the header regex finds the id in a pack the server actually rendered.
"""

from __future__ import annotations

import inspect
import json
import re
from typing import TYPE_CHECKING, Any

from fastmcp import Client

from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp import server
from trellis.mcp.app import PACK_TOOL_TAG
from trellis.mcp.auth import set_auth_enforced
from trellis.retrieve.builder_factory import GET_LESSONS_PACK_EVENT_ENV
from trellis.stores.base.event_log import EventType
from trellis_cli import hooks
from trellis_wire import loop_text
from trellis_wire.dtos import PackFeedbackRequest
from trellis_workers.session_capture import transcripts

if TYPE_CHECKING:
    import pytest

    from trellis.stores.registry import StoreRegistry

PACK_ID = "01HZX3K9Q7M2V8T5N4B6C0D1EF"

#: Tagged tools session capture does not recognise, each with the reason. Its
#: ``retrieval_results`` and ``pack_ids_unparsed`` feed ``analyze holdout``'s
#: funnel, so recognising a tool there is a holdout change, not a naming fix.
CAPTURE_OMITS = {
    "get_lessons": "its footer pack id is unread and its replies would move the "
    "holdout funnel's retrieval counters",
}

#: The label a reply prints before a pack id; broader than the header regex, so a
#: reply with the label in an unexpected place still counts as printing one.
PACK_LABEL = "**pack_id:**"

_CALL = re.compile(r"(?<!\w)(get_context|record_feedback|save_experience)\(([^)]*)\)")
_KEYWORD = re.compile(r"(?<![\w=])(\w+)=")


def _hook_texts() -> dict[str, str]:
    return {
        "HOOK_PREWORK_NUDGE": loop_text.HOOK_PREWORK_NUDGE,
        "HOOK_EDIT_DENY_REASON": loop_text.HOOK_EDIT_DENY_REASON,
        "HOOK_TRACE_ASK": loop_text.HOOK_TRACE_ASK,
        "ask": loop_text.hook_grading_ask(PACK_ID) or "",
        "ask+trace": loop_text.hook_grading_ask(PACK_ID, ask_trace=True) or "",
    }


async def _tagged_pack_tools() -> set[str]:
    """The tools the server itself marks as returning a pack id."""
    # ``list_tools`` is scope-filtered, so an anonymous listing is empty; drop
    # enforcement to enumerate. The suite's autouse fixture restores it.
    set_auth_enforced(enforced=False)
    try:
        listed = await server.mcp.list_tools()
    finally:
        set_auth_enforced(enforced=True)
    return {tool.name for tool in listed if PACK_TOOL_TAG in (tool.tags or ())}


def _dummy(schema: dict[str, Any]) -> Any:
    """A minimal value for one input-schema property: valid in type, empty in sense."""
    kinds = [
        option.get("type")
        for option in schema.get("anyOf", [schema])
        if option.get("type") not in (None, "null")
    ]
    kind = kinds[0] if kinds else "string"
    if kind == "array":
        return [_dummy(schema.get("items", {}))]
    return {
        "integer": 1,
        "number": 0.5,
        "boolean": False,
        "object": {"name": "x"},
    }.get(kind, "x")


async def _tools_printing_a_pack_id(registry: StoreRegistry) -> set[str]:
    """The tools whose reply carries a ``**pack_id:**`` line, found by calling each.

    Every registered tool is called through FastMCP's in-memory client with its
    required arguments filled by :func:`_dummy`, plus ``pack_id`` where the tool
    takes one (``get_items`` echoes it). Nothing is skipped: a call the server
    rejects returns an error that carries no pack id. Two pre-conditions are
    set so the one conditional path prints: ``get_lessons`` ends with a pack id
    only behind its knob and only when it served a lesson. A tool earlier in the
    roster may have written to the store by then, which only gives a later
    retrieval more to serve.
    """
    for index in range(2):
        registry.operational.event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="test",
            entity_id=f"prec-{index}",
            payload={
                "title": f"Lesson {index}",
                "description": "body",
                "domain": "ops",
            },
        )
    set_auth_enforced(enforced=False)
    printing: set[str] = set()
    async with Client(server.mcp) as client:
        for tool in await client.list_tools():
            properties = tool.inputSchema.get("properties", {})
            arguments = {
                name: _dummy(properties[name])
                for name in tool.inputSchema.get("required", [])
            }
            if "pack_id" in properties:
                arguments["pack_id"] = PACK_ID
            reply = await client.call_tool(tool.name, arguments, raise_on_error=False)
            if any(PACK_LABEL in getattr(block, "text", "") for block in reply.content):
                printing.add(tool.name)
    return printing


def _drift(tagged: set[str]) -> dict[str, set[str]]:
    """How the hooks' literal set and the server's tagged tools differ, each way."""
    return {
        "server_tags_it_the_hook_lacks": tagged - set(hooks.PACK_TOOLS),
        "hook_names_it_the_server_does_not_tag": set(hooks.PACK_TOOLS) - tagged,
    }


def _bad_keywords(text: str) -> list[str]:
    """Keywords in the calls ``text`` writes that the real tool does not take."""
    wrong = []
    for tool, arguments in _CALL.findall(text):
        real = set(inspect.signature(unwrap_tool(getattr(server, tool))).parameters)
        wrong += [
            f"{tool}({keyword}=)"
            for keyword in _KEYWORD.findall(arguments)
            if keyword not in real
        ]
    return wrong


class TestTheTextNamesRealCalls:
    def test_every_call_the_hooks_print_takes_the_keywords_it_names(self):
        for label, text in _hook_texts().items():
            assert _bad_keywords(text) == [], label

    def test_the_scan_sees_calls_in_the_texts(self):
        # Guards the check above against matching nothing.
        found = {
            tool for text in _hook_texts().values() for tool, _ in _CALL.findall(text)
        }
        assert found == {"get_context", "record_feedback", "save_experience"}

    def test_a_drifted_keyword_is_caught(self):
        drifted = loop_text.HOOK_PREWORK_NUDGE.replace("session_id=", "sess=")
        assert _bad_keywords(drifted) == ["get_context(sess=)"]

    def test_the_grading_ask_names_the_item_fields_the_request_has(self):
        ask = loop_text.hook_grading_ask(PACK_ID) or ""
        for name in ("helpful_item_ids", "unhelpful_item_ids"):
            assert name in ask
            assert name in PackFeedbackRequest.model_fields


class TestTheHooksReadRealNames:
    def test_every_pack_returning_tool_is_a_real_mcp_tool(self):
        for name in hooks.PACK_TOOLS:
            assert callable(unwrap_tool(getattr(server, name))), name

    def test_the_feedback_and_trace_tools_are_real(self):
        assert callable(unwrap_tool(getattr(server, hooks._FEEDBACK_TOOL)))
        assert callable(unwrap_tool(getattr(server, hooks._TRACE_TOOL)))

    def test_the_attribution_fields_are_fields_of_the_feedback_call(self):
        real = set(inspect.signature(unwrap_tool(server.record_feedback)).parameters)
        for key in hooks._ATTRIBUTION_KEYS:
            assert key in real, key
            assert key in PackFeedbackRequest.model_fields, key

    async def test_the_hooks_set_is_the_set_the_server_tags(self):
        # The hook keeps a literal copy (it must start without importing the
        # server), so the server is where the set is derived from.
        assert _drift(await _tagged_pack_tools()) == {
            "server_tags_it_the_hook_lacks": set(),
            "hook_names_it_the_server_does_not_tag": set(),
        }

    async def test_the_derivation_finds_the_tools_it_should(self):
        # Guards the comparison above against deriving nothing.
        assert await _tagged_pack_tools() >= {
            "get_context",
            "search",
            "get_lessons",
            "get_items",
        }

    async def test_a_new_pack_returning_tool_fails_the_comparison(self):
        def get_fake_pack(intent: str) -> str:
            return f"# Context for: {intent}\n**pack_id:** `{PACK_ID}`"

        server.mcp.tool(name="get_fake_pack", tags={PACK_TOOL_TAG})(get_fake_pack)
        try:
            drift = _drift(await _tagged_pack_tools())
        finally:
            server.mcp.local_provider.remove_tool("get_fake_pack")
        assert drift == {
            "server_tags_it_the_hook_lacks": {"get_fake_pack"},
            "hook_names_it_the_server_does_not_tag": set(),
        }
        # Removed again, the server and the hook agree.
        assert not any(_drift(await _tagged_pack_tools()).values())

    async def test_a_name_the_hook_has_and_the_server_does_not_tag_fails_too(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(hooks, "PACK_TOOLS", hooks.PACK_TOOLS | {"get_graph"})
        assert _drift(await _tagged_pack_tools()) == {
            "server_tags_it_the_hook_lacks": set(),
            "hook_names_it_the_server_does_not_tag": {"get_graph"},
        }


class TestTheCaptureSetIsPinnedToo:
    async def test_session_capture_recognises_the_tagged_tools_less_its_omissions(self):
        tagged = await _tagged_pack_tools()
        assert set(transcripts._PACK_TOOLS) == tagged - set(CAPTURE_OMITS)

    async def test_every_omission_is_a_tagged_tool_it_really_leaves_out(self):
        # A stale entry (renamed, untagged or since recognised) must go.
        tagged = await _tagged_pack_tools()
        assert set(CAPTURE_OMITS) <= tagged
        assert not set(CAPTURE_OMITS) & set(transcripts._PACK_TOOLS)

    async def test_a_new_tagged_tool_fails_the_capture_comparison(self):
        def get_fake_pack(intent: str) -> str:
            return f"# Context for: {intent}\n**pack_id:** `{PACK_ID}`"

        server.mcp.tool(name="get_fake_pack", tags={PACK_TOOL_TAG})(get_fake_pack)
        try:
            tagged = await _tagged_pack_tools()
        finally:
            server.mcp.local_provider.remove_tool("get_fake_pack")
        assert set(transcripts._PACK_TOOLS) != tagged - set(CAPTURE_OMITS)


class TestEveryReplyThatPrintsAPackIsTagged:
    """The tag is the claim; this reads the reply, which is the fact."""

    async def test_a_tool_whose_reply_prints_a_pack_id_is_tagged(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(GET_LESSONS_PACK_EVENT_ENV, "1")
        printing = await _tools_printing_a_pack_id(temp_registry)
        assert printing - await _tagged_pack_tools() == set()

    async def test_the_reply_check_reaches_every_tagged_tool(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ):
        # Guards the check above against calling nothing: each tool the server
        # tags must print a pack id under the arguments the check builds, or the
        # check is blind to it and its arguments need extending.
        monkeypatch.setenv(GET_LESSONS_PACK_EVENT_ENV, "1")
        printing = await _tools_printing_a_pack_id(temp_registry)
        assert await _tagged_pack_tools() - printing == set()

    async def test_an_untagged_tool_that_prints_a_pack_id_is_caught(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ):
        def get_fake_pack(intent: str) -> str:
            return f"# Context for: {intent}\n**pack_id:** `{PACK_ID}`"

        monkeypatch.setenv(GET_LESSONS_PACK_EVENT_ENV, "1")
        server.mcp.tool(name="get_fake_pack")(get_fake_pack)
        try:
            printing = await _tools_printing_a_pack_id(temp_registry)
            tagged = await _tagged_pack_tools()
        finally:
            server.mcp.local_provider.remove_tool("get_fake_pack")
        assert printing - tagged == {"get_fake_pack"}


class TestTheHeaderRegexFindsARenderedPack:
    def test_the_pack_id_of_a_real_reply_is_found(self, temp_registry: StoreRegistry):
        temp_registry.knowledge.document_store.put(
            "doc-1",
            "restart the replica sidecar after promoting a failover. " * 4,
            {"title": "Runbook"},
        )
        pack = unwrap_tool(server.get_context)("replica failover")
        found = hooks._header_pack_id(pack)
        assert found is not None
        assert f"**pack_id:** `{found}`" in pack

    def test_the_id_is_found_when_the_reply_is_wrapped_as_a_transcript_stores_it(
        self, temp_registry: StoreRegistry
    ):
        temp_registry.knowledge.document_store.put(
            "doc-1",
            "restart the replica sidecar after promoting a failover. " * 4,
            {"title": "Runbook"},
        )
        pack = unwrap_tool(server.get_context)("replica failover")
        wrapped = json.dumps({"result": pack})
        assert hooks._header_pack_id(wrapped) == hooks._header_pack_id(pack)
        blocks = [{"type": "text", "text": wrapped}]
        assert hooks._header_pack_id(blocks) == hooks._header_pack_id(pack)

    def test_get_items_echoes_the_pack_id_it_was_given_where_the_hook_reads_it(
        self, temp_registry: StoreRegistry
    ):
        temp_registry.knowledge.document_store.put(
            "doc-1", "restart the replica sidecar. " * 4, {"title": "Runbook"}
        )
        get_items: Any = unwrap_tool(server.get_items)
        reply = get_items(item_ids=["doc-1"], pack_id=PACK_ID)
        assert hooks._header_pack_id(reply) == PACK_ID
        # Without a pack id it prints none, so the hook has nothing to grade.
        assert hooks._header_pack_id(get_items(item_ids=["doc-1"])) is None

    def test_get_lessons_prints_its_footer_only_behind_the_knob(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ):
        for index, name in enumerate(("First lesson", "Second lesson")):
            temp_registry.operational.event_log.emit(
                EventType.PRECEDENT_PROMOTED,
                source="test",
                entity_id=f"prec-{index}",
                payload={"title": name, "description": f"{name} body", "domain": "ops"},
            )
        get_lessons: Any = unwrap_tool(server.get_lessons)
        monkeypatch.delenv(GET_LESSONS_PACK_EVENT_ENV, raising=False)
        assert hooks._header_pack_id(get_lessons()) is None

        monkeypatch.setenv(GET_LESSONS_PACK_EVENT_ENV, "1")
        reply = get_lessons()
        (event,) = temp_registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        assert hooks._header_pack_id(reply) == event.entity_id
