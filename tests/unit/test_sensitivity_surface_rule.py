"""Every MCP tool is behind the sensitivity gate or free of document content (#194).

A new tool that reads documents must not slip in unlabelled-aware. The roster
below names each tool and the claim made for it; the rule checks the claim
against the call graph inside ``trellis/mcp/server.py``:

* ``gated``: the tool's transitive callees (within the module) include a call
  to ``current_clearance``, which is the only source of a read clearance.
* ``no-document-content``: nothing the tool reaches inside the module touches
  ``document_store``.

``PackBuilder.build`` / ``build_sectioned`` take ``clearance`` with no default
(pinned below), so the pack surfaces cannot forget it either.

Not covered, by design: REST routes are checked by ``mypy`` (the same required
keyword) and by ``tests/unit/api/test_sensitivity_routes.py``.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import ClassVar

from tests.ast_rules import assert_hand_read_floor
from trellis.retrieve.pack_builder import PackBuilder

SERVER = Path(__file__).resolve().parents[2] / "src" / "trellis" / "mcp" / "server.py"

GATED = "gated"
FREE = "no-document-content"

ROSTER: dict[str, str] = {
    "get_context": GATED,
    "get_objective_context": GATED,
    "get_task_context": GATED,
    "get_sectioned_context": GATED,
    "search": GATED,
    "get_items": GATED,
    "get_file_context": GATED,
    "save_memory": GATED,
    "save_knowledge": GATED,
    "record_feedback": GATED,
    "get_graph": GATED,
    "get_lessons": GATED,
    "query_observations": GATED,
    "record_observation": FREE,
    "save_experience": FREE,
    "execute_mutation": GATED,
}


def _tools_and_functions(source: str) -> tuple[set[str], dict[str, ast.AST]]:
    tree = ast.parse(source)
    functions = {
        n.name: n
        for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    tools = {
        name
        for name, fn in functions.items()
        if any(
            "mcp.tool" in ast.unparse(dec) for dec in getattr(fn, "decorator_list", [])
        )
    }
    return tools, functions


def _reach(name: str, functions: dict[str, ast.AST]) -> set[str]:
    seen: set[str] = set()
    todo = [name]
    while todo:
        current = todo.pop()
        if current in seen or current not in functions:
            continue
        seen.add(current)
        todo.extend(
            node.id
            for node in ast.walk(functions[current])
            if isinstance(node, ast.Name) and node.id in functions
        )
    return seen


def _calls(names: set[str], functions: dict[str, ast.AST], callee: str) -> bool:
    return any(
        isinstance(n, ast.Call) and ast.unparse(n.func) == callee
        for name in names
        for n in ast.walk(functions[name])
    )


def _touches_attr(names: set[str], functions: dict[str, ast.AST], attr: str) -> bool:
    return any(
        isinstance(n, ast.Attribute) and n.attr == attr
        for name in names
        for n in ast.walk(functions[name])
    )


def violations(source: str, roster: dict[str, str]) -> list[str]:
    tools, functions = _tools_and_functions(source)
    found = [f"{t}: not in the roster" for t in sorted(tools - roster.keys())]
    found += [f"{t}: rostered but not a tool" for t in sorted(roster.keys() - tools)]
    for tool in sorted(tools & roster.keys()):
        reached = _reach(tool, functions)
        if roster[tool] == GATED and not _calls(
            reached, functions, "current_clearance"
        ):
            found.append(f"{tool}: claims gated, never reaches current_clearance()")
        if roster[tool] == FREE and _touches_attr(reached, functions, "document_store"):
            found.append(f"{tool}: claims no-document-content, reaches document_store")
    return found


def test_every_mcp_tool_is_rostered_and_its_claim_holds() -> None:
    source = SERVER.read_text()
    tools, _ = _tools_and_functions(source)
    assert_hand_read_floor(
        len(tools),
        16,
        subject="MCP tools in server.py",
        hint="the tool list a person read off the file",
    )
    assert violations(source, ROSTER) == []


class TestTheRuleCatchesADefectiveSubject:
    """Proven against deliberately defective sources, not just the real one."""

    CLEAN = """
@mcp.tool()
def reads():
    return current_clearance()
@mcp.tool()
def free():
    return 1
