"""Ratchet for raw exception text reaching an MCP caller (trellis-ai#748).

A caller-facing site in ``src/trellis/mcp/server.py`` renders a caught
exception through ``_exception_detail``, which sanitizes text Trellis did
not write (``test_store_error_sanitization.py`` pins two sites). The sites
below still embed the exception on purpose: the caller's own JSON/pydantic
payload errors, which the caller needs to fix its call, and one message
``MutationExecutor`` already rendered safe. The roster is the exhaustive,
hand-read exemption list, not a todo list: a new site fails the build, and
a fixed site must shrink this roster and its count. The scan cannot see an
exception that a tool never catches.

The scan reads every module under ``src/trellis/mcp``, not just
``server.py``, because any of them can build caller-facing text.
``_MODULE_EXEMPTIONS`` is keyed by each module's path relative to the
package (:func:`_module_key`) -- a top-level module keys by its bare
filename, exactly as before, but a nested module keys by its full
relative path, so an exemption in one module never covers a same-named
function in another, and a roster key naming a module the scan never
visits fails the build instead of passing silently.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import trellis.mcp.server as server_mod
from tests.ast_rules import iter_modules, name_of

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

#: Enclosing function or method -> why its raw exception text stays. A
#: method is keyed ``ClassName.method`` so it reads the same way a
#: top-level function does; keyed by name, not line number, so moving the
#: def does not desync the roster.
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
    "_SanitizeUncaughtToolErrors.on_call_tool": (
        "str(exc) feeds a startswith(prefix) comparison that decides "
        "whether to rebuild the message through _exception_detail; it is "
        "never written into a reply itself, and the rebuilt message uses "
        "the safe helper."
    ),
}

#: Per-module exemption rosters, keyed by each module's path relative to
#: the package (see :func:`_module_key`) -- a top-level module keys by its
#: bare filename, so this reads the same regardless of where the package
#: is installed. A module absent here must scan clean: every one of its
#: raw exception-text sites is unrostered, which fails the build exactly
#: like an undeclared function in server.py does. A key here naming a
#: module the scan never visits -- renamed or deleted -- fails the build
#: too, rather than being silently ignored.
_MODULE_EXEMPTIONS: dict[str, dict[str, str]] = {
    "server.py": EXCEPTION_TEXT_EXEMPTIONS,
}

# Hand count (trellis-ai#748): 1 in save_experience, 2 in
# _resolve_evidence_pointer, 1 in record_observation, 1 in
# execute_mutation, 1 in _SanitizeUncaughtToolErrors.on_call_tool, all in
# server.py. Re-grep '{exc}\|str(exc)' in src/trellis/mcp/server.py and
# subtract the one log-only site (_build_llm_client, excluded structurally
# above) to reconcile. The scan's other shapes, repr(), an
# attribute chain inside str()/repr()/an f-string, a `%` right operand and
# a `.format(...)` argument, have no site in server.py: re-grep
# 'repr(exc\|{exc\.\|str(exc\.\|% exc\|format(exc' finds none. Every other
# module under src/trellis/mcp (__init__.py, auth.py, knowledge_links.py,
# reconcile.py, supersession.py) hand-reads clean at this count.
_HAND_READ_SITE_COUNT = 6

_SERVER_PATH = Path(inspect.getsourcefile(server_mod) or "")
_MCP_PACKAGE_DIR = _SERVER_PATH.parent

#: Calls that render their single argument's text.
_STRINGIFY_CALL_NAMES = frozenset({"str", "repr"})


def _attr_chain_root(node: ast.AST) -> str | None:
    """``Name.id`` at the root of *node*, a bare name or a chain of
    attribute access ending in one: ``exc``, ``exc.args`` and
    ``exc.__cause__.args`` all return ``"exc"``. ``None`` for anything
    else: ``type(exc).__name__`` bottoms out in a call and ``exc.args[0]``
    is a subscript, so neither is flagged, while ``exc.__class__.__name__``
    is."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _raw_exception_uses(node: ast.AST, exc_name: str) -> list[ast.AST]:
    """Renderings of *exc_name*'s text under *node*: *exc_name* or an
    attribute chain rooted at it as the sole argument to ``str()`` or
    ``repr()``, or as an f-string value (``{exc!r}`` included, since a
    conversion is not part of the value); *exc_name* itself as the right
    operand of ``%`` or as an argument to a ``.format(...)`` call. Does
    not descend into a :data:`_SAFE_CALL_NAMES` call."""
    found: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Call) and name_of(child.func) in _SAFE_CALL_NAMES:
            continue
        is_stringify_call = (
            isinstance(child, ast.Call)
            and name_of(child.func) in _STRINGIFY_CALL_NAMES
            and len(child.args) == 1
            and _attr_chain_root(child.args[0]) == exc_name
        )
        is_format_call = (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "format"
            and (
                any(isinstance(a, ast.Name) and a.id == exc_name for a in child.args)
                or any(
                    isinstance(kw.value, ast.Name) and kw.value.id == exc_name
                    for kw in child.keywords
                )
            )
        )
        if is_stringify_call or is_format_call:
            found.append(child)
        elif isinstance(child, ast.JoinedStr):
            found.extend(
                value
                for value in child.values
                if isinstance(value, ast.FormattedValue)
                and _attr_chain_root(value.value) == exc_name
            )
        elif (
            isinstance(child, ast.BinOp)
            and isinstance(child.op, ast.Mod)
            and isinstance(child.right, ast.Name)
            and child.right.id == exc_name
        ):
            found.append(child)
        found.extend(_raw_exception_uses(child, exc_name))
    return found


