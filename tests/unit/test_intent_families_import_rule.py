"""Nothing that serves or learns may import the intent-family relabel module.

``trellis.learning.intent_families`` measures a candidate learning key offline.
The live key is ``normalize_intent_family`` and changing it is a post-readout
decision, so the scoring, observation, retrieval, tagging, MCP and promotion
code must not read the candidate set, not even by accident of a helpful import.
Only the module's own CLI command and REST route may; tests are outside ``src``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.ast_rules import assert_hand_read_floor, iter_modules, name_of
from trellis.core.path_presence import path_is_present

_ROOT = Path(__file__).parents[2]
_SRC = _ROOT / "src"
_TARGET = "trellis.learning.intent_families"
_PARENT, _LEAF = _TARGET.rsplit(".", 1)
_DYNAMIC_IMPORT_NAMES = frozenset({"import_module", "__import__"})

#: The module itself, its CLI command and its REST route.
_ALLOWED = frozenset(
    {
        "trellis/learning/intent_families.py",
        "trellis_cli/analyze_intent_families.py",
        "trellis_api/routes/analyze.py",
    }
)

#: Surfaces the constraint names; each must still exist, so a rename cannot
#: empty the pin while it stays green.
_NAMED_SURFACES = (
    "trellis/learning/scoring.py",
    "trellis/learning/pack_observations.py",
    "trellis/retrieve",
    "trellis/classify",
    "trellis/mcp",
)

# Hand-read on the tree in test_detector_sees_every_import_shape: 2 static
# imports, 2 from-imports of the module or its parent, 1 relative from-import,
# 1 relative parent import, 2 dynamic imports.
_SYNTHETIC_FLOOR = 8

# Hand-read on the tree: the CLI module and the REST route.
_REAL_IMPORTER_FLOOR = 2


def _package_of(relative: Path) -> list[str]:
    parts = list(relative.with_suffix("").parts)
    return parts[:-1]


def _resolved_module(node: ast.ImportFrom, package: list[str]) -> str:
    if node.level == 0:
        return node.module or ""
    base = package[: len(package) - (node.level - 1)]
    tail = node.module.split(".") if node.module else []
    return ".".join([*base, *tail])


def _string_args(call: ast.Call) -> list[str]:
    args = (*call.args, *(kw.value for kw in call.keywords))
    return [
        arg.value
        for arg in args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]


def _is_target_import(node: ast.AST, package: list[str]) -> bool:
    if isinstance(node, ast.Import):
        return any(
            alias.name == _TARGET or alias.name.startswith(f"{_TARGET}.")
            for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        module = _resolved_module(node, package)
        if module == _TARGET or module.startswith(f"{_TARGET}."):
            return True
        return module == _PARENT and any(alias.name == _LEAF for alias in node.names)
    if isinstance(node, ast.Call) and name_of(node.func) in _DYNAMIC_IMPORT_NAMES:
        return _TARGET in _string_args(node)
    return False


def _imports_of_target(tree: ast.AST, package: list[str]) -> list[ast.AST]:
    return [node for node in ast.walk(tree) if _is_target_import(node, package)]


def test_detector_sees_every_import_shape() -> None:
    tree = ast.parse(
        """
import trellis.learning.intent_families
import trellis.learning.intent_families as fams
from trellis.learning.intent_families import classify_intent_family
from trellis.learning import intent_families
from .intent_families import RuleSet
from . import intent_families
importlib.import_module("trellis.learning.intent_families")
__import__("trellis.learning.intent_families")
from trellis.learning import scoring
import trellis.learning.intent_families_other_thing_is_not_a_hit_but_is_a_prefix
"""
    )

    found = _imports_of_target(tree, ["trellis", "learning"])

    # The last line shares the module's name as a prefix without being it or a
    # submodule, and a naive ``startswith`` would count it; it is not a hit.
    assert_hand_read_floor(
        len(found),
        _SYNTHETIC_FLOOR,
        subject="intent_families imports on the synthetic tree",
        hint=(
            "The tree carries 2 static imports, 2 absolute from-imports (module "
            "and parent), 2 relative ones and 2 dynamic ones. A detector that "
            "skips relative imports reports 6; one that skips dynamic ones, 6."
        ),
    )
    assert len(found) == _SYNTHETIC_FLOOR


def test_the_named_surfaces_still_exist() -> None:
    missing = [name for name in _NAMED_SURFACES if not path_is_present(_SRC / name)]

    assert not missing, f"the pin names surfaces that moved: {missing}"


def test_only_the_module_and_its_cli_and_route_import_it() -> None:
    importers: dict[str, int] = {}
    for path, tree in iter_modules(_SRC):
        relative = path.relative_to(_SRC)
        hits = _imports_of_target(tree, _package_of(relative))
        if hits:
            importers[relative.as_posix()] = len(hits)

    assert_hand_read_floor(
        len(importers),
        _REAL_IMPORTER_FLOOR,
        subject="src modules that import the intent-family module",
        hint="The CLI command module and the analyze REST route import it.",
    )
    stray = sorted(set(importers) - _ALLOWED)
    assert not stray, (
        "serving and learning keep the legacy intent family; only the relabel "
        f"report's own CLI and REST route may import {_TARGET}. Found: {stray}"
    )


@pytest.mark.parametrize("surface", _NAMED_SURFACES)
def test_no_named_surface_imports_it(surface: str) -> None:
    root = _SRC / surface
    paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
    offenders = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _imports_of_target(tree, _package_of(path.relative_to(_SRC))):
            offenders.append(path.relative_to(_SRC).as_posix())

    assert paths, f"{surface} holds no modules to check"
    assert not offenders, offenders