"""
    ROSTER: ClassVar[dict[str, str]] = {"reads": GATED, "free": FREE}

    def test_the_clean_subject_passes(self) -> None:
        assert violations(self.CLEAN, self.ROSTER) == []

    def test_a_new_unrostered_tool_fails(self) -> None:
        source = self.CLEAN + "\n@mcp.tool()\ndef sneaky():\n    return 1\n"
        assert violations(source, self.ROSTER) == ["sneaky: not in the roster"]

    def test_a_gated_claim_without_the_gate_fails(self) -> None:
        source = self.CLEAN.replace("return current_clearance()", "return 2")
        assert any("never reaches" in v for v in violations(source, self.ROSTER))

    def test_the_gate_reached_through_a_helper_counts(self) -> None:
        source = (
            self.CLEAN.replace("return current_clearance()", "return helper()")
            + "\ndef helper():\n    return current_clearance()\n"
        )
        assert violations(source, self.ROSTER) == []

    def test_a_free_claim_that_reads_documents_fails(self) -> None:
        source = self.CLEAN.replace(
            "return 1", "return registry.knowledge.document_store.get('x')"
        )
        assert any(
            "reaches document_store" in v for v in violations(source, self.ROSTER)
        )


def test_pack_builders_require_a_clearance_with_no_default() -> None:
    for method in (PackBuilder.build, PackBuilder.build_sectioned):
        param = inspect.signature(method).parameters["clearance"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# What a ``public`` caller (an ``external`` key: scopes ``read,external``) meets
# ---------------------------------------------------------------------------
#
# ``external`` satisfies ``read`` and nothing above it. Every tool it can call
# is either refused outright (``_refuse_public_caller(<tool>)`` in the tool's own
# body: it reads content that cannot carry a label) or listed as answering, which
# the rule holds to ``gated`` above and, when the tool can reach the graph or a
# trace by any route, to a check of the caller's clearance on that route
# (``is_public_only``). A tool above ``read`` needs neither. A new read tool that
# is none of these fails.

ANSWERS = "answers"
REFUSED = "refused"
SCOPE_CLOSED = "scope-closed"

EXTERNAL_STANCE: dict[str, str] = {
    "get_context": ANSWERS,
    "get_objective_context": ANSWERS,
    "get_task_context": ANSWERS,
    "get_sectioned_context": ANSWERS,
    "search": ANSWERS,
    "get_items": ANSWERS,
    "get_file_context": REFUSED,
    "get_graph": REFUSED,
    "get_lessons": REFUSED,
    "query_observations": REFUSED,
    "save_memory": SCOPE_CLOSED,
    "save_knowledge": SCOPE_CLOSED,
    "record_feedback": SCOPE_CLOSED,
    "record_observation": SCOPE_CLOSED,
    "save_experience": SCOPE_CLOSED,
    "execute_mutation": SCOPE_CLOSED,
}

_UNLABELLED_STORES = ("graph_store", "trace_store")


def _tool_scope(fn: ast.AST) -> str | None:
    for dec in getattr(fn, "decorator_list", []):
        text = ast.unparse(dec)
        if "mcp.tool" in text and "trellis_scope(" in text:
            return text.split("trellis_scope(", 1)[1].split(")", 1)[0]
    return None


def _refuses_in_own_body(name: str, fn: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call)
        and ast.unparse(n.func) == "_refuse_public_caller"
        and len(n.args) == 1
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == name
        for n in ast.walk(fn)
    )


def external_violations(
    source: str, stance: dict[str, str], roster: dict[str, str]
) -> list[str]:
    tools, functions = _tools_and_functions(source)
    found = [f"{t}: no external stance" for t in sorted(tools - stance.keys())]
    found += [
        f"{t}: external stance but not a tool" for t in sorted(stance.keys() - tools)
    ]
    for tool in sorted(tools & stance.keys()):
        scope = _tool_scope(functions[tool])
        claim = stance[tool]
        refuses = _refuses_in_own_body(tool, functions[tool])
        if claim == SCOPE_CLOSED:
            if scope in (None, "SCOPE_READ"):
                found.append(f"{tool}: claims scope-closed but is reachable at read")
            continue
        if scope != "SCOPE_READ":
            found.append(f"{tool}: claims {claim} but its scope is {scope}")
        if claim == REFUSED and not refuses:
            found.append(
                f"{tool}: claims refused, never calls _refuse_public_caller({tool!r})"
            )
        if claim == ANSWERS:
            reached = _reach(tool, functions)
            if refuses:
                found.append(f"{tool}: claims answers but refuses")
            if roster.get(tool) != GATED:
                found.append(f"{tool}: claims answers but is not rostered gated")
            if any(
                _touches_attr(reached, functions, s) for s in _UNLABELLED_STORES
            ) and not _calls(reached, functions, "is_public_only"):
                found.append(
                    f"{tool}: claims answers, reaches the graph or traces, "
                    "never checks is_public_only()"
                )
    return found


def test_no_mcp_tool_defaults_open_to_an_external_key() -> None:
    source = SERVER.read_text()
    # Hand read: of the 16 tools, 6 answer, 4 are refused and 6 need a scope above read.
    assert_hand_read_floor(
        sum(1 for v in EXTERNAL_STANCE.values() if v == REFUSED),
        4,
        subject="MCP tools refused to an external key",
        hint="a refusal was dropped from the stance roster",
    )
    assert_hand_read_floor(
        len(EXTERNAL_STANCE),
        16,
        subject="MCP tools with an external stance",
        hint="the tool list a person read off the file",
    )
    assert external_violations(source, EXTERNAL_STANCE, ROSTER) == []


class TestTheExternalRuleCatchesADefectiveSubject:
    CLEAN = """
