"""Every claim the shipped loop text makes about Trellis is checked against Trellis.

``trellis_cli/prompts/*.md`` is handed to models that have no skill and no
way to ask: a model called with an API key, a framework's system message, a
dispatched subagent. It names tools, parameters and the shape of a pack. A
name that drifted from the server would make the agent call a tool with an
argument the server rejects, or grade a pack with a field the learning join
never reads, and neither failure announces itself in the text.

So each claim here is checked against the thing it describes, not against a
second hand-written list:

* a keyword passed to a tool is a parameter of that tool's real signature;
* a ``*_ids`` field is a field of ``PackFeedbackRequest``, and the full
  system prompt names every one the request has;
* a step's ``result`` is an object, by the ``TraceStep`` schema;
* a rendered pack carries the ``pack_id`` line and the item heading the text
  says to look for;
* ``save_knowledge`` mints a new node on every call.

The checks are functions returning what is wrong, so each is also run
against a defective copy of the text to show it can fail.
"""

from __future__ import annotations

import inspect
import re
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp import server
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import TraceStep
from trellis_cli.prompts import PROMPT_VARIANTS, load_prompt
from trellis_wire.dtos import PackFeedbackRequest

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

#: The tools the text writes as ``name(...)`` calls. Each must be a real MCP
#: tool; the signature is read from the server module, not copied here.
TOOLS = (
    "get_context",
    "record_feedback",
    "save_experience",
    "save_knowledge",
    "search",
)

get_context = unwrap_tool(server.get_context)
save_knowledge = unwrap_tool(server.save_knowledge)

_CALL = re.compile(rf"(?<!\w)({'|'.join(TOOLS)})\(")
_KEYWORD = re.compile(r"(?<![\w=])(\w+)=")
_IDS = re.compile(r"(?<![\w])(\w+_ids)(?!\w)")


def _calls(text: str) -> list[tuple[str, str]]:
    """Return ``(tool, argument text)`` for each ``tool(...)`` call in ``text``."""
    found = []
    for match in _CALL.finditer(text):
        depth, end = 1, match.end()
        while depth and end < len(text):
            depth += {"(": 1, ")": -1}.get(text[end], 0)
            end += 1
        found.append((match.group(1), text[match.end() : end - 1]))
    return found


def unknown_keywords(text: str) -> list[str]:
    """Keywords the text passes to a tool that the tool does not take."""
    bad = []
    for tool, args in _calls(text):
        params = inspect.signature(unwrap_tool(getattr(server, tool))).parameters
        bad += [f"{tool}({kw}=)" for kw in _KEYWORD.findall(args) if kw not in params]
    return bad


def unknown_ids(text: str) -> list[str]:
    """``*_ids`` fields the text names that ``PackFeedbackRequest`` lacks."""
    return [n for n in _IDS.findall(text) if n not in PackFeedbackRequest.model_fields]


def unnamed_ids(text: str) -> list[str]:
    """``*_ids`` fields ``PackFeedbackRequest`` has that the text never names."""
    wire = {n for n in PackFeedbackRequest.model_fields if n.endswith("_ids")}
    return sorted(wire - set(_IDS.findall(text)))


def unknown_statuses(text: str) -> list[str]:
    """Outcome statuses the text lists that ``OutcomeStatus`` lacks."""
    listed = [s for m in re.findall(r"status: ([a-z|]+)", text) for s in m.split("|")]
    return [s for s in listed if s not in {o.value for o in OutcomeStatus}]


class TestEveryToolCallIsReal:
    def test_the_tools_the_text_calls_are_mcp_tools(self):
        for tool in TOOLS:
            assert callable(unwrap_tool(getattr(server, tool))), tool

    @pytest.mark.parametrize("variant", PROMPT_VARIANTS)
    def test_every_keyword_is_a_parameter_of_its_tool(self, variant):
        text = load_prompt(variant)
        assert _calls(text), "the scan found no call, so it proves nothing"
        assert unknown_keywords(text) == []

    def test_the_scan_sees_the_system_prompts_five_tools(self):
        assert {tool for tool, _ in _calls(load_prompt("system"))} == set(TOOLS)

    @pytest.mark.parametrize(
        ("old", "new", "expected"),
        [
            ("pack_id=<from step 1>", "pack=<from step 1>", "record_feedback(pack=)"),
            ("rating=<0.0-1.0>", "score=<0.0-1.0>", "record_feedback(score=)"),
            (
                "trace_json=<a JSON string>",
                "trace=<a JSON string>",
                "save_experience(trace=)",
            ),
            ("search(query=<name>)", "search(text=<name>)", "search(text=)"),
        ],
    )
    def test_a_misnamed_keyword_is_caught(self, old, new, expected):
        text = load_prompt("system")
        assert old in text
        assert unknown_keywords(text.replace(old, new)) == [expected]


