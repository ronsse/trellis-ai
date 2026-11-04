"""No ``src/trellis_api/`` module reaches a private name in any ``trellis.mcp`` module.

``routes/settings.py`` used to import ``_resolve_get_context_max_tokens`` and
two siblings from the MCP server, in order to report what an MCP request would
resolve: a REST route depending on another surface's private helpers, so a
rename inside the MCP server broke the settings page and nothing said they
were coupled. The resolvers moved to :mod:`trellis.core.knobs`, which both
surfaces import. This rule keeps the inversion from coming back.

The rule covers every module in the ``trellis.mcp`` package, not only the
``server`` facade: the server's private helpers now live in ``wiring``,
``rejections``, ``boundary`` and ``tools/*``, so a rule keyed on ``server``
alone would pass for ``from trellis.mcp.wiring import _get_registry``.

A name is private when it starts with a single underscore (a dunder is not).
The scan reports four spellings, because a rule that watched only
``from ... import _x`` would pass for ``server._x``:

* ``from trellis.mcp.<mod> import _x [as y]``
* ``from trellis.mcp[.<pkg>] import <mod> [as m]`` / ``import trellis.mcp.<mod> as m``,
  then ``m._x`` (also ``getattr(m, "_x")``)
* ``import trellis.mcp.<mod>`` then the dotted ``trellis.mcp.<mod>._x``

A public name imported *from* an MCP module is treated as an alias too, so
``from trellis.mcp.tools import context`` then ``context._x`` is reported.
That also reports a private attribute of a public object (``mcp._x``), which
is the same coupling.

Not covered, and said so rather than implied: rebinding the module to another
name (``m2 = m``) and ``importlib.import_module("trellis.mcp.server")``. Neither
occurs under ``src/trellis_api/``, and a public ``trellis.mcp`` import
(``mcp``, ``main``) is allowed.

The floors are hand-counted, not derived from the scan (see
:func:`tests.ast_rules.assert_hand_read_floor`): a scan that quietly stopped
descending would otherwise satisfy a floor it computed for itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.ast_rules import assert_hand_read_floor, iter_modules

_ROOT = Path(__file__).parents[2]
_API = _ROOT / "src" / "trellis_api"

_MCP_PACKAGE = "trellis.mcp"


def _is_mcp_module(name: str | None) -> bool:
    return name is not None and (
        name == _MCP_PACKAGE or name.startswith(_MCP_PACKAGE + ".")
    )


#: ``find src/trellis_api -name '*.py' | wc -l`` at the time of writing.
_API_MODULE_FLOOR = 27

#: Hand-counted offences in :data:`_SYNTHETIC` below: 2 names in the one
#: from-import, 1 attribute through a ``from trellis.mcp import server``
#: alias, 1 through an ``import ... as`` alias, 1 ``getattr`` literal, 1 full
#: dotted access = 6 on the ``server`` facade; then 1 from-import from
#: ``wiring``, 1 attribute through a ``from trellis.mcp.tools import context``
#: alias and 1 full dotted access into ``tools.lookup`` = 9. The decoys at the
#: end of the source must add nothing.
_SYNTHETIC_OFFENCE_FLOOR = 9

_SYNTHETIC = """
from trellis.mcp.server import _resolve_search_max_tokens, _FLAT_MAX_ITEMS as flat
from trellis.mcp import server
import trellis.mcp.server
import trellis.mcp.server as srv

server._flat_context
srv._resolve_get_context_max_tokens
getattr(srv, "_resolve_search_max_tokens")
trellis.mcp.server._get_registry

from trellis.mcp.wiring import _get_registry
from trellis.mcp.tools import context as ctx
ctx._flat_context
trellis.mcp.tools.lookup._lessons_payload

