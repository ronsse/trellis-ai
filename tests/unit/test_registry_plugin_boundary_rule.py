"""Keep plugin-specific Bolt setup out of the core store registry."""

from __future__ import annotations

import ast
from pathlib import Path

from tests.ast_rules import assert_hand_read_floor, name_of

_ROOT = Path(__file__).parents[2]
_REGISTRY = _ROOT / "src" / "trellis" / "stores" / "registry.py"
_FORBIDDEN_MODULES = (
    "trellis.stores.neo4j",
    "trellis.stores.arcadedb",
    "trellis.stores.bolt_opencypher",
)
_FORBIDDEN_BACKENDS = frozenset({"neo4j", "arcadedb"})
_DYNAMIC_IMPORT_NAMES = frozenset({"import_module", "__import__"})

# Hand-counted on the tree in test_rule_detects_known_plugin_couplings:
# 3 Import/ImportFrom, 1 import_module Call, 2 equality Compares, 1 In Compare.
_SYNTHETIC_COUPLING_FLOOR = 7


def _is_forbidden_module(module: str) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.")
        for prefix in _FORBIDDEN_MODULES
    )


def _string_call_args(call: ast.Call) -> list[str]:
    args = (*call.args, *(kw.value for kw in call.keywords))
    return [
        arg.value
        for arg in args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]


def _constant_values(node: ast.AST) -> list[object]:
    if isinstance(node, ast.Constant):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        values: list[object] = []
        for elt in node.elts:
            values.extend(_constant_values(elt))
        return values
    return []


def _forbidden_imports(tree: ast.AST) -> list[ast.AST]:
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        elif isinstance(node, ast.Call) and name_of(node.func) in _DYNAMIC_IMPORT_NAMES:
            modules = _string_call_args(node)
        else:
            continue
        if any(_is_forbidden_module(module) for module in modules):
            found.append(node)
    return found


def _is_forbidden_backend_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in _FORBIDDEN_BACKENDS


def _backend_name_comparisons(tree: ast.AST) -> list[ast.Compare]:
    found: list[ast.Compare] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if any(
            _is_forbidden_backend_constant(value)
            for value in [node.left, *node.comparators]
        ):
            found.append(node)
            continue
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, (ast.In, ast.NotIn)) and any(
                value in _FORBIDDEN_BACKENDS for value in _constant_values(comparator)
            ):
                found.append(node)
                break
    return found


def _plugin_couplings(tree: ast.AST) -> list[ast.AST]:
    return [*_forbidden_imports(tree), *_backend_name_comparisons(tree)]


def test_rule_detects_known_plugin_couplings() -> None:
    tree = ast.parse(
        """
import trellis.stores.neo4j.graph
from trellis.stores.arcadedb.base import ensure_database
from trellis.stores.bolt_opencypher import base
importlib.import_module("trellis.stores.neo4j.base")
if backend == "neo4j":
    pass
if "arcadedb" == backend:
    pass
if backend in ("arcadedb",):
    pass
"""
    )

    found = _plugin_couplings(tree)
    assert_hand_read_floor(
        len(found),
        _SYNTHETIC_COUPLING_FLOOR,
        subject="plugin couplings on the synthetic registry tree",
        hint=(
            "The tree carries 3 static imports, one import_module literal, "
            "two equality compares, and one In-tuple membership. Equality-only "
            "Compare reports 6; Import-plus-equality reports 5."
        ),
    )


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