def _def_hits(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Raw exception-text sites in every ``except`` block under *func*,
    one in a def nested inside it included (``ast.walk`` descends into
    nested defs)."""
    hits: list[ast.AST] = []
    for node in ast.walk(func):
        if isinstance(node, ast.ExceptHandler) and node.name:
            for stmt in node.body:
                hits.extend(_raw_exception_uses(stmt, node.name))
    return hits


def _scan(tree: ast.Module) -> dict[str, list[ast.AST]]:
    """Raw exception-text sites in *tree*, grouped by enclosing def: a
    top-level function by its own name, a method of a top-level class by
    ``ClassName.method``. Same-named defs, such as a property and its
    setter, share a key and their sites add up. A def nested inside a
    function or method counts toward it; a class nested inside a class is
    not scanned."""
    by_function: dict[str, list[ast.AST]] = {}
    for top in tree.body:
        if isinstance(top, ast.FunctionDef | ast.AsyncFunctionDef):
            hits = _def_hits(top)
            if hits:
                by_function.setdefault(top.name, []).extend(hits)
        elif isinstance(top, ast.ClassDef):
            for member in top.body:
                if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                hits = _def_hits(member)
                if hits:
                    key = f"{top.name}.{member.name}"
                    by_function.setdefault(key, []).extend(hits)
    return by_function


def _mcp_scan(root: Path | None = None) -> dict[Path, dict[str, list[ast.AST]]]:
    """Raw exception-text sites for every ``*.py`` module under *root*
    (default: the real ``src/trellis/mcp`` package this process imported),
    keyed by path so two files' same-named functions cannot collide."""
    package_dir = root if root is not None else _MCP_PACKAGE_DIR
    return {path: _scan(tree) for path, tree in iter_modules(package_dir)}


def _module_key(path: Path, root: Path) -> str:
    """*path*'s roster key: its location relative to *root*, POSIX
    separators. A top-level module keys by its bare filename
    (``server.py``), unchanged from before. A module in a subpackage keys
    by its full relative path (``sub/server.py``) rather than by basename
    -- basename keying would let it inherit a same-named top-level
    module's exemptions, or (if it has none of its own) report under the
    wrong module's path when it fails to scan clean."""
    return path.relative_to(root).as_posix()


def _roster_problems(
    found_by_module: dict[str, dict[str, list[ast.AST]]],
    roster: dict[str, dict[str, str]],
) -> list[str]:
    """Reconcile a scan (module key -> function name -> sites) against
    *roster* (module key -> function name -> reason), both keyed by
    :func:`_module_key`. One problem string per module key that needs
    attention:

    - A roster key the scan never produced (its module was renamed or
      deleted) is reported on its own, since there is no scanned module
      left to diff function names against.
    - A key the scan did produce is checked against *only that key's*
      declared names, never the roster as a whole: a function exempted
      under a different module's key does not count as declaring an
      exemption here, even when two modules define a same-named function.
    """
    problems: list[str] = []
    for key in sorted(set(found_by_module) | set(roster)):
        if key not in found_by_module:
            problems.append(f"{key}: roster names a module that was not scanned")
            continue
        found = found_by_module[key]
        declared = set(roster.get(key, {}))
        unrostered = set(found) - declared
        stale = declared - set(found)
        if unrostered or stale:
            problems.append(
                f"{key}: unrostered={sorted(unrostered)}; stale={sorted(stale)}"
            )
    return problems


def test_every_raw_exception_text_site_is_a_declared_exemption() -> None:
    found_by_module = {
        _module_key(path, _MCP_PACKAGE_DIR): found
        for path, found in _mcp_scan().items()
    }
    problems = _roster_problems(found_by_module, _MODULE_EXEMPTIONS)
    assert not problems, (
        "raw exception text sites differ from the roster:\n" + "\n".join(problems)
    )
    total = sum(
        len(hits) for found in found_by_module.values() for hits in found.values()
    )
    thin = [
        f"{module}:{name}"
        for module, exemptions in _MODULE_EXEMPTIONS.items()
        for name, reason in exemptions.items()
        if len(reason.split()) < 4
    ]
    assert not thin, f"exemptions need prose reasons: {thin}"
    assert total == _HAND_READ_SITE_COUNT, (
        f"found {total} raw exception text sites across src/trellis/mcp, "
        f"expected the hand-read {_HAND_READ_SITE_COUNT}. A new site must "
        "not inherit an existing function-level exemption; a fixed site "
        "must shrink this count."
    )


def test_module_key_uses_the_relative_path_not_the_bare_filename() -> None:
    """F1: a nested module's roster key is its path relative to the
    package, not its bare filename -- looking modules up by bare filename
    would key a nested ``sub/server.py`` identically to the top-level
    ``server.py`` and let it inherit that module's exemptions."""
    root = Path("/pkg")
    assert _module_key(root / "server.py", root) == "server.py"
    assert _module_key(root / "sub" / "server.py", root) == "sub/server.py"


def test_roster_problems_catches_a_stale_module_key() -> None:
    """F1: a roster entry naming a module the scan never visited -- its
    file renamed or deleted -- fails naming that key, instead of passing
    silently because the comparison only ever walked the scanned files."""
    found_by_module: dict[str, dict[str, list[ast.AST]]] = {"server.py": {}}
    roster = {
        "server.py": {},
        "deleted_module.py": {"old_tool": "module no longer exists"},
    }
    problems = _roster_problems(found_by_module, roster)
    assert problems == ["deleted_module.py: roster names a module that was not scanned"]


def test_roster_problems_keys_exemptions_per_module() -> None:
    """F2: an exemption declared under module A's key does not excuse a
    same-named function's site in module B. Pooling exemption names across
    modules is the gap the exact total alone does not pin -- it only fails
    on a net change in site count, so a pooled lookup that both gains and
    loses a site of the same name slips through."""
    site_a, site_b = ast.parse("1").body[0], ast.parse("2").body[0]
    found_by_module = {
        "a.py": {"leaky_tool": [site_a]},
        "b.py": {"leaky_tool": [site_b]},
    }
    roster = {"a.py": {"leaky_tool": "exempt only in a.py"}}
    problems = _roster_problems(found_by_module, roster)
    assert problems == ["b.py: unrostered=['leaky_tool']; stale=[]"]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("return f'failed: {exc!r}'", id="fstring_conversion_r"),
        pytest.param("return repr(exc)", id="repr_bare"),
        pytest.param("return f'failed: {exc.args}'", id="fstring_attr_chain"),
        pytest.param("return str(exc.__cause__.args)", id="str_deep_attr_chain"),
        pytest.param("return 'failed: %s' % exc", id="percent_operand"),
        pytest.param("return 'failed: {}'.format(exc)", id="format_positional"),
        pytest.param("return 'failed: {e}'.format(e=exc)", id="format_keyword"),
    ],
)
def test_scan_catches_each_rendering_shape(body: str) -> None:
    """Non-vacuousness per shape: each shape :func:`_raw_exception_uses`
    reads, beyond the bare ``str(exc)`` and ``{exc}`` that the two tests
    below pin, is found in a function the roster has never seen."""
    defective = ast.parse(
        "def _brand_new_tool():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as exc:\n"
        f"        {body}\n"
    )
    found = _scan(defective)
    assert "_brand_new_tool" in found, f"not caught: {body}"


