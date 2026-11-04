"""Nothing that serves or learns may reach the intent-family relabel.

``trellis.learning.intent_families`` measures a candidate learning key offline.
The live key is ``normalize_intent_family`` and changing it is a post-readout
decision, so the scoring, observation, retrieval, tagging and MCP code must not
read the candidate set, not even by accident of a helpful import.

The pin runs one direction: those surfaces may not import the module, its CLI
command (``trellis_cli.analyze_intent_families``) or the REST route module
(``trellis_api.routes.analyze``), and may not name the ``intent_families`` config
key as a string, which is how a dynamic import or a read of
``registry.retrieval_config`` would reach it. The relabel itself may import
whatever it needs from them. Only the module's own CLI command and REST route
may import the module; tests are outside ``src``.
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
#: What the guarded surfaces may not import: the module, the command that
#: renders it and the route module that serves it.
_FORBIDDEN_TARGETS = (
    _TARGET,
    "trellis_cli.analyze_intent_families",
    "trellis_api.routes.analyze",
)
#: The config key; as a string constant it is a way in that no import shows.
_CONFIG_KEY = "intent_families"
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

# Hand-read on the tree in test_detector_sees_the_cli_and_route_shapes: 3 CLI
# imports (plain, from-import, parent import), 3 route imports of the same
# shapes, 2 dynamic imports, 2 relative route imports.
_SYNTHETIC_CLI_ROUTE_FLOOR = 10

# Hand-read on the tree in test_detector_sees_the_config_key_literal: a
# subscript, a bare assignment and a ``getattr`` argument.
_SYNTHETIC_LITERAL_FLOOR = 3

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


def _is_import_of(node: ast.AST, package: list[str], target: str) -> bool:
    parent, leaf = target.rsplit(".", 1)
    if isinstance(node, ast.Import):
        return any(
            alias.name == target or alias.name.startswith(f"{target}.")
            for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        module = _resolved_module(node, package)
        if module == target or module.startswith(f"{target}."):
            return True
        return module == parent and any(alias.name == leaf for alias in node.names)
    if isinstance(node, ast.Call) and name_of(node.func) in _DYNAMIC_IMPORT_NAMES:
        return target in _string_args(node)
    return False


def _imports_of(
    tree: ast.AST, package: list[str], targets: tuple[str, ...]
) -> list[ast.AST]:
    return [
        node
        for node in ast.walk(tree)
        if any(_is_import_of(node, package, target) for target in targets)
    ]


def _imports_of_target(tree: ast.AST, package: list[str]) -> list[ast.AST]:
    return _imports_of(tree, package, (_TARGET,))


def _config_key_literals(tree: ast.AST) -> list[ast.Constant]:
    """String constants equal to the config key (a longer string is not a hit)."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == _CONFIG_KEY
    ]


def _surface_paths(src: Path, surface: str) -> list[Path]:
    root = src / surface
    return [root] if root.is_file() else sorted(root.rglob("*.py"))


def _violations(src: Path, surface: str) -> list[str]:
    """Each way a module under ``surface`` reaches the relabel, by path."""
    found = []
    for path in _surface_paths(src, surface):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(src)
        if _imports_of(tree, _package_of(relative), _FORBIDDEN_TARGETS):
            found.append(f"{relative.as_posix()}: imports a relabel module")
        if _config_key_literals(tree):
            found.append(f"{relative.as_posix()}: names the {_CONFIG_KEY} config key")
    return found


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


