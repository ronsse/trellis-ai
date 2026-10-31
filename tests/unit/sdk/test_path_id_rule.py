"""Guard: every id the SDK clients put in a URL path goes through ``path_id``.

An id is any string. Interpolated raw into ``/api/v1/entities/{id}``, ``tbl#col``
is requested as ``tbl`` and the caller reads another record's content with a
200 (``trellis_sdk._http.path_id`` has the mechanics). Fixing the three sites
that existed is not a rule: the next id-taking method would be written the
same way, so this test reads the source of both clients.

It finds every place a client builds a path -- a string that starts ``/api/``
and has anything interpolated into it, spelled as an f-string, ``+``, ``%`` or
``.format`` -- and requires each interpolated value to be a ``path_id(...)``
call. It then requires the set of methods that build such a path to equal the
roster ``test_path_id.py`` drives on the wire, so a new id-taking method fails
here until it has a row there, and on the sync and async client alike.

What it does not see: a path assembled from a literal that does not start
``/api/``, or from pieces passed through another function. A template held in
a variable (``"/api/v1/x/{}"``) is not a blind spot, because a ``/api/``
literal with a placeholder in it is itself reported.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.ast_rules import assert_hand_read_floor, is_call_to
from tests.unit.sdk.test_path_id import ROSTER

SDK_DIR = Path(__file__).parents[3] / "src" / "trellis_sdk"
CLIENTS = ("client.py", "async_client.py")
ENCODER = "path_id"
PREFIX = "/api/"


@dataclass(frozen=True)
class Site:
    """One place that builds a path, and the values it interpolates."""

    function: str
    lineno: int
    values: tuple[ast.expr, ...]
    template: bool = False

    @property
    def violations(self) -> list[str]:
        found = [
            f"{self.function}:{self.lineno} interpolates "
            f"`{ast.unparse(value)}` without {ENCODER}()"
            for value in self.values
            if not is_call_to(value, ENCODER)
        ]
        if self.template:
            found.append(
                f"{self.function}:{self.lineno} holds a path template; build the "
                f"path where it is used so {ENCODER}() can be checked"
            )
        return found


def _is_path_literal(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith(PREFIX)
    )


def _is_add(node: ast.AST) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)


def _add_leaves(node: ast.expr) -> list[ast.expr]:
    """The operands of an ``a + b + c`` chain, left to right.

    ``+`` is left-associative, so the chain is the left spine; a bracketed
    right operand is one leaf, and is read as a value to encode.
    """
    if isinstance(node, ast.BinOp) and _is_add(node):
        return [*_add_leaves(node.left), node.right]
    return [node]


def _interpolated(node: ast.expr) -> list[ast.expr]:
    """What an operand contributes: nothing for a literal, else the value."""
    if isinstance(node, ast.Constant):
        return []
    if isinstance(node, ast.JoinedStr):
        return [v.value for v in node.values if isinstance(v, ast.FormattedValue)]
    return [node]


def _site(function: str, node: ast.AST) -> Site | None:
    """The path *node* builds, if it builds one that carries a value."""
    lineno = getattr(node, "lineno", 0)
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        if first is not None and _is_path_literal(first):
            return Site(function, lineno, tuple(_interpolated(node)))
    elif _is_add(node):
        leaves = _add_leaves(node)  # type: ignore[arg-type]
        if _is_path_literal(leaves[0]):
            values = [v for leaf in leaves for v in _interpolated(leaf)]
            return Site(function, lineno, tuple(values))
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        if _is_path_literal(node.left):
            right = node.right
            values = list(right.elts) if isinstance(right, ast.Tuple) else [right]
            return Site(function, lineno, tuple(values))
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and _is_path_literal(node.func.value)
    ):
        values = [*node.args, *(kw.value for kw in node.keywords)]
        return Site(function, lineno, tuple(values))
    elif _is_path_literal(node):
        text = node.value  # type: ignore[attr-defined]
        if "{" in text or "%" in text:
            return Site(function, lineno, (), template=True)
    return None


def sites_in(source: str) -> list[Site]:
    """Every path-building site in *source*, with its enclosing function."""
    found: list[Site] = []
    covered: set[int] = set()  # inner links of a ``+`` chain already reported

    def visit(node: ast.AST, function: str) -> None:
        site = None if id(node) in covered else _site(function, node)
        if site is not None:
            found.append(site)
            spine = node
            while isinstance(spine, ast.BinOp) and _is_add(spine):
                spine = spine.left
                covered.add(id(spine))
            # The literal a ``%`` or ``.format`` fills is its own template.
            if isinstance(node, ast.BinOp):
                covered.add(id(node.left))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                covered.add(id(node.func.value))
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
            else:
                visit(child, function)

    visit(ast.parse(source), "<module>")
    return found


def _carrying(sites: list[Site]) -> list[Site]:
    return [site for site in sites if site.values or site.template]


@pytest.mark.parametrize("filename", CLIENTS)
def test_every_interpolated_path_value_is_encoded(filename: str) -> None:
    sites = _carrying(sites_in((SDK_DIR / filename).read_text(encoding="utf-8")))
    assert_hand_read_floor(
        len(sites),
        len(ROSTER),
        subject=f"path-building sites in {filename}",
        hint="get_trace, get_entity and record_feedback each build one.",
    )
    problems = [problem for site in sites for problem in site.violations]
    assert not problems, f"{filename}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("filename", CLIENTS)
def test_roster_matches_the_methods_that_build_a_path(filename: str) -> None:
    sites = _carrying(sites_in((SDK_DIR / filename).read_text(encoding="utf-8")))
    built = {site.function for site in sites}
    assert built == set(ROSTER), (
        f"{filename} builds a path in {sorted(built)}; test_path_id.py drives "
        f"{sorted(ROSTER)} on the wire. Add or remove the row in ROSTER there "
        "so the method is exercised with hostile ids on both clients."
    )


# -- the scanner against deliberately defective subjects ----------------------

DEFECTIVE = {
    "f-string": 'def m(i):\n    return f"/api/v1/x/{i}"\n',
    "another encoder": 'def m(i):\n    return f"/api/v1/x/{quote(i)}"\n',
    "plus": 'def m(i):\n    return "/api/v1/x/" + i\n',
    "plus, middle": 'def m(i):\n    return "/api/v1/x/" + i + "/y"\n',
    "plus of f-string": 'def m(i):\n    return "/api/v1/" + f"{i}"\n',
    "percent": 'def m(i):\n    return "/api/v1/x/%s" % i\n',
    "percent tuple": 'def m(a, b):\n    return "/api/v1/%s/%s" % (a, b)\n',
    "format": 'def m(i):\n    return "/api/v1/x/{}".format(i)\n',
    "format keyword": 'def m(i):\n    return "/api/v1/x/{i}".format(i=i)\n',
    "template variable": 'T = "/api/v1/x/{}"\n',
    "async method": 'class C:\n    async def m(self, i):\n        return f"/api/{i}"\n',
    "second value unencoded": (
        'def m(a, b):\n    return f"/api/v1/{path_id(a)}/{b}"\n'
    ),
}

COMPLIANT = {
    "f-string": 'def m(i):\n    return f"/api/v1/x/{path_id(i)}"\n',
    "qualified": 'def m(i):\n    return f"/api/v1/x/{_http.path_id(i)}"\n',
    "plus": 'def m(i):\n    return "/api/v1/x/" + path_id(i) + "/y"\n',
    "format": 'def m(i):\n    return "/api/v1/x/{}".format(path_id(i))\n',
    "percent": 'def m(i):\n    return "/api/v1/x/%s" % path_id(i)\n',
    "constant path": 'def m():\n    return "/api/v1/traces"\n',
    # A message that names a path is not a path.
    "message": 'def m(path):\n    return f"unexpected response body for {path}"\n',
    "docstring": 'def m():\n    """POST /api/v1/packs/{pack_id}/feedback."""\n',
}


@pytest.mark.parametrize("name", list(DEFECTIVE))
def test_scanner_reports_a_defective_subject(name: str) -> None:
    problems = [p for site in sites_in(DEFECTIVE[name]) for p in site.violations]
    assert problems, f"a {name} spelling went unreported"


@pytest.mark.parametrize("name", list(COMPLIANT))
def test_scanner_accepts_a_compliant_subject(name: str) -> None:
    problems = [p for site in sites_in(COMPLIANT[name]) for p in site.violations]
    assert not problems, problems


def test_scanner_attributes_a_site_to_its_function() -> None:
    sites = sites_in(
        "class C:\n"
        "    def a(self, i):\n"
        '        return f"/api/v1/a/{i}"\n'
        "    async def b(self, i):\n"
        '        return f"/api/v1/b/{i}"\n'
    )
    assert [site.function for site in sites] == ["a", "b"]