@mcp.tool(auth=trellis_scope(SCOPE_READ))
def answers():
    return current_clearance()
@mcp.tool(auth=trellis_scope(SCOPE_READ))
def refused():
    _refuse_public_caller("refused")
    return current_clearance()
@mcp.tool(auth=trellis_scope(SCOPE_MUTATE))
def writes():
    return 1
"""
    STANCE: ClassVar[dict[str, str]] = {
        "answers": ANSWERS,
        "refused": REFUSED,
        "writes": SCOPE_CLOSED,
    }
    ROSTER: ClassVar[dict[str, str]] = {
        "answers": GATED,
        "refused": GATED,
        "writes": FREE,
    }

    def _run(self, source: str) -> list[str]:
        return external_violations(source, self.STANCE, self.ROSTER)

    def test_the_clean_subject_passes(self) -> None:
        assert self._run(self.CLEAN) == []

    def test_a_new_read_tool_without_a_stance_fails(self) -> None:
        source = (
            self.CLEAN
            + "\n@mcp.tool(auth=trellis_scope(SCOPE_READ))\n"
            + "def sneaky():\n    return 1\n"
        )
        assert self._run(source) == ["sneaky: no external stance"]

    def test_a_refusal_removed_from_a_refused_tool_fails(self) -> None:
        source = self.CLEAN.replace('    _refuse_public_caller("refused")\n', "")
        assert any("claims refused" in v for v in self._run(source))

    def test_a_refusal_naming_another_tool_does_not_count(self) -> None:
        source = self.CLEAN.replace(
            '_refuse_public_caller("refused")', '_refuse_public_caller("other")'
        )
        assert any("claims refused" in v for v in self._run(source))

    def test_an_answering_tool_that_reaches_the_graph_unchecked_fails(self) -> None:
        source = self.CLEAN.replace(
            "def answers():\n    return current_clearance()",
            "def answers():\n    current_clearance()\n"
            "    return registry.knowledge.graph_store.get_node('x')",
        )
        assert any("never checks is_public_only" in v for v in self._run(source))

    def test_the_check_reached_through_a_helper_counts(self) -> None:
        source = self.CLEAN.replace(
            "def answers():\n    return current_clearance()",
            "def answers():\n    current_clearance()\n    return helper()",
        ) + (
            "\ndef helper():\n"
            "    if is_public_only(current_clearance()):\n        return None\n"
            "    return registry.knowledge.trace_store.get('x')\n"
        )
        assert self._run(source) == []

    def test_a_write_tool_claimed_scope_closed_but_reachable_at_read_fails(
        self,
    ) -> None:
        source = self.CLEAN.replace(
            "trellis_scope(SCOPE_MUTATE)", "trellis_scope(SCOPE_READ)"
        )
        assert any("reachable at read" in v for v in self._run(source))

    def test_an_answering_tool_must_be_rostered_gated(self) -> None:
        found = external_violations(
            self.CLEAN, self.STANCE, {**self.ROSTER, "answers": FREE}
        )
        assert any("not rostered gated" in v for v in found)

    def test_a_stale_stance_fails(self) -> None:
        found = external_violations(
            self.CLEAN, {**self.STANCE, "gone": ANSWERS}, self.ROSTER
        )
        assert "gone: external stance but not a tool" in found
