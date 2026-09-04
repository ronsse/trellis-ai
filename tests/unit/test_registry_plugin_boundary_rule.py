"""Keep plugin-specific Bolt setup out of the core store registry."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_REGISTRY = _ROOT / "src" / "trellis" / "stores" / "registry.py"
_FORBIDDEN_MODULES = (
    "trellis.stores.neo4j",
    "trellis.stores.arcadedb",
    "trellis.stores.bolt_opencypher",
)
_FORBIDDEN_BACKENDS = frozenset({"neo4j", "arcadedb"})


def _forbidden_imports(tree: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    found: list[ast.Import | ast.ImportFrom] = []
    for node in ast.walk(tree):
        modules = (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
        if any(
            module == prefix or module.startswith(f"{prefix}.")
            for module in modules
            for prefix in _FORBIDDEN_MODULES
        ):
            found.append(node)
    return found


def _backend_name_comparisons(tree: ast.AST) -> list[ast.Compare]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(
            isinstance(value, ast.Constant) and value.value in _FORBIDDEN_BACKENDS
            for value in [node.left, *node.comparators]
        )
    ]


def test_rule_detects_known_plugin_couplings() -> None:
    tree = ast.parse(
        """
import trellis.stores.neo4j.graph
from trellis.stores.arcadedb.base import ensure_database
from trellis.stores.bolt_opencypher import base
if backend == "neo4j":
    pass
if "arcadedb" == backend:
    pass
"""
    )

    assert len(_forbidden_imports(tree)) == 3
    assert len(_backend_name_comparisons(tree)) == 2


def test_registry_has_no_bolt_plugin_coupling() -> None:
    tree = ast.parse(_REGISTRY.read_text())

    imports = _forbidden_imports(tree)
    comparisons = _backend_name_comparisons(tree)

    assert not imports, [
        f"{node.lineno}: Bolt backend import belongs on the backend class"
        for node in imports
    ]
    assert not comparisons, [
        f"{node.lineno}: Bolt backend branch belongs on the backend class"
        for node in comparisons
    ]