class TestFeedbackFieldsAreTheWireModel:
    @pytest.mark.parametrize("variant", PROMPT_VARIANTS)
    def test_every_ids_field_named_is_a_field_of_the_request(self, variant):
        text = load_prompt(variant)
        assert _IDS.findall(text), "no *_ids field found, so the scan is vacuous"
        assert unknown_ids(text) == []

    def test_the_system_prompt_names_every_ids_field_the_request_has(self):
        assert unnamed_ids(load_prompt("system")) == []

    def test_a_renamed_field_is_caught(self):
        text = load_prompt("system")
        defective = text.replace("unhelpful_item_ids", "unhelpful_ids")
        assert defective != text
        assert set(unknown_ids(defective)) == {"unhelpful_ids"}

    def test_a_dropped_field_is_caught(self):
        text = load_prompt("system").replace("followed_advisory_ids=[...]", "")
        assert unnamed_ids(text) == ["followed_advisory_ids"]

    def test_pack_id_is_a_parameter_the_mcp_tool_takes(self):
        # REST carries the pack in the URL, so the request model has no
        # ``pack_id``; the tool the text calls does.
        params = inspect.signature(unwrap_tool(server.record_feedback)).parameters
        assert {"pack_id", "rating", "helpful_item_ids", "unhelpful_item_ids"} <= set(
            params
        )

    def test_every_ids_field_the_request_has_is_a_tool_parameter(self):
        # The id fields are read from the REST model, so the MCP tool must take
        # all of them. It also takes ``ignored_item_ids``, which REST would
        # reject as an extra field; the text omits it on purpose.
        wire = {n for n in PackFeedbackRequest.model_fields if n.endswith("_ids")}
        params = set(inspect.signature(unwrap_tool(server.record_feedback)).parameters)
        assert wire <= params
        assert {n for n in params if n.endswith("_ids")} - wire == {"ignored_item_ids"}

    def test_the_rating_range_the_text_gives_is_the_models(self):
        rating = PackFeedbackRequest.model_fields["rating"]
        bounds = {type(m).__name__: m for m in rating.metadata}
        assert bounds["Ge"].ge == 0.0
        assert bounds["Le"].le == 1.0
        assert "0.0-1.0" in load_prompt("system")


class TestTheTraceShape:
    def test_a_step_result_must_be_an_object(self):
        with pytest.raises(ValidationError):
            TraceStep(step_type="note", name="run", result="12 passed")  # type: ignore[arg-type]
        assert TraceStep(step_type="note", name="run", result={"summary": "ok"})

    def test_the_text_says_so(self):
        for variant in ("system", "claude-md"):
            assert "`result` is a JSON object" in load_prompt(variant)

    def test_a_step_error_must_be_a_string(self):
        with pytest.raises(ValidationError):
            TraceStep(step_type="note", name="run", error={"msg": "boom"})  # type: ignore[arg-type]
        for variant in ("system", "claude-md"):
            assert "`error` as a string" in load_prompt(variant)

    def test_the_source_the_text_names_is_real(self):
        assert '"agent"' in load_prompt("system")
        assert "agent" in {s.value for s in TraceSource}

    def test_the_outcome_statuses_listed_are_real(self):
        text = load_prompt("system")
        assert re.search(r"status: ([a-z|]+)", text), "no statuses listed"
        assert unknown_statuses(text) == []

    def test_an_invented_status_is_caught(self):
        text = load_prompt("system").replace("success|failure", "success|done")
        assert unknown_statuses(text) == ["done"]


class TestAPackReadsTheWayTheTextSaysItDoes:
    """The text tells the agent where to find the ids it must cite."""

    @staticmethod
    def _seed(registry: StoreRegistry) -> None:
        registry.knowledge.document_store.put(
            "doc-1",
            "restart the replica sidecar after promoting a failover. " * 4,
            {"title": "Runbook"},
        )

    def test_the_header_carries_the_pack_id_and_each_item_has_a_heading(
        self, temp_registry: StoreRegistry
    ):
        self._seed(temp_registry)
        pack = get_context("replica failover")

        # The text points at a "**pack_id:**" line, not at "the second line":
        # a capture-health banner can sit above the header.
        assert re.search(r"^\*\*pack_id:\*\* `[^`\s]+`$", pack, re.MULTILINE)
        assert re.search(r"^## \[\w+\] `[^`\s]+`$", pack, re.MULTILINE)
        system = load_prompt("system")
        assert '"**pack_id:**"' in system
        assert '"## [type] `item_id`"' in system

    def test_the_pack_offers_the_feedback_call_the_text_describes(
        self, temp_registry: StoreRegistry
    ):
        self._seed(temp_registry)
        pack = get_context("replica failover")
        assert "record_feedback(" in pack
        assert "helpful_item_ids" in pack

    def test_an_empty_pack_says_what_the_text_quotes(
        self, temp_registry: StoreRegistry
    ):
        # Whether an empty pack also carries a pack_id is not fixed, and the
        # text is written to hold either way (it makes grading conditional on
        # one having been shown). The phrase it quotes is fixed.
        pack = get_context("a topic nothing was ever stored about")
        assert "No context found" in pack
        assert '"No context found"' in load_prompt("system")


class TestSaveKnowledgeAlwaysCreates:
    def test_the_same_name_twice_is_two_nodes(self, temp_registry: StoreRegistry):
        first = save_knowledge("orders-api")
        second = save_knowledge("orders-api")
        ids = [
            re.search(r"Entity created: (\S+)", reply).group(1)  # type: ignore[union-attr]
            for reply in (first, second)
        ]
        assert ids[0] != ids[1]

    def test_the_system_prompt_warns_and_names_the_check(self):
        system = " ".join(load_prompt("system").split())
        assert "It always creates a new node" in system
        assert "search(query=<name>) first" in system
