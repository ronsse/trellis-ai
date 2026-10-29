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
    "get_graph": FREE,
    "get_lessons": FREE,
    "query_observations": FREE,
    "record_observation": FREE,
    "save_experience": FREE,
    "execute_mutation": FREE,
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
