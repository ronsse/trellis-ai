"""What the packaged hooks say and read is checked against the server they talk to.

``trellis_cli.hooks`` recognises Trellis tool calls in a transcript by name and
reads the pack id out of a reply by its header. ``trellis_wire.loop_text`` tells
an agent which call to make and which fields to fill. A name or field that
drifted from the server would make a hook blind (it would never see a retrieval)
or make an agent send an argument the server rejects, and neither failure
announces itself. Each claim is read from the thing it describes:

* a tool a hook treats as pack-returning is a real MCP tool;
* a keyword in a call a hook prints is a parameter of that tool's signature;
* a field that counts as attribution is a field of ``PackFeedbackRequest``;
* the header regex finds the id in a pack the server actually rendered.
"""

from __future__ import annotations

import inspect
import json
import re
from typing import TYPE_CHECKING

from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp import server
from trellis_cli import hooks
from trellis_wire import loop_text
from trellis_wire.dtos import PackFeedbackRequest

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

PACK_ID = "01HZX3K9Q7M2V8T5N4B6C0D1EF"

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

    def test_a_pack_returning_tool_is_not_missing_from_the_hook(self):
        # Every tool whose reply carries a pack header is one the hooks watch.
        header_tools = {
            "get_context",
            "search",
            "get_objective_context",
            "get_task_context",
            "get_sectioned_context",
        }
        assert header_tools == set(hooks.PACK_TOOLS)


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
