"""Guard: ``scrub_jsonb_unsafe`` has exactly one implementation.

``mutate/executor.py`` imports ``scrub_jsonb_unsafe`` from
``trellis.core.text_safety`` under the alias ``_scrub_jsonb_unsafe`` instead
of defining its own copy (see the comment at the top of that import block).
A second private copy reappearing there would be byte-for-byte behaviorally
identical to the canonical one -- no hazard test would ever catch it, since
both would scrub the same way -- so the only thing that can catch *this*
regression is a structural check: no function definition anywhere in the
module may be named ``scrub_jsonb_unsafe`` or ``_scrub_jsonb_unsafe``, and
the import from ``trellis.core.text_safety`` must still be there.

This intentionally does not build on ``tests/ast_rules.py``'s shared
mechanical layer. That module's resolvers (``construction_names`` and
friends) solve a different problem -- *which call sites, spelled several
ways, invoke a policed function* -- for repo-wide, floor-gated scans. This
rule asks a narrower, unambiguous question about a single, named file: does
a ``FunctionDef``/``AsyncFunctionDef`` with this exact name exist in it. No
alias, rebinding or subclass shape makes that ambiguous, so there is no
resolution step to share, and no population floor to argue about.
"""

from __future__ import annotations

import ast
from pathlib import Path

import trellis.mutate.executor as executor_module

_SCRUB_NAMES = frozenset({"scrub_jsonb_unsafe", "_scrub_jsonb_unsafe"})


def _executor_source_tree() -> ast.Module:
    source_path = Path(executor_module.__file__)
    return ast.parse(source_path.read_text(encoding="utf-8"))


def test_executor_does_not_redefine_the_scrub_helper() -> None:
    tree = _executor_source_tree()
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in _SCRUB_NAMES
    }
    assert not defined, (
        f"executor.py defines {sorted(defined)} locally -- a second copy "
        f"of the scrub helper. It must stay a single import of "
        f"trellis.core.text_safety.scrub_jsonb_unsafe so exactly one "
        f"implementation exists."
    )


def test_executor_imports_the_canonical_scrub_helper() -> None:
    tree = _executor_source_tree()
    matching_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "trellis.core.text_safety"
        and any(alias.name == "scrub_jsonb_unsafe" for alias in node.names)
    ]
    assert matching_imports, (
        "executor.py must import scrub_jsonb_unsafe from "
        "trellis.core.text_safety rather than defining its own copy."
    )
