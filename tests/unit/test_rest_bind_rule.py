"""``uvicorn.run`` for the REST API is called from exactly one place (#189).

``trellis_api.app.main`` runs ``check_bind_is_safe`` immediately before
calling ``uvicorn.run``; both CLI entry points (``trellis serve``,
``trellis admin serve``) now delegate to ``main`` rather than calling
``uvicorn.run`` themselves. A second call site anywhere in ``src/`` would
be a second, unchecked way to start the server — this rule keeps that
shut.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.ast_rules import assert_hand_read_floor, name_of

_ROOT = Path(__file__).parents[2]
_SRC = _ROOT / "src"
_APP_MODULE = _SRC / "trellis_api" / "app.py"

# Hand-counted on the synthetic tree in test_matcher_detects_bare_and_aliased_calls:
# bare `uvicorn.run(...)`, aliased `uv.run(...)` via `import uvicorn as uv`.
# `subprocess.run(...)` and a bare `run(...)` are decoys that must NOT match.
_SYNTHETIC_MATCH_FLOOR = 2

# This project runs the REST server exactly one sanctioned way; see the
# module docstring. A second legitimate site would mean a second place
# that must remember to call check_bind_is_safe first.
_REAL_TREE_FLOOR = 1
_SOLE_SITE_REASON = (
    "trellis_api.app.main is the only place that calls uvicorn.run for the "
    "REST API; both trellis serve and trellis admin serve delegate to it "
    "instead of calling uvicorn themselves, by design (#189)."
)


def _uvicorn_aliases(tree: ast.Module) -> set[str]:
    """Local names bound to the ``uvicorn`` module in *tree*.

    Handles bare ``import uvicorn`` and ``import uvicorn as X`` — the one
    shape ``tests.ast_rules.construction_names`` does not resolve, since
    that helper only follows ``from X import Y as Z`` aliasing.
    """
    aliases = {"uvicorn"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            if alias.name == "uvicorn" and alias.asname:
                aliases.add(alias.asname)
    return aliases


def _uvicorn_run_calls(tree: ast.Module) -> list[ast.Call]:
    """Every call shaped like ``uvicorn.run(...)`` (any local alias)."""
    aliases = _uvicorn_aliases(tree)
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and name_of(func.value) in aliases
        ):
            found.append(node)
    return found


def _main_function_range(tree: ast.Module) -> tuple[int, int]:
    """The ``(start, end)`` line range of the module-level ``def main``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            end = node.end_lineno
            assert end is not None
            return (node.lineno, end)
    msg = "trellis_api.app has no module-level `def main` to bound the check against"
    raise AssertionError(msg)


def test_matcher_detects_bare_and_aliased_calls() -> None:
    tree = ast.parse(
        """
import subprocess
import uvicorn
import uvicorn as uv


def start_a():
    uvicorn.run(app, host="0.0.0.0", port=8420)


def start_b():
    uv.run(app, host="0.0.0.0", port=8420)


def decoy():
    subprocess.run(["true"])
    run(app)
"""
    )

    found = _uvicorn_run_calls(tree)
    assert_hand_read_floor(
        len(found),
        _SYNTHETIC_MATCH_FLOOR,
        subject="uvicorn.run call shapes on the synthetic tree",
        hint=(
            "The tree carries one bare `uvicorn.run` and one aliased "
            "`uv.run`, plus a `subprocess.run` and a bare `run()` that "
            "must NOT match."
        ),
    )
    assert sorted(node.lineno for node in found) == [8, 12]


def test_uvicorn_run_called_only_inside_app_main() -> None:
    """Every ``uvicorn.run`` call anywhere in ``src/`` is bound by
    ``trellis_api.app.main``'s own line range — the sole sanctioned
    bind-checked call site."""
    start, end = _main_function_range(ast.parse(_APP_MODULE.read_text()))

    sites: list[tuple[Path, ast.Call]] = [
        (path, call)
        for path in sorted(_SRC.rglob("*.py"))
        for call in _uvicorn_run_calls(ast.parse(path.read_text(), filename=str(path)))
    ]

    assert_hand_read_floor(
        len(sites),
        _REAL_TREE_FLOOR,
        subject="uvicorn.run call sites across src/",
        sole_site_reason=_SOLE_SITE_REASON,
    )

    offenders = [
        f"{path}:{call.lineno} calls uvicorn.run outside trellis_api.app.main"
        for path, call in sites
        if not (path == _APP_MODULE and start <= call.lineno <= end)
    ]
    assert not offenders, offenders
