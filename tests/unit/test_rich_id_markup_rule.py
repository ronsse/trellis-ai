"""Enforcement for the id-through-Rich rule (#492).

``trellis retrieve search`` printed a real production id like this::

    in:  '  - dataset:snowflake://db/schema/table: preview [document] tail'
    out: '  - dataset❄//db/schema/table: preview  tail'

Two independent corruptions in one line, and neither raises anything:

* **Emoji substitution.** Rich reads ``:snowflake:`` inside
  ``dataset:snowflake://…`` as a shortcode and swaps in the glyph. Trellis
  ids are colon-delimited by construction, and ``notes``, ``book``, ``art``,
  ``key``, ``link``, ``memo``, ``zap``, ``warning`` and ``x`` are all live
  emoji names — this is a property of the id scheme, not bad luck.
* **Markup interpretation.** ``[document]`` is read as a style tag and
  **deleted**. That is the worse half: the text is removed rather than
  altered, so nothing on screen signals that anything happened.

An item id is the operator's handle for the next ``get_items`` or
``trellis retrieve get``. A mangled id is not merely ugly — it is *not the
id*, and copying it produces a lookup that fails for a reason the operator
cannot see. #403 fixed this class on the ``--format json`` arm, where
:mod:`tests.unit.test_machine_output_rule` now enforces it; this is the
text arm of the same family, and #488 had already fixed two instances of
it by hand on one renderer in one file. The issue's own words: *a roster
of two is how this recurs a third time.*

So the rule, in two halves, because the two corruptions are switched off
in different places:

    **Emoji substitution is off at the console.** Nothing under ``src/``
    builds a :class:`rich.console.Console` except
    :func:`trellis_cli.output.build_console`, which passes
    ``emoji=False``.

    **Markup is escaped at the value.** Every operator-copyable expression
    reaching a Rich renderer in ``src/trellis_cli`` — an identifier or a
    filesystem location, by the name-shape below — is wrapped in
    :func:`rich.markup.escape`, unless the render call turns markup off
    wholesale with ``markup=False``.

**Why the emoji half is a construction-time setting and not a per-call
one.** The characters have to survive verbatim — the whole point is that
the operator can copy them — so nothing may be *inserted* into the id to
defang a shortcode, which rules out any value-side fix. That leaves
turning the renderer's emoji pass off, and doing it at construction covers
every render path a console has (``console.print``, a ``Table`` cell, a
``Panel`` body) including the ones nobody has written yet. That last claim
is checked rather than asserted:
:func:`test_a_table_cell_inherits_the_console_setting` renders a cell,
which never touches ``Console.print``'s ``emoji=`` argument at all.

**And why it is a factory rather than twenty-one repetitions of the
keyword.** ``tests.cli_output.force_colour`` replaces a module's console
with a colour-forcing one so that a test can assert against the *coloured*
renderer — the path CI and a real terminal take, and the one #495 found 21
tests blind to. A helper rebuilding a plain ``Console(force_terminal=True)``
would have handed that path an emoji-substituting renderer production does
not have: a failure invented by the harness, visible only under colour.
One door means the test console and the shipped console cannot drift, and
this module enforces that the door is the only one.

**Why the markup half is a value-side escape and not ``markup=False``.**
``markup=False`` also disables the ``[green]…[/green]`` tags in the
literal half of the same f-string, so applying it everywhere would strip
the CLI's colour. ``escape`` renders ``\\[`` for a literal ``[`` and Rich
prints it back as ``[``, so the id survives *and* the styling does. Both
spellings satisfy the rule: ``retrieve pack``'s item line already used the
second and keeps it, and ``curate link``'s arrow — whose *literal*
``--[edge_kind]-->`` brackets were being eaten too — is a fresh case where
escaping the values alone would have fixed half a line.

**What the name shape means, and what it deliberately does not.** A name
ending ``_id`` / ``_ids`` / ``_path`` / ``_paths`` / ``_dir`` / ``_dirs``
/ ``_file`` / ``_files``, or spelled as one of those words bare, read off
an :class:`ast.Name`, an :class:`ast.Attribute`'s trailing attribute, or
any string constant in the expression — which covers ``row['doc_id']`` and
``entry.get('source_path')`` without special-casing either. That is a rule
over the *text of the expression*, not a list of blessed call sites, which
is the whole point — but it buys the coverage with two honest costs,
stated here rather than discovered later:

* **It over-collects, and that is left alone.** ``analyze.py``'s
  ``report.response_events_with_pack_id`` is an integer count whose name
  happens to end in ``_id``; it is escaped like everything else, at a cost
  of one ``str()``. Deciding per site which ``_id`` is really an
  identifier is exactly the judgement that rots into a roster, and the
  ULID-only fields the #492 plan review set aside (``pack_id``,
  ``key_id``, ``candidate_id``) are set aside here too — the rule cannot
  tell them apart, escaping them costs nothing, and the day one of them
  stops being a ULID nobody has to notice.
* **It under-collected on names that do not say what they hold, and that
  cost was not hypothetical.** An id bound to ``x`` and printed as
  ``f"{x}"`` walked past: :func:`tests.ast_rules.construction_names`
  follows ``x = doc_id``, an import alias and a subclass to a fixed point,
  but not value flow, so ``x = row["doc_id"]`` was invisible. Filed as
  residue — and **nine live offences were hiding behind it** when the
  first sweep landed, across five modules, carrying a trace id, an entity
  id, a corpus ``doc_id``, two ``Path`` objects, a source file and a legacy
  graph key. The pair that says it best is ``ingest_conversations.py`` and
  ``ingest_corpus.py``: the *same expression*,
  ``entry.get("source_path") or entry["doc_id"]``, escaped in the file
  that wrote it inline and missed in the file that hoisted it into a
  local one line up. :func:`_hoisted_handle_names` closes it, bounded to
  values that *build a string* so a dict holding a handle does not taint
  its own unrelated keys — measured at 9 reports and zero false ones,
  against 49 and mostly false for the unbounded version.

**Where the rule does not reach.** ``sys.stdout.write`` — what ``retrieve
search --quiet`` and ``retrieve pack --quiet`` already use — is not a
renderer and is correctly ignored; that is the property that made the
``--quiet`` arms safe before this rule existed, and it is pinned as a
non-offence in :data:`_JUDGEMENTS` rather than left to this sentence. The
markup half is scoped to ``src/trellis_cli`` because no other package
renders through Rich — which is not left as a claim either:
:func:`test_rich_rendering_is_confined_to_the_cli_package` sweeps the whole
of ``src/`` for a console reached *by either route*, so the day another
package wants one, that test says the markup scan has to widen with it.
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

from rich.markup import escape
from rich.table import Table

from tests.ast_rules import (
    assert_hand_read_floor,
    assert_scan_is_not_vacuous,
    construction_names,
    iter_modules,
    name_of,
)
from trellis_cli.output import build_console

#: Rich methods that render markup. ``print`` / ``print_json`` / ``out`` /
#: ``log`` are :mod:`tests.unit.test_machine_output_rule`'s set, kept
#: identical on purpose — two rules policing the same renderer must not
#: disagree about what a renderer is. ``add_row`` is this rule's addition:
#: a ``Table`` cell goes through the console's own markup pass exactly as
#: a printed line does, and ``policy list``, ``admin api-keys list``,
#: ``curate promote-learning`` and three ``metrics`` tables all put ids in
#: cells.
#:
#: Matched on the attribute name alone, with no check on the receiver. The
#: CLI reaches Rich through ``console``, ``err_console`` and a locally
#: built console in ``stores.py``, and matching the receiver would miss
#: whichever spelling a future module picks. Over-collection here costs a
#: spurious escape.
_RICH_RENDER_METHODS = frozenset({"print", "print_json", "out", "log", "add_row"})

#: Name shapes that mean "an operator will copy this". Suffixes and whole
#: names are separate because ``path`` is a name and ``_path`` is an
#: ending, and a single ``endswith`` over ``("id", "path", "dir")`` would
#: drag in every ``valid``, ``grid``, ``width`` and ``death`` in the tree.
_ID_SUFFIXES = (
    "_id",
    "_ids",
    "_path",
    "_paths",
    "_dir",
    "_dirs",
    "_file",
    "_files",
)
_ID_NAMES = frozenset(
    {
        "id",
        "ids",
        "path",
        "paths",
        "dir",
        "dirs",
        "file",
        "files",
        # ``relpath`` has no underscore, so neither the suffix list above
        # nor the whole-name list caught it — and ``ingest corpus`` /
        # ``ingest conversations`` each printed one raw, from a directory
        # of *user-named* files, which is the most adversarial input this
        # CLI takes. A roster of eight is how the ninth gets missed.
        "relpath",
        "relpaths",
    }
)

#: The escape function the rule accepts. Bare name, so
#: ``from rich.markup import escape`` and ``markup.escape`` both land — and
#: so would a local ``escape`` that does something else, which is the
#: over-collecting direction and the cheap one.
_ESCAPE = "escape"

#: The one Rich *class* whose construction renders markup: ``Table``'s
#: ``title`` and ``caption`` go through the console's markup pass exactly
#: as its cells do. Nothing else Rich exposes is constructed anywhere in
#: ``src/`` — no ``Panel``, ``Markdown``, ``Syntax`` or ``Progress`` — so
#: the list is one entry rather than a guess at the library's surface.
_TABLE = "Table"

#: The one module allowed to construct a ``rich.console.Console``. Matched
#: on the file *and* its package, so a synthetic corpus containing an
#: ``output.py`` cannot exempt itself and neither can a second
#: ``output.py`` elsewhere in ``src/``.
_CONSOLE_FACTORY_MODULE = ("trellis_cli", "output.py")


def _cli_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src" / "trellis_cli"
    assert root.is_dir(), f"trellis_cli not found at {root}"
    return root


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src"
    assert root.is_dir(), f"src/ not found at {root}"
    return root


def _is_id_shaped(name: str) -> bool:
    return name.endswith(_ID_SUFFIXES) or name in _ID_NAMES


def _names_read(node: ast.AST) -> set[str]:
    """Every name a subtree mentions: locals, attributes and string keys.

    Four spellings, and each one is a live site rather than a hypothetical:
    ``item_id`` (a local), ``result.trace_id`` (a trailing attribute),
    ``row['doc_id']`` (a subscript key) and
    ``entry.get('source_path')`` (a key that is a plain call argument). A
    predicate reading only :class:`ast.Name` sees the second as ``result``
    and the third as ``row`` and lets both through — **44 of the 81**
    values this rule polices across ``src/trellis_cli`` reach their handle
    through an attribute or a string key, so that predicate would be blind
    to more than half of them.

    String constants are read wholesale rather than only in a subscript,
    which is what covers the ``.get`` spelling without special-casing
    ``get``. It over-collects in principle — a literal ``"path"`` passed to
    anything at all counts — and by measurement it over-collects by
    **zero** sites across ``src/trellis_cli``, which is the trade this rule
    takes everywhere: an unnecessary ``escape`` costs nothing and a missed
    one costs an id.
    """
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
        elif isinstance(child, ast.Attribute):
            found.add(child.attr)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.add(child.value)
    return found


#: Value shapes that make a local *a rendered string built from a handle*
#: rather than an object that merely holds one. Taint follows only these.
#:
#: The distinction is the whole of why this widening is affordable, and it
#: is measured rather than argued. Tainting on *any* value that mentions an
#: id-shaped name reports 49 sites across ``src/trellis_cli`` and the large
#: majority are false — ``payload = {...,"doc_id":...}`` then
#: ``payload["scanned"]``, ``summary["tier"]``, ``result.effect_size``,
#: ``match.enforcement.value`` — because an *object* binding inherits taint
#: from a key nobody renders. Restricting the value to an f-string, a
#: ``or``-default, a subscript or a concatenation reports **9**, and every
#: one of the nine was a live offence.
_STRING_BUILDING_VALUES = (ast.JoinedStr, ast.BoolOp, ast.Subscript, ast.BinOp)


def _hoisted_handle_names(scope: ast.AST, handles: set[str]) -> set[str]:
    """Locals in *scope* bound to a string built out of a *handles* name.

    The under-collection this rule shipped with, closed. ``_names_read``
    reads the expression a render *hands to Rich*, so a handle that was
    interpolated one line earlier is invisible::

        pruned_name = entry.get("source_path") or entry["doc_id"]
        console.print(f"  [red]prune [/red] {pruned_name}")

    That is ``ingest_corpus.py``, and its twin in
    ``ingest_conversations.py`` — the identical expression, left inline —
    *was* escaped by #492's sweep. Same defect, same pair of files, and
    only the one whose author had not hoisted it into a local was found.
    Nine such sites were live after that sweep across five modules,
    carrying a trace id, an entity id, a corpus ``doc_id``, two ``Path``
    objects, a source file and a legacy graph key.

    Scoped to one function rather than the module, because a module-wide
    pass makes every ``msg`` and ``result`` in the file mean whatever the
    unluckiest one means.

    ``construction_names`` is not the tool for this: it resolves a
    binding whose *value is a name*, and none of these are. The two stay
    separate — a bare rebinding is a different shape from a string built
    around a handle, and collapsing them would make the roster's
    ``partial_binding`` residue look resolved when it is not.

    Loop targets need their own binding rule because they create no
    :class:`ast.Assign`. All targets inherit taint when the iterable is a
    bare handle-bearing name (``for line in doc_ids``). Keeping that
    lexical bound matters: ``report.files`` is a collection of outcome
    objects, not filesystem paths, and tainting the object would falsely
    classify every rendered attribute on it as a handle. The live
    ``migrate-graph`` shape is narrower and otherwise carries no lexical
    clue: ``MigrationReport.errors`` stores ``(legacy_graph_key, message)``
    tuples. For an ``*.errors`` iterable only the first unpacked target is
    tainted; the message beside it remains prose. That bounds the widening
    to the report contract instead of declaring every loop variable a
    handle.
    """
    tainted: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            target_names = [
                child.id
                for child in ast.walk(node.target)
                if isinstance(child, ast.Name)
            ]
            if isinstance(node.iter, ast.Name) and node.iter.id in handles:
                tainted.update(name for name in target_names if not _is_id_shaped(name))
            elif (
                isinstance(node.target, (ast.Tuple, ast.List))
                and node.target.elts
                and "errors" in _names_read(node.iter)
            ):
                first_names = [
                    child.id
                    for child in ast.walk(node.target.elts[0])
                    if isinstance(child, ast.Name)
                ]
                tainted.update(name for name in first_names if not _is_id_shaped(name))
            continue
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value: ast.expr | None = node.value
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and isinstance(
            node.target, ast.Name
        ):
            targets, value = [node.target.id], node.value
        else:
            continue
        if not targets or value is None:
            continue
        if not isinstance(value, _STRING_BUILDING_VALUES):
            continue
        if _is_escaped(value) or not (_names_read(value) & handles):
            continue
        tainted.update(name for name in targets if not _is_id_shaped(name))
    return tainted


def _id_bearing_names(tree: ast.Module) -> set[str]:
    """Every name in *tree* that holds a copyable handle, aliases included.

    The literal name-shaped ones, closed under
    :func:`tests.ast_rules.construction_names` so ``_short = doc_id`` and
    ``from mod import doc_id as _d`` do not buy an exemption. On the real
    tree that closure adds nothing today — the CLI writes no such
    rebinding — and it is here because the shared roster's whole binding
    axis would otherwise be a blind spot, and a blind spot nobody wrote
    down is how the last four AST rules in this repo shipped evadable.

    :func:`_hoisted_handle_names` adds the closure that *does* pay on the
    real tree: a handle interpolated into a local one line above the
    render. That one is applied per function, in
    :func:`_unescaped_id_renders`, so it is not folded in here.
    """
    names: set[str] = set()
    for name in _names_read(tree):
        if _is_id_shaped(name):
            names |= construction_names(name, tree)
    return names


def _is_escaped(expr: ast.expr) -> bool:
    """Is *expr* wrapped in ``escape(...)`` at its **outermost** node?

    Outermost, not "contains an ``escape`` call somewhere". ``worker.py``
    read::

        f"[red]  {failure['trace_id']}: {escape(str(failure['error']))}[/red]"

    where the *message* was escaped and the trace id beside it was not. A
    substring search for ``escape(`` calls that line compliant, and it was
    a live offence.
    """
    return isinstance(expr, ast.Call) and name_of(expr.func) == _ESCAPE


def _rendered_values(call: ast.Call) -> list[ast.expr]:
    """The expressions *call* hands to Rich, one per interpolation.

    An f-string argument contributes each ``{...}`` separately, so the
    ``worker.py`` line above is judged per value rather than per line. Any
    other argument (a ``Table`` cell is a bare expression) contributes
    itself.

    Keyword values are read as well as positional ones, and that is for
    ``Table(title=...)`` / ``Table(caption=...)`` rather than for
    ``print``: Rich's ``print`` and ``add_row`` take their renderables
    positionally and their keywords are switches, but a table's title is a
    rendered string arriving under a name. No live site puts a handle
    there today — measured, zero across ``src/trellis_cli`` — which is
    exactly why it is worth covering now: the alternative is to notice the
    first one after it ships, which is the whole history of this defect.
    """
    values: list[ast.expr] = []
    for arg in [*call.args, *(kw.value for kw in call.keywords)]:
        if isinstance(arg, ast.JoinedStr):
            values.extend(
                node.value
                for node in ast.walk(arg)
                if isinstance(node, ast.FormattedValue)
            )
        else:
            values.append(arg)
    return values


def _markup_disabled(call: ast.Call) -> bool:
    """``markup=False`` — the wholesale alternative to escaping.

    Only a literal ``False`` counts. ``markup=some_flag`` is not a decision
    this scan can read, and ``emoji=False`` is deliberately *not* accepted:
    it closes the other half of the defect and leaves ``[document]`` being
    eaten. That distinction is pinned as a judgement shape rather than left
    to this docstring.
    """
    return any(
        keyword.arg == "markup"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in call.keywords
    )


def _is_rich_render(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _RICH_RENDER_METHODS
    )


def _rich_entry_points(tree: ast.Module) -> list[ast.Call]:
    """Every call in *tree* whose arguments Rich parses as markup.

    Two shapes: a render method on a console or a table, and a ``Table``
    *construction*, whose ``title`` is rendered the same way its cells are.
    The second is resolved by name through
    :func:`tests.ast_rules.construction_names` rather than matched on an
    attribute, because it is a class and the CLI imports it bare.
    """
    table_names = construction_names(_TABLE, tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (_is_rich_render(node) or name_of(node.func) in table_names)
    ]


def _enclosing_scopes(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """``{node: innermost enclosing function, or the module}``.

    Built by descent rather than read off line numbers: a decorator
    expression sits outside its function's body but inside its line span,
    and the shared roster ships a ``decorator`` placement shape.
    """
    scopes: dict[ast.AST, ast.AST] = {}

    def descend(node: ast.AST, scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else scope
            )
            scopes[child] = inner
            descend(child, inner)

    scopes[tree] = tree
    descend(tree, tree)
    return scopes


def _unescaped_id_renders(root: Path | None = None) -> list[str]:
    """Rich renders carrying a copyable handle nothing escaped.

    *root* is injectable so the vacuity guards run **this** function over a
    synthetic tree. A guard that re-implements the predicate guards a copy
    of itself and leaves the shipped one free to regress — the failure
    ``test_machine_output_rule`` names in its own docstring.

    Two name sets, not one: the module-wide handles, plus whatever the
    *enclosing function* hoisted a handle into (:func:`_hoisted_handle_names`).
    The second is per scope because ``msg`` and ``line`` mean something
    different in every command body.
    """
    cli_root = root if root is not None else _cli_root()
    found: list[str] = []
    for path, tree in iter_modules(cli_root):
        names = _id_bearing_names(tree)
        scopes = _enclosing_scopes(tree)
        hoisted: dict[ast.AST, set[str]] = {}
        for node in _rich_entry_points(tree):
            if _markup_disabled(node):
                continue
            scope = scopes.get(node, tree)
            if scope not in hoisted:
                hoisted[scope] = _hoisted_handle_names(scope, names)
            names_here = names | hoisted[scope]
            offending = [
                value
                for value in _rendered_values(node)
                if _names_read(value) & names_here and not _is_escaped(value)
            ]
            if offending:
                rendered = ", ".join(ast.unparse(value) for value in offending)
                found.append(f"{path.name}:{node.lineno}: {rendered}")
    return found


def _console_constructions(root: Path | None = None) -> list[tuple[Path, ast.Call]]:
    """Every direct ``Console(...)`` under *root*, however the name is bound."""
    search_root = root if root is not None else _src_root()
    sites: list[tuple[Path, ast.Call]] = []
    for path, tree in iter_modules(search_root):
        names = construction_names("Console", tree)
        sites.extend(
            (path, node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and name_of(node.func) in names
        )
    return sites


def _consoles_built_outside_the_factory(root: Path | None = None) -> list[str]:
    package, module = _CONSOLE_FACTORY_MODULE
    return [
        f"{path.name}:{node.lineno}"
        for path, node in _console_constructions(root)
        if not (path.name == module and path.parent.name == package)
    ]


def _factory_calls(root: Path | None = None) -> list[tuple[Path, ast.Call]]:
    """Every ``build_console(...)`` under *root* — the compliant half."""
    search_root = root if root is not None else _src_root()
    sites: list[tuple[Path, ast.Call]] = []
    for path, tree in iter_modules(search_root):
        names = construction_names("build_console", tree)
        sites.extend(
            (path, node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and name_of(node.func) in names
        )
    return sites


# ---------------------------------------------------------------------------
# The two invariants
# ---------------------------------------------------------------------------


def test_no_copyable_handle_reaches_a_rich_renderer_unescaped() -> None:
    """The markup half. Wrap the value in ``rich.markup.escape``."""
    violations = _unescaped_id_renders()
    assert not violations, (
        "Rich reads `[...]` as a style tag and deletes it (#492), so an id "
        "or path printed raw is not the id the operator copies. Wrap the "
        "value in `rich.markup.escape` — or pass `markup=False` to the "
        "render call if it carries no styling of its own.\n  " + "\n  ".join(violations)
    )


def test_every_console_comes_from_the_shared_factory() -> None:
    """The emoji half, enforced structurally rather than by name shape."""
    violations = _consoles_built_outside_the_factory()
    assert not violations, (
        "Rich rewrites `:snowflake:` inside a real "
        "`dataset:snowflake://...` id (#492/#403), and no value-side fix "
        "is possible because the characters have to survive verbatim. "
        "Build the console with `trellis_cli.output.build_console`, which "
        "passes `emoji=False` — a bare `Console()` reintroduces the "
        "defect for every render path that module has.\n  " + "\n  ".join(violations)
    )


def test_rich_rendering_is_confined_to_the_cli_package() -> None:
    """The scoping claim above, checked instead of asserted.

    The *markup* half only scans ``src/trellis_cli``, which is sound
    exactly while nothing else renders through Rich. The console rule
    alone does not establish that — a module in ``trellis_api`` importing
    the factory would satisfy it and still print ids no scan reads — so
    both routes to a console are pinned to the CLI package here. The day
    another package wants one, this is what says the markup rule has to
    widen with it.
    """
    outside = sorted(
        f"{path.relative_to(_src_root())}:{node.lineno}"
        for path, node in [
            *_console_constructions(_src_root()),
            *_factory_calls(_src_root()),
        ]
        if "trellis_cli" not in path.parts
    )
    assert not outside, (
        "a Rich console is built outside src/trellis_cli, so the "
        "identifier-escaping rule no longer covers every Rich surface in "
        f"the tree. Widen _cli_root() or move the render: {outside}"
    )


def test_only_the_real_factory_module_is_exempt(tmp_path: Path) -> None:
    """The exemption is a path, not a filename.

    ``_consoles_built_outside_the_factory`` waves through exactly one file,
    and "the file called ``output.py``" is not the same rule as "the file
    called ``output.py`` **in trellis_cli**". Found by mutation: relaxing
    the check to the filename alone left every other test in this module
    green, because the shared roster's corpus contains no ``output.py`` to
    tell the two apart. A second ``output.py`` anywhere under ``src/`` —
    ``trellis_api/output.py`` is an entirely plausible one — would then
    have been silently licensed to build an emoji-substituting console.
    """
    package = tmp_path / "trellis_api"
    package.mkdir()
    (package / "output.py").write_text(
        "from rich.console import Console\n\nconsole = Console()\n"
    )

    assert _consoles_built_outside_the_factory(tmp_path) == ["output.py:3"], (
        "a module named output.py outside trellis_cli exempted itself from "
        "the console rule"
    )


# ---------------------------------------------------------------------------
# Floors — hand-read, so a scan that stops matching cannot supply them
# ---------------------------------------------------------------------------

#: Hand-read off ``src/trellis_cli`` at ``8a21d3f``: 594 ``console.print``
#: -family calls, 48 ``Table.add_row`` calls and 42 ``Table(...)``
#: constructions — 684 entry points. Floored far below that:
#: the CLI's prose output changes every week and a floor that tracks the
#: tree is one nobody re-reads. What it has to catch is the scan going to
#: near-zero.
_RENDER_FLOOR = 100

#: Hand-read off ``src/`` at ``8a21d3f``: 21 console constructions, all in
#: ``trellis_cli`` — one per command module, plus ``classify.py``'s stderr
#: console and the one ``stores.py`` builds inline for its
#: not-initialized warning. After this change they are 21
#: ``build_console`` calls plus the single ``Console(...)`` inside the
#: factory, and the floor is taken over *both*, because the rule's
#: population is "console constructions in the tree" however they are
#: spelled — a floor over direct ``Console(...)`` calls alone would read 1
#: and be satisfied by a scan that had stopped working.
_CONSOLE_FLOOR = 15


def _render_population(root: Path | None = None) -> int:
    search_root = root if root is not None else _cli_root()
    return sum(len(_rich_entry_points(tree)) for _, tree in iter_modules(search_root))


def _console_population(root: Path | None = None) -> int:
    return len(_console_constructions(root)) + len(_factory_calls(root))


def test_the_scan_finds_the_render_calls_it_polices() -> None:
    assert_hand_read_floor(
        _render_population(),
        _RENDER_FLOOR,
        subject="Rich render call in trellis_cli",
        hint="console/err_console .print, .print_json, .out, .log and Table.add_row.",
    )


def test_the_scan_finds_the_consoles_it_polices() -> None:
    assert_hand_read_floor(
        _console_population(),
        _CONSOLE_FLOOR,
        subject="console construction in src/",
        hint="build_console() calls plus any direct rich Console(...).",
    )


# ---------------------------------------------------------------------------
# This rule's own judgement, against hand-read line numbers
# ---------------------------------------------------------------------------

#: The distinctions no shared roster can encode, because they are about
#: *what this rule considers an offence* rather than about where a call can
#: hide. Each line's comment carries its real line number in the rendered
#: file.
_JUDGEMENTS = """
import sys
from rich.markup import escape

console.print(f"  - {doc_id}")                       # 4  the control
console.print(f"  - {row['doc_id']}")                # 5  string subscript key
console.print(f"  - {result.trace_id}")              # 6  trailing attribute
console.print(f"  - {doc_id[:8]}")                   # 7  sliced id
console.print(f"  - {entry.get('source_path')}")     # 8  a path, not an id
err_console.print(f"{policy_id}")                    # 9  a second console name
table.add_row(candidate_id)                          # 10 a Table cell renders too
console.print(f"{doc_id}", emoji=False)              # 11 emoji=False is NOT enough
console.print(f"{escape(msg)}: {doc_id}")            # 12 one escaped, one not
console.print(f"[green]ok[/green]: {escape(doc_id)}")  # 13 ALLOWED: escaped
console.print(f"{escape(str(config_path))}")         # 14 ALLOWED: escape(str(...))
console.print(f"  - {doc_id}", markup=False)         # 15 ALLOWED: markup off
table.add_row(escape(candidate_id))                  # 16 ALLOWED: escaped cell
console.print(f"  - {count} of {total}")             # 17 ALLOWED: not id-shaped
console.print("literal text")                        # 18 ALLOWED: no value at all
sys.stdout.write(f"{doc_id}\\n")                      # 19 ALLOWED: not a renderer
Table(title=f"rows for {doc_id}")                    # 20 a title renders markup too
Table(title=f"rows for {escape(doc_id)}")            # 21 ALLOWED: escaped title
console.print("x", style="dim", soft_wrap=True)      # 22 ALLOWED: switches not values
line = f"  - {doc_id}: preview"                      # 23
console.print(line)                                  # 24 hoisted one line earlier
bag = {"doc_id": doc_id}                             # 25
console.print(bag["count"])                          # 26 ALLOWED: object, not a string
plain = f"  - {count} items"                         # 27
console.print(plain)                                 # 28 ALLOWED: no handle in it
console.print(escape(line))                          # 29 ALLOWED: escaped at the render
console.print(f"{fmt(escape(msg), doc_id)}")         # 30 escape is not outermost
console.print(f"  - {outcome.relpath}")              # 31 relpath has no underscore
for loop_value in doc_ids:                           # 32 handle-bearing iterable
    console.print(loop_value)                        # 33 loop target inherits handle
for target, msg in report.errors:                    # 34 migrate-graph report shape
    console.print(target)                            # 35 first tuple item is legacy key
    console.print(msg)                               # 36 ALLOWED: message is prose
"""

#: Read off :data:`_JUDGEMENTS` by hand. Line 24 is the newest and the one
#: #521's gate added: a handle interpolated into a local one line above the
#: render was invisible to a scan that reads only the rendered expression,
#: and nine such sites were live in ``src/trellis_cli`` *after* the escaping
#: sweep. Lines 26 and 28 are its negative controls, and 26 is the one that
#: bounds the widening: a *dict* built around a handle must not taint its
#: own unrelated keys, or ``payload["scanned"]`` and forty of its siblings
#: become offences. Line 11 is the other one worth naming:
#: ``emoji=False`` closes the shortcode half and leaves ``[document]``
#: being eaten, so it must not buy an exemption. Line 30 pins
#: ``_is_escaped``'s *outermost* rule, which nothing pinned before: its
#: docstring cites a ``worker.py`` line where one interpolation was escaped
#: and its neighbour was not, but ``_rendered_values`` splits an f-string
#: per ``{...}`` and covers that on its own — so relaxing ``_is_escaped``
#: to "contains an ``escape`` call anywhere" left the whole module green.
#: Only a *single* interpolation carrying both an escaped and a raw value
#: separates the two. Line 31 is the
#: cheapest of the lot and was a live miss in two files: ``relpath`` has no
#: underscore, so neither ``_ID_SUFFIXES`` nor the original
#: ``_ID_NAMES`` saw a path in it. Line 19 is the other:
#: ``retrieve search --quiet`` writes straight to ``sys.stdout`` and was
#: already safe — a rule that flagged it would be turned off by the first
#: author it inconvenienced. Line 22 is the cost of reading keyword values
#: at all: ``print``'s keywords are switches rather than content, and if
#: scanning them ever starts flagging one, this is where it shows. Lines 33
#: and 35 distinguish a handle-bearing iterable and migrate-graph's
#: ``(legacy_graph_key, message)`` report from the prose target on line 36.
_EXPECTED_JUDGEMENT_LINES = [
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    20,
    24,
    30,
    31,
    33,
    35,
]


def test_the_scan_makes_the_judgements_this_rule_claims(tmp_path: Path) -> None:
    """Run the shipped predicate over the offences and the non-offences.

    Both halves matter. Without the allowed lines the rule is satisfied by
    a predicate that reports every render call; without the offending ones
    it is satisfied by one that reports nothing.
    """
    (tmp_path / "judgements.py").write_text(_JUDGEMENTS.lstrip("\n"))

    reported = sorted(
        int(violation.split(":")[1]) for violation in _unescaped_id_renders(tmp_path)
    )
    assert reported == _EXPECTED_JUDGEMENT_LINES, (
        f"scanner reported {reported}, expected {_EXPECTED_JUDGEMENT_LINES}; "
        f"missing={sorted(set(_EXPECTED_JUDGEMENT_LINES) - set(reported))} "
        f"spurious={sorted(set(reported) - set(_EXPECTED_JUDGEMENT_LINES))}"
    )


# ---------------------------------------------------------------------------
# The shared roster — spelling, binding, placement, discovery
# ---------------------------------------------------------------------------

#: Both guards below exempt the same two shapes, and only those two.
#: :func:`tests.ast_rules.assert_scan_is_not_vacuous` refuses an exemption
#: for anything not marked ``residue``, so this pair is the whole of what
#: either scan cannot reach.
_RESIDUE_EXEMPTIONS = {
    "partial_binding": (
        "residue: the binding's value is a call, so there is no name for "
        "construction_names to resolve and nothing in the tree says what "
        "_Partial holds"
    ),
    "cross_module_subclass": (
        "residue: the subclass is defined in another module, so a "
        "per-module resolution cannot see that _Exported reaches the "
        "subject at all"
    ),
}


def test_the_id_scan_sees_every_shape_in_the_shared_roster(tmp_path: Path) -> None:
    """The cross-rule half, run through :func:`_unescaped_id_renders`.

    The corpus above encodes what this rule *decides*; the shared roster
    encodes where a call can *hide*, which is knowledge no single rule
    author has — #488 shipped evadable because its synthetic tree carried
    only the spellings its own scan already handled.

    ``wrap`` supplies the composite this rule polices: a Rich render
    carrying a copyable handle. The roster supplies the handle, under every
    spelling (``pkg.doc_id()``, two levels of attribute), every binding
    (alias, rebinding, a chain, a walrus, an annotated assignment, a
    subclass) and every placement (``except`` body, nested function,
    lambda, module scope, class body, ``async def``, decorator, ``with``
    item, comprehension clause, keyword argument, default argument) — plus
    a second file, which is what makes the :func:`iter_modules` walk load
    bearing rather than incidental.
    """
    assert_scan_is_not_vacuous(
        lambda root: [
            int(violation.split(":")[1]) for violation in _unescaped_id_renders(root)
        ],
        subject="doc_id",
        kwarg="markup",
        wrap="console.print({call})",
        tmp_path=tmp_path,
        live_population=_render_population(),
        floor=_RENDER_FLOOR,
        exempt=_RESIDUE_EXEMPTIONS,
    )


def test_the_console_scan_sees_every_shape_in_the_shared_roster(
    tmp_path: Path,
) -> None:
    """The same guard for the emoji half.

    ``Console`` is an ordinary construction, which is the shape the roster
    was built around, so this one needs no ``wrap``: every shipped shape
    renders a direct ``Console(...)`` in a file that is not the factory,
    and every one of them must be reported. ``kwarg`` is supplied because
    the roster's judgement axis renders it, but this predicate does not
    read it — ``Console(emoji=None)`` and ``Console(**kwargs)`` are both
    offences here for the same reason a bare ``Console()`` is: the
    construction did not go through the door.
    """
    assert_scan_is_not_vacuous(
        lambda root: [
            int(site.split(":")[1])
            for site in _consoles_built_outside_the_factory(root)
        ],
        subject="Console",
        kwarg="emoji",
        tmp_path=tmp_path,
        live_population=_console_population(),
        floor=_CONSOLE_FLOOR,
        exempt=_RESIDUE_EXEMPTIONS,
    )


# ---------------------------------------------------------------------------
# The corruption is real, and the fix is what stops it
# ---------------------------------------------------------------------------

#: The line the #492 plan review ran through Rich verbatim.
_TRAP_LINE = "  - dataset:snowflake://db/schema/table: preview [document] tail"


def test_an_unprotected_render_really_does_mangle_the_id() -> None:
    """The negative control for the whole rule.

    A guard that never fails against a defective subject pins nothing.
    This is the defect reproduced against the installed Rich rather than
    quoted from the issue: if a future Rich stops substituting shortcodes
    or stops eating style tags, this test goes red and the rule above can
    be re-argued instead of carried forward on folklore. It builds a bare
    ``Console`` on purpose: the factory would fix half the defect, and
    what has to be shown here is the defect.
    """
    # A deliberately unprotected console: the defective control the rule
    # is measured against. The rule scans ``src/``, so building one here
    # is legal; the local import keeps the forbidden name out of this
    # module's import block, where a reader would mistake it for a use.
    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=200).print(_TRAP_LINE)
    out = buf.getvalue()

    assert "\N{SNOWFLAKE}" in out, out
    assert "dataset:snowflake://" not in out, out
    assert "[document]" not in out, out


def test_the_factory_and_an_escape_return_the_id_verbatim() -> None:
    """Both halves together, and neither alone.

    Three renders, because the two corruptions are independent: the
    factory's ``emoji=False`` fixes the shortcode and not the style tag,
    the escape fixes the style tag and not the shortcode, and only the
    pair reproduces the id an operator can copy.
    """
    # A deliberately unprotected console: the defective control the rule
    # is measured against. The rule scans ``src/``, so building one here
    # is legal; the local import keeps the forbidden name out of this
    # module's import block, where a reader would mistake it for a use.
    from rich.console import Console

    factory_only = io.StringIO()
    build_console(file=factory_only, force_terminal=False, width=200).print(_TRAP_LINE)
    assert "dataset:snowflake://" in factory_only.getvalue()
    assert "[document]" not in factory_only.getvalue(), "markup still eats the tag"

    escape_only = io.StringIO()
    Console(file=escape_only, force_terminal=False, width=200).print(escape(_TRAP_LINE))
    assert "[document]" in escape_only.getvalue()
    assert "\N{SNOWFLAKE}" in escape_only.getvalue(), "the shortcode substitutes"

    both = io.StringIO()
    build_console(file=both, force_terminal=False, width=200).print(escape(_TRAP_LINE))
    assert _TRAP_LINE.strip() in " ".join(both.getvalue().split()), both.getvalue()


def test_a_table_cell_inherits_the_console_setting() -> None:
    """Why the emoji fix belongs on the console and not on ``print``.

    ``policy list``, ``admin api-keys list`` and three ``metrics`` tables
    put ids in ``Table`` cells, which never see ``Console.print``'s
    ``emoji=`` argument — they render through the console's own setting. A
    per-call fix would have left every one of those tables mangled.
    """
    table = Table()
    table.add_column("id")
    table.add_row(escape("dataset:snowflake://db [document]"))

    buf = io.StringIO()
    build_console(file=buf, force_terminal=False, width=200).print(table)
    rendered = buf.getvalue()

    assert "dataset:snowflake://db" in rendered, rendered
    assert "[document]" in rendered, rendered
