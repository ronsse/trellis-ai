"""The REST server is started from exactly one place (#189).

``trellis_api.app.main`` calls ``uvicorn.run`` immediately after
``create_app`` (which itself now runs ``check_bind_is_safe`` — #189
follow-up); both CLI entry points (``trellis serve``, ``trellis admin
serve``) delegate to ``main`` rather than starting uvicorn themselves. A
second call site anywhere in ``src/`` would be a second, unchecked way to
start the server, so this rule scans for every shape that starts one:

* ``uvicorn.run(...)`` — bare or module-aliased (``import uvicorn as uv``).
* a bare ``run(...)`` bound via ``from uvicorn import run`` (any import
  alias) — the same function, imported under a name that no longer reads
  as ``uvicorn.anything``.
* ``uvicorn.Server(...).run()`` / ``.serve()`` — chained directly on the
  constructor, or called on a variable the construction was assigned to.
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


def _uvicorn_run_name_aliases(tree: ast.Module) -> set[str]:
    """Local names bound to ``uvicorn.run`` via ``from uvicorn import run``
    (any import alias)."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != "uvicorn":
            continue
        for alias in node.names:
            if alias.name == "run":
                aliases.add(alias.asname or "run")
    return aliases


def _bare_run_calls(tree: ast.Module) -> list[ast.Call]:
    """Every bare ``run(...)`` call bound via ``from uvicorn import run``.

    Distinct from :func:`_uvicorn_run_calls`: that matcher requires an
    ``X.run(...)`` attribute access, which a ``from uvicorn import run``
    import never produces — the name no longer reads as ``uvicorn``
    anything, so a scanner that only matched the attribute shape would
    miss this import form entirely.
    """
    aliases = _uvicorn_run_name_aliases(tree)
    if not aliases:
        return []
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in aliases
    ]


def _is_server_construction(node: ast.expr, uvicorn_aliases: set[str]) -> bool:
    """Whether *node* constructs a ``uvicorn.Server(...)`` (any uvicorn
    module alias from :func:`_uvicorn_aliases`)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Server"
        and name_of(node.func.value) in uvicorn_aliases
    )


def _server_run_or_serve_calls(tree: ast.Module) -> list[ast.Call]:
    """Every ``.run()``/``.serve()`` call on a ``uvicorn.Server`` instance.

    Covers both the chained form (``uvicorn.Server(config).run()``) and
    the assigned-variable form (``server = uvicorn.Server(config)`` then
    ``server.serve()`` later) — a single-hop rebinding, not the full
    fixed-point alias resolution ``tests.ast_rules.construction_names``
    offers for the generic class-construction case, because a server
    object is started right where it is built or one assignment away, in
    every real shape this project has seen.
    """
    uvicorn_aliases = _uvicorn_aliases(tree)
    constructed_vars = {
        node.targets[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and _is_server_construction(node.value, uvicorn_aliases)
    }

    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in {"run", "serve"}):
            continue
        if _is_server_construction(func.value, uvicorn_aliases) or (
            isinstance(func.value, ast.Name) and func.value.id in constructed_vars
        ):
            found.append(node)
    return found


def _server_start_calls(tree: ast.Module) -> list[ast.Call]:
    """Every call shaped like a way to start the REST server (#189)."""
    return [
        *_uvicorn_run_calls(tree),
        *_bare_run_calls(tree),
        *_server_run_or_serve_calls(tree),
    ]


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


def test_matcher_detects_bare_run_import() -> None:
    tree = ast.parse(
        """
from uvicorn import run
from uvicorn import run as start_uvicorn
import subprocess


def start_a():
    run(app, host="0.0.0.0", port=8420)


def start_b():
    start_uvicorn(app, host="0.0.0.0", port=8420)


def decoy():
    subprocess.run(["true"])
    other_run(app)
"""
    )

    found = _bare_run_calls(tree)
    assert_hand_read_floor(
        len(found),
        _SYNTHETIC_MATCH_FLOOR,
        subject="bare run() call shapes bound via `from uvicorn import run`",
        hint=(
            "One un-aliased `run(...)` and one `start_uvicorn(...)` aliased "
            "via `from uvicorn import run as start_uvicorn`; `subprocess.run` "
            "and `other_run` are decoys that must NOT match."
        ),
    )
    assert sorted(node.lineno for node in found) == [8, 12]


def test_matcher_detects_server_run_and_serve_including_via_variable() -> None:
    tree = ast.parse(
        """
import uvicorn
import subprocess


def start_chained():
    uvicorn.Server(config).run()


def start_via_variable():
    server = uvicorn.Server(config)
    server.serve()


def decoy():
    subprocess.run(["true"])
    other = NotAServer(config)
    other.run()
"""
    )

    found = _server_run_or_serve_calls(tree)
    assert_hand_read_floor(
        len(found),
        _SYNTHETIC_MATCH_FLOOR,
        subject="uvicorn.Server(...).run()/.serve() call shapes",
        hint=(
            "One chained `uvicorn.Server(config).run()` and one `.serve()` "
            "called on a variable assigned from `uvicorn.Server(...)`; "
            "`subprocess.run` and `.run()` on a non-Server variable are "
            "decoys that must NOT match."
        ),
    )
    assert sorted(node.lineno for node in found) == [7, 12]


def test_server_started_only_inside_app_main() -> None:
    """Every call that starts the REST server anywhere in ``src/`` is bound
    by ``trellis_api.app.main``'s own line range — the sole sanctioned
    bind-checked call site. Covers all three shapes :func:`_server_start_calls`
    recognizes, not just bare ``uvicorn.run``."""
    start, end = _main_function_range(ast.parse(_APP_MODULE.read_text()))

    sites: list[tuple[Path, ast.Call]] = [
        (path, call)
        for path in sorted(_SRC.rglob("*.py"))
        for call in _server_start_calls(ast.parse(path.read_text(), filename=str(path)))
    ]

    assert_hand_read_floor(
        len(sites),
        _REAL_TREE_FLOOR,
        subject="server-start call sites across src/",
        sole_site_reason=_SOLE_SITE_REASON,
    )

    offenders = [
        f"{path}:{call.lineno} starts the server outside trellis_api.app.main"
        for path, call in sites
        if not (path == _APP_MODULE and start <= call.lineno <= end)
    ]
    assert not offenders, offenders