def test_scan_does_not_flag_the_exception_type_name() -> None:
    """Negative control: ``type(exc).__name__`` inside an f-string names
    the exception's type, not its text, so it is not flagged."""
    defective = ast.parse(
        "def _brand_new_tool():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as exc:\n"
        "        return f'failed: {type(exc).__name__}'\n"
    )
    assert _scan(defective) == {}


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


def test_the_scan_covers_every_module_not_just_server() -> None:
    """Non-vacuousness for the file discovery: the real scan visits the
    package's other modules, not just server.py."""
    scanned = {path.name for path in _mcp_scan()}
    assert scanned >= {
        "server.py",
        "supersession.py",
        "auth.py",
        "knowledge_links.py",
        "reconcile.py",
    }


def test_scan_catches_a_newly_added_leak_in_a_non_server_module(tmp_path: Path) -> None:
    """Non-vacuousness for the widening itself: a leak appended to a
    scratch copy of a real non-server module (supersession.py) is still
    caught. Checked by membership, not roster
    equality, so this holds whether or not supersession.py's own sites
    are declared exemptions at the time this runs."""
    package_dir = tmp_path / "mcp"
    package_dir.mkdir()
    source = (_MCP_PACKAGE_DIR / "supersession.py").read_text(encoding="utf-8")
    defective = source + (
        "\n\n"
        "def _brand_new_tool():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as exc:\n"
        "        return f'failed: {exc}'\n"
    )
    (package_dir / "supersession.py").write_text(defective, encoding="utf-8")
    found = _mcp_scan(package_dir)
    assert "_brand_new_tool" in found[package_dir / "supersession.py"]


def test_scan_catches_a_newly_added_leak_in_a_class_method() -> None:
    """Non-vacuousness for methods: every method of a class is found under
    ``ClassName.method``, and a property and its setter, which share that
    key, count both their sites."""
    defective = ast.parse(
        "class _BrandNewMiddleware:\n"
        "    async def on_call_tool(self, context, call_next):\n"
        "        try:\n"
        "            return await call_next(context)\n"
        "        except Exception as exc:\n"
        "            return f'failed: {exc}'\n"
        "    @property\n"
        "    def state(self):\n"
        "        try:\n"
        "            pass\n"
        "        except Exception as exc:\n"
        "            return str(exc)\n"
        "    @state.setter\n"
        "    def state(self, value):\n"
        "        try:\n"
        "            pass\n"
        "        except Exception as exc:\n"
        "            raise ValueError(f'bad state: {exc}') from exc\n"
    )
    found = {key: len(hits) for key, hits in _scan(defective).items()}
    assert found == {
        "_BrandNewMiddleware.on_call_tool": 1,
        "_BrandNewMiddleware.state": 2,
    }
