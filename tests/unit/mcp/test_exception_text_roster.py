"""Ratchet for raw exception text reaching an MCP caller (#748 follow-up 1).

#748 fixed ``execute_mutation``'s own ``executor.execute`` wrapper.
``_exception_detail`` (``src/trellis/mcp/server.py``) carries the same fix to
every other caller-facing site in this module where a store or driver
exception could reach the text (``gates/748.md`` follow-up 1;
``tests/unit/mcp/test_store_error_sanitization.py`` pins two of them). What
remains is caller-input exceptions — the caller's own JSON/pydantic payload,
which they need back to fix their call — and one site already rendered safe
by ``MutationExecutor``'s own design. The roster below is that exhaustive,
hand-read exemption list, not a todo list: a new site fails the build, and a
fixed site must shrink this roster and its count.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import trellis.mcp.server as server_mod
from tests.ast_rules import assert_hand_read_floor, name_of

#: Calls whose arguments never reach the caller: structlog methods (full
#: driver text in a log line is the house pattern) and this module's own
#: sanitizing helpers. A raw ``str(exc)``/``{exc}`` nested inside one of
#: these is not a leak, so the scan does not descend into it.
_SAFE_CALL_NAMES = frozenset(
    {
        "debug",
        "info",
        "warning",
        "error",
        "exception",
        "critical",
        "_exception_detail",
        "sanitize_error_message",
        "sanitized_error_payload",
    }
)

#: Enclosing top-level function -> why its raw exception text stays.
#: Keyed by function name, not line number, so moving the function does
#: not desync the roster.
EXCEPTION_TEXT_EXEMPTIONS: dict[str, str] = {
    "save_experience": (
        "a pydantic ValidationError on the caller's own trace JSON; the "
        "caller needs the detail to fix their payload."
    ),
    "_resolve_evidence_pointer": (
        "MutationError.message is already rendered safe by "
        "MutationExecutor's own design, never raw driver text (two sites: "
        "the raised message and its data['message'] echo)."
    ),
    "record_observation": (
        "a pydantic ValidationError on the caller's own Observation "
        "fields; the caller needs the detail to fix their call."
    ),
    "execute_mutation": (
        "a pydantic ValidationError on the caller's own Command op/args, "
        "the same shape #748 itself left alone in this function."
    ),
}

# Hand count at worktree HEAD (trellis-ai#748 follow-up 1): 1 in
# save_experience, 2 in _resolve_evidence_pointer, 1 in record_observation,
# 1 in execute_mutation. Re-grep '{exc}\|str(exc)' in src/trellis/mcp/server.py
# and subtract the helper's own internal use and the one log-only site
# (_build_llm_client, excluded structurally above) to reconcile.
_HAND_READ_SITE_COUNT = 5

_SERVER_PATH = Path(inspect.getsourcefile(server_mod) or "")


def _raw_exception_uses(node: ast.AST, exc_name: str) -> list[ast.AST]:
    """Every ``str(<exc_name>)`` call or ``{<exc_name>}`` f-string under
    *node*, not descending into a :data:`_SAFE_CALL_NAMES` call."""
    found: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Call) and name_of(child.func) in _SAFE_CALL_NAMES:
            continue
        if (
            isinstance(child, ast.Call)
            and name_of(child.func) == "str"
            and len(child.args) == 1
            and isinstance(child.args[0], ast.Name)
            and child.args[0].id == exc_name
        ):
            found.append(child)
        elif isinstance(child, ast.JoinedStr):
            found.extend(
                value
                for value in child.values
                if isinstance(value, ast.FormattedValue)
                and isinstance(value.value, ast.Name)
                and value.value.id == exc_name
            )
        found.extend(_raw_exception_uses(child, exc_name))
    return found


def _scan(tree: ast.Module) -> dict[str, list[ast.AST]]:
    """Raw exception-text sites in *tree*, grouped by enclosing top-level
    function name."""
    by_function: dict[str, list[ast.AST]] = {}
    for top in tree.body:
        if not isinstance(top, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        hits: list[ast.AST] = []
        for node in ast.walk(top):
            if isinstance(node, ast.ExceptHandler) and node.name:
                for stmt in node.body:
                    hits.extend(_raw_exception_uses(stmt, node.name))
        if hits:
            by_function[top.name] = hits
    return by_function


def _server_scan() -> dict[str, list[ast.AST]]:
    return _scan(ast.parse(_SERVER_PATH.read_text(encoding="utf-8")))


def test_every_raw_exception_text_site_is_a_declared_exemption() -> None:
    found = _server_scan()
    declared = set(EXCEPTION_TEXT_EXEMPTIONS)
    assert set(found) == declared, (
        "raw exception text sites in src/trellis/mcp/server.py differ from "
        f"the roster: unrostered={sorted(set(found) - declared)}; "
        f"stale={sorted(declared - set(found))}"
    )
    thin = [
        name
        for name, reason in EXCEPTION_TEXT_EXEMPTIONS.items()
        if len(reason.split()) < 4
    ]
    assert not thin, f"exemptions need prose reasons: {thin}"
    total = sum(len(hits) for hits in found.values())
    assert total == _HAND_READ_SITE_COUNT, (
        f"found {total} raw exception text sites, expected the hand-read "
        f"{_HAND_READ_SITE_COUNT}. A new site must not inherit an existing "
        "function-level exemption; a fixed site must shrink this count."
    )


def test_hand_read_count_has_not_shrunk_silently() -> None:
    total = sum(len(hits) for hits in _server_scan().values())
    assert_hand_read_floor(
        total,
        _HAND_READ_SITE_COUNT,
        subject="raw exception text reaching an MCP caller",
        hint="Re-run the grep from gates/748.md follow-up 1 and reconcile.",
    )


def test_scan_catches_a_newly_added_leak() -> None:
    """Non-vacuousness: a deliberately defective function the roster has
    never seen is still found, proving the scan would fail a real build."""
    defective = ast.parse(
        "def _brand_new_tool():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as exc:\n"
        "        return f'failed: {exc}'\n"
    )
    found = _scan(defective)
    assert "_brand_new_tool" in found