def test_detector_sees_the_cli_and_route_shapes() -> None:
    tree = ast.parse(
        """
import trellis_cli.analyze_intent_families
from trellis_cli.analyze_intent_families import register
from trellis_cli import analyze_intent_families
import trellis_api.routes.analyze as route
from trellis_api.routes.analyze import router
from trellis_api.routes import analyze
importlib.import_module("trellis_cli.analyze_intent_families")
__import__("trellis_api.routes.analyze")
from .analyze import router
from . import analyze
from trellis_api.routes import packs
import trellis_api.routes.analyze_other_is_a_prefix_not_a_hit
from trellis_cli import analyze
"""
    )

    found = _imports_of(tree, ["trellis_api", "routes"], _FORBIDDEN_TARGETS[1:])

    assert_hand_read_floor(
        len(found),
        _SYNTHETIC_CLI_ROUTE_FLOOR,
        subject="CLI and route imports on the synthetic tree",
        hint=(
            "The tree carries 3 CLI imports, 3 route imports, 2 dynamic imports "
            "and 2 relative route imports; the last three lines are not hits."
        ),
    )
    assert len(found) == _SYNTHETIC_CLI_ROUTE_FLOOR


def test_detector_sees_the_config_key_literal() -> None:
    tree = ast.parse(
        """
"""
        '"""A module that mentions intent_families in prose is not a hit."""'
        """
a = registry.retrieval_config["intent_families"]
NAME = "intent_families"
b = getattr(learning, "intent_families")
c = "intent_families_other"
"""
    )

    found = _config_key_literals(tree)

    assert_hand_read_floor(
        len(found),
        _SYNTHETIC_LITERAL_FLOOR,
        subject="config-key literals on the synthetic tree",
        hint=(
            "The tree carries a subscript, an assignment and a getattr argument; "
            "a docstring and a longer string are not hits."
        ),
    )
    assert len(found) == _SYNTHETIC_LITERAL_FLOOR


@pytest.mark.parametrize("surface", _NAMED_SURFACES)
def test_no_named_surface_reaches_the_relabel(surface: str) -> None:
    assert _surface_paths(_SRC, surface), f"{surface} holds no modules to check"
    assert not _violations(_SRC, surface)


# A guard is proved by running it against a subject that has the defect: one
# module per way in, written under a surface, each of which the pin must flag.
_DEFECTS = {
    "module import": "from trellis.learning.intent_families import RuleSet\n",
    "module parent import": "from trellis.learning import intent_families\n",
    "cli import": "import trellis_cli.analyze_intent_families\n",
    "cli from-import": (
        "from trellis_cli.analyze_intent_families import analyze_intent_families\n"
    ),
    "cli parent import": "from trellis_cli import analyze_intent_families\n",
    "route import": "import trellis_api.routes.analyze\n",
    "route from-import": "from trellis_api.routes.analyze import router\n",
    "route parent import": "from trellis_api.routes import analyze\n",
    "cli dynamic import": (
        "import importlib\n"
        'importlib.import_module("trellis_cli.analyze_intent_families")\n'
    ),
    "route dynamic import": '__import__("trellis_api.routes.analyze")\n',
    "config key subscript": 'x = registry.retrieval_config["intent_families"]\n',
    "config key constant": 'NAME = "intent_families"\n',
    "config key getattr": 'y = getattr(learning, "intent_families")\n',
}


@pytest.mark.parametrize("source", _DEFECTS.values(), ids=_DEFECTS.keys())
def test_the_pin_flags_a_defective_subject(tmp_path: Path, source: str) -> None:
    surface = tmp_path / "trellis" / "retrieve"
    surface.mkdir(parents=True)
    (surface / "defect.py").write_text(source, encoding="utf-8")

    assert _violations(tmp_path, "trellis/retrieve") != []


def test_the_pin_passes_a_clean_subject(tmp_path: Path) -> None:
    surface = tmp_path / "trellis" / "retrieve"
    surface.mkdir(parents=True)
    (surface / "clean.py").write_text(
        '"""Mentions intent_families in prose, which is not a use."""\n'
        "from trellis.learning import scoring\n"
        "import trellis_api.routes.packs\n"
        'KEY = "intent_families_other"\n',
        encoding="utf-8",
    )

    assert _violations(tmp_path, "trellis/retrieve") == []