# decoys: none of these may be reported
from trellis.mcp.server import mcp, main
from trellis.retrieve.builder_factory import _graph_seeding_enabled
from trellis.mcp.tools.context import get_context
get_context.__name__
trellis.retrieve.builder_factory._graph_seeding_enabled
server.mcp
srv.__name__
getattr(srv, "mcp")
other._private
"""


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _dotted(node: ast.AST) -> str | None:
    """``a.b.c`` for a pure Name/Attribute chain, else ``None``."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _module_aliases(tree: ast.AST) -> set[str]:
    """Local names bound to an MCP module, or to a public name imported from one."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_mcp_module(alias.name) and alias.asname:
                    aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and _is_mcp_module(node.module):
            for alias in node.names:
                if not _is_private(alias.name):
                    aliases.add(alias.asname or alias.name)
    return aliases


def _names_mcp(dotted: str | None, aliases: set[str]) -> bool:
    return dotted in aliases or _is_mcp_module(dotted)


def private_server_uses(tree: ast.AST) -> list[tuple[int, str]]:
    """``(line, what)`` for every private ``trellis.mcp`` name *tree* reaches."""
    aliases = _module_aliases(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _is_mcp_module(node.module):
            found.extend(
                (node.lineno, f"from {node.module} import {alias.name}")
                for alias in node.names
                if _is_private(alias.name)
            )
        elif isinstance(node, ast.Attribute) and _is_private(node.attr):
            owner = _dotted(node.value)
            if owner is not None and (owner in aliases or _is_mcp_module(owner)):
                found.append((node.lineno, f"{owner}.{node.attr}"))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and _names_mcp(_dotted(node.args[0]), aliases)
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and _is_private(node.args[1].value)
        ):
            found.append((node.lineno, f"getattr(..., {node.args[1].value!r})"))
    return found


def private_server_uses_under(root: Path) -> list[str]:
    """One ``path:line: what`` per offence in any module under *root*."""
    return [
        f"{path.relative_to(root)}:{line}: {what}"
        for path, tree in iter_modules(root)
        for line, what in private_server_uses(tree)
    ]


def test_rule_reports_every_spelling_and_no_decoy() -> None:
    found = private_server_uses(ast.parse(_SYNTHETIC))

    assert_hand_read_floor(
        len(found),
        _SYNTHETIC_OFFENCE_FLOOR,
        subject="private trellis.mcp uses in the synthetic tree",
        hint=(
            "The tree carries 2 from-import names, 1 alias attribute via "
            "`from trellis.mcp import server`, 1 via `import ... as`, 1 getattr "
            "literal and 1 full dotted access on the server facade, plus 3 on "
            "split modules. A scan keyed on trellis.mcp.server alone reports 6; "
            "an import-only scan reports 3."
        ),
    )
    # A floor is a lower bound; the decoys are the upper one.
    assert len(found) == _SYNTHETIC_OFFENCE_FLOOR, found


def test_the_shipped_scan_fails_a_planted_defect_in_a_real_tree(
    tmp_path: Path,
) -> None:
    """Run the function the rule below calls, over a tree it must reject."""
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes" / "clean.py").write_text(
        "from trellis.mcp.server import mcp\n"
        "from trellis.core.knobs import resolve_search_max_tokens\n"
    )
    (tmp_path / "routes" / "planted.py").write_text(
        "def f():\n"
        "    from trellis.mcp.server import (  # noqa: PLC0415\n"
        "        _resolve_search_max_tokens,\n"
        "    )\n"
    )
    (tmp_path / "routes" / "split.py").write_text(
        "from trellis.mcp.wiring import _get_registry\n"
    )

    found = private_server_uses_under(tmp_path)

    assert len(found) == 2, found
    assert sorted(f.split(":")[0] + ":" + f.split(":")[1] for f in found) == [
        "routes/planted.py:2",
        "routes/split.py:1",
    ]


def test_trellis_api_reaches_no_private_mcp_name() -> None:
    modules = [path for path, _ in iter_modules(_API)]
    assert_hand_read_floor(
        len(modules),
        _API_MODULE_FLOOR,
        subject="modules scanned under src/trellis_api",
        hint="`find src/trellis_api -name '*.py' | wc -l`; discovery narrowed.",
    )

    offences = private_server_uses_under(_API)

    assert not offences, (
        "a REST module reaches a private name in a trellis.mcp module; put the "
        "shared helper in trellis.core (e.g. trellis.core.knobs) and import "
        "it from both surfaces:\n" + "\n".join(offences)
    )
