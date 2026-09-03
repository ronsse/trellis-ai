"""The shared table the AST-derived rules argue over.

This repo derives invariants by walking the AST of ``src/`` (and, for the
subprocess rule, of ``tests/``). Four of those rules have now shipped
**evadable as written**, and the cause was identical every time: *the
vacuity guard divided by the scan's own output*, so a scan that merely
under-collected satisfied every guard it had.

===========  ==========================================================
rule         how the guard stayed green
===========  ==========================================================
#457         the scanner descended only ``ast.stmt`` and so never
             entered an ``ast.ExceptHandler`` — 148 sites became 123,
             and all three guards computed a ratio of 123/123.
#464         two "independent" counters shared file discovery, so
             narrowing it took 8 real sites to 6 and both shrank
             together.
#466         the roster floor was ``len(SITES) > 0``; three blind spots
             satisfied it.
#488         the scan matched only ``ast.Name``, so
             ``mod.PackBuilder(...)``, an aliased import and a subclass
             all walked past — and the synthetic tree the guard ran over
             carried only the spellings the scan already handled.
===========  ==========================================================

Each author re-derived the guard, and each got it wrong independently.
That is the signature of a missing shared primitive, not of four careless
authors — the same argument :mod:`tests.unreadable_paths` makes for
fixtures, transferred to scan predicates: suites asserting *opposite*
things should "argue over the same table rather than each inventing its
own".

What is shared here is the **mechanical layer only**: how a call target is
spelled, how a tree is walked, and which shapes are known to hide from a
scan. Every rule's *subject* stays its own, and so does every judgement it
encodes — several of those are prose for good reasons and no predicate
derives them.

Three properties are deliberate and worth stating, because getting any of
them wrong reproduces the defect this module exists to end.

**The floor stays per-rule and hand-read.** :func:`assert_hand_read_floor`
takes the number as an argument and refuses zero. A floor a scan can
compute for itself is #466 verbatim; no shared helper can supply one,
because the only thing that makes a floor mean anything is a person having
counted.

**Exemptions are declared, not implied.** A rule that cannot see through
an import alias says so, in code, with a reason
(:func:`assert_scan_is_not_vacuous`'s ``exempt``). Before this module the
blind spots were invisible: the rule simply never rendered the shape, and
the guard passed. An exemption is an *upper bound* on blindness — a rule
that turns out to catch an exempt shape still passes.

**The roster's own claims are checkable.** Each :class:`Evasion` declares
which of the :data:`NAIVE_SCANNERS` it defeats, and
``tests/unit/test_ast_rules.py`` verifies every one of those claims **by
running them**, then demonstrates that removing any single entry lets some
under-collecting predicate through. A shared vacuity harness that is
itself vacuous is strictly worse than none, because it launders
confidence.

**The class is not confined to AST rules, and #495 is the proof.** Found
the same night this module was written: 21 CLI tests fail under
``FORCE_COLOR=1``, and **four of them are tests written specifically to
prove Rich does not mangle operator output** — each asserting that a
recovery command an operator must copy survives rendering, each blind to
the coloured path, which is the path CI and a real terminal actually take.
A guard whose blind spot is the thing it was built to watch, with no AST
anywhere near it. So read the split here carefully when borrowing:
:data:`EVASIONS` is AST-specific and does not transfer, but the two
properties around it do. **A guard must be run against a deliberately
defective subject and watched to fail**, and **the population floor must
come from outside the measurement** — in #495's terms, from someone
noticing that every one of those four tests renders uncoloured. Colour is
#495's to fix and is deliberately out of scope here; the pattern is what
transfers.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "EVASIONS",
    "EVASION_IDS",
    "MARKER",
    "NAIVE_SCANNERS",
    "CallSite",
    "Evasion",
    "assert_hand_read_floor",
    "assert_scan_is_not_vacuous",
    "calls_named",
    "construction_sites",
    "is_call_to",
    "iter_modules",
    "marker_lines",
    "name_of",
    "render_evasion_module",
]


# ---------------------------------------------------------------------------
# The predicate every rule re-implemented
# ---------------------------------------------------------------------------


def name_of(node: ast.AST) -> str | None:
    """Bare name of a call target: ``f``, ``mod.f``, ``a.b.f`` all give ``f``.

    Lifted verbatim from ``tests/unit/test_policy_gate_rule.py``, which had
    it right; ``tests/unit/test_capture_surface_roster.py`` and
    ``tests/unit/test_subprocess_pythonpath_rule.py`` each solved the same
    sub-problem independently, and #488 was the site that needed it and got
    it wrong. Against the tree as it stands there are **563** class-like
    ``module.Attr(...)`` calls in ``src/`` versus 1,536 bare-name ones, so
    an ``ast.Name``-only predicate is blind to the repo's own second-most
    common call style — not to an exotic shape.

    Returns ``None`` for anything that is neither, which is the honest
    answer for ``f()()``'s inner call, a subscript, or a lambda: those
    targets have no *name*, and inventing one for them is how an
    over-collecting predicate starts.
    """
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def is_call_to(node: ast.AST, name: str) -> bool:
    """Is *node* a call whose target's bare name is *name*?

    Matching the trailing name over-collects at worst — a same-named method
    on an unrelated object — and over-collecting costs a spurious
    classification while under-collecting costs an unwatched site. That
    trade is the one ``test_capture_surface_roster`` already wrote down; it
    holds here for the same reason.
    """
    return isinstance(node, ast.Call) and name_of(node.func) == name


def calls_named(name: str, tree: ast.AST) -> list[ast.Call]:
    """Every call to *name* anywhere in *tree*, in source order.

    ``ast.walk`` rather than a descent over statements: #457's scanner
    descended only ``ast.stmt`` and so never entered an
    ``ast.ExceptHandler``. ``walk`` visits every node regardless of its
    kind, which is why :data:`EVASIONS` carries ``inside_except``,
    ``nested_function`` and ``lambda_body`` — so that any rule adopting
    this helper has *pinned* the property rather than inherited it by luck.
    """
    return [node for node in ast.walk(tree) if is_call_to(node, name)]


def iter_modules(root: Path) -> Iterator[tuple[Path, ast.Module]]:
    """Every ``*.py`` under *root*, parsed, in path order.

    One walker, so a rule's two counters cannot drift apart the way #464's
    did — but note what that does *not* buy: two counters sharing this
    discovery still shrink together if it narrows. That is why a
    cross-check worth having (``test_capture_surface_roster``'s tokenizer
    scan) does not share the subject's method, and why the floor below is
    hand-read rather than derived.
    """
    for path in sorted(root.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class CallSite:
    """One call, and the file it was found in."""

    path: Path
    node: ast.Call

    @property
    def lineno(self) -> int:
        return self.node.lineno

    def describe(self, root: Path | None = None) -> str:
        where = self.path.relative_to(root) if root is not None else self.path.name
        snippet = ast.unparse(self.node).replace("\n", " ")[:90]
        return f"{where}:{self.lineno}: {snippet}"


def construction_sites(cls: str, root: Path) -> list[CallSite]:
    """Every ``cls(...)`` under *root*, however the target is spelled.

    "Construction" is the common case; nothing here is specific to classes
    — a rule policing calls to a *function* uses the same shape, which is
    why :func:`calls_named` is exposed separately for callers that already
    hold a tree.
    """
    return [
        CallSite(path=path, node=node)
        for path, tree in iter_modules(root)
        for node in calls_named(cls, tree)
    ]


# ---------------------------------------------------------------------------
# The roster of known evasion shapes
# ---------------------------------------------------------------------------

#: Prefix of the comment that marks the one line of an evasion a scan must
#: report. Machine-readable on purpose: the id rides in the comment, so a
#: failure names the *shape* rather than a line number, and the marker can
#: be recovered by tokenizing as well as by parsing.
MARKER = "#!evade:"


@dataclass(frozen=True)
class Evasion:
    """One way a call site hides from — or is waved through by — a scan.

    Attributes:
        id: Names the shape, so a failure says ``aliased_import`` rather
            than ``line 34``.
        why: What makes it invisible. Read by the failure message, so the
            author of the next rule is told what they missed rather than
            that they missed something.
        call: The call expression, with ``{SUBJECT}``, ``{ARGS}`` and
            ``{KWARG}`` placeholders, so one roster serves rules policing
            different names.
        context: The lines the call sits in. Exactly one contains
            ``{STMT}``, at the indentation it needs; that is the line the
            marker lands on and the line a scan must report.
        preamble: Module-level lines this shape needs (an aliasing import,
            a subclass definition). Hoisted above every context so the
            rendered module parses as one file.
        missed_by: Ids of the :data:`NAIVE_SCANNERS` this shape defeats.
            **Verified by execution** in ``tests/unit/test_ast_rules.py``,
            not trusted — a declared miss that is not a real one is the
            same class of rot as a hand-maintained roster.
    """

    id: str
    why: str
    call: str
    context: tuple[str, ...]
    preamble: tuple[str, ...] = ()
    missed_by: frozenset[str] = frozenset()


#: Every shape known to walk past an AST rule in this repo, plus the
#: control. Three of them — ``aliased_import``, ``local_rebinding`` and
#: ``subclass_then_construct`` — are defeated by *every* predicate here
#: including the correct one: resolving a binding is not something a
#: single-file AST walk does. They are in the roster anyway, and that is
#: the point: a rule adopting :func:`assert_scan_is_not_vacuous` must
#: either catch them or say in code why it does not, where before this
#: module the blind spot was simply never rendered.
EVASIONS: tuple[Evasion, ...] = (
    Evasion(
        id="bare_call",
        why=(
            "the control: the one spelling every scan in this repo already "
            "handles. Present so a predicate that reports nothing at all "
            "fails loudly rather than by arithmetic."
        ),
        call="{SUBJECT}({ARGS})",
        context=("def _bare_call():", "    {STMT}"),
        missed_by=frozenset(),
    ),
    Evasion(
        id="attribute_call",
        why=(
            "reached through its module (`mod.Subject(...)`). An "
            "`ast.Name`-only match walks straight past it, which is #488 "
            "verbatim and covers 563 class-like calls in src/."
        ),
        call="pkg.{SUBJECT}({ARGS})",
        context=("def _attribute_call():", "    {STMT}"),
        missed_by=frozenset({"name_only"}),
    ),
    Evasion(
        id="dotted_attribute",
        why=(
            "two levels of attribute access. A predicate that reads "
            "`func.value.id` to check the receiver — rather than the "
            "trailing `func.attr` — sees `sub` and gives up."
        ),
        call="pkg.sub.{SUBJECT}({ARGS})",
        context=("def _dotted_attribute():", "    {STMT}"),
        missed_by=frozenset({"name_only"}),
    ),
    Evasion(
        id="aliased_import",
        why=(
            "bound under another name at import time. No single-file AST "
            "walk resolves this, so a rule must close it another way or "
            "declare it — silently not rendering the shape is how it "
            "stayed invisible."
        ),
        call="_Aliased({ARGS})",
        preamble=("from pkg import {SUBJECT} as _Aliased",),
        context=("def _aliased_import():", "    {STMT}"),
        missed_by=frozenset(
            {
                "name_only",
                "stmt_descent",
                "shallow",
                "kwarg_presence",
                "splat_tolerant",
            }
        ),
    ),
    Evasion(
        id="local_rebinding",
        why=(
            "rebound to a local name one line before the call "
            "(`PB = PackBuilder; PB(...)`). Same blind spot as the alias, "
            "reachable without touching the imports."
        ),
        call="_Rebound({ARGS})",
        context=(
            "def _local_rebinding():",
            "    _Rebound = {SUBJECT}",
            "    {STMT}",
        ),
        missed_by=frozenset(
            {
                "name_only",
                "stmt_descent",
                "shallow",
                "kwarg_presence",
                "splat_tolerant",
            }
        ),
    ),
    Evasion(
        id="subclass_then_construct",
        why=(
            "constructed under the subclass's own name, reaching the "
            "policed constructor through `super().__init__(...)`, whose "
            "call target is `__init__`."
        ),
        call="_Subclassed({ARGS})",
        preamble=("class _Subclassed({SUBJECT}):", "    pass"),
        context=("def _subclass_then_construct():", "    {STMT}"),
        missed_by=frozenset(
            {
                "name_only",
                "stmt_descent",
                "shallow",
                "kwarg_presence",
                "splat_tolerant",
            }
        ),
    ),
    Evasion(
        id="kwargs_splat",
        why=(
            "wiring passed as `**kwargs`, which is opaque to a static "
            "scan. A judgement that treats the splat as 'probably fine' "
            "is the obvious way a rule is turned off without being "
            "deleted."
        ),
        call="{SUBJECT}(**_kwargs)",
        context=("def _kwargs_splat(**_kwargs):", "    {STMT}"),
        missed_by=frozenset({"splat_tolerant"}),
    ),
    Evasion(
        id="none_keyword",
        why=(
            "the keyword is present and its value is `None`. A "
            "presence-only judgement reads as satisfied while the runtime "
            "guard it feeds stays false — the obvious way a future edit "
            "'fixes' the rule without fixing anything."
        ),
        call="{SUBJECT}({KWARG}=None)",
        context=("def _none_keyword():", "    {STMT}"),
        missed_by=frozenset({"kwarg_presence"}),
    ),
    Evasion(
        id="inside_except",
        why=(
            "#457's own shape. `ast.ExceptHandler` is not an `ast.stmt`, "
            "so a descent over statements never enters it and every site "
            "in an error path vanishes from the population the guards "
            "divide by."
        ),
        call="{SUBJECT}({ARGS})",
        context=(
            "def _inside_except():",
            "    try:",
            "        pass",
            "    except ValueError:",
            "        {STMT}",
        ),
        missed_by=frozenset({"stmt_descent"}),
    ),
    Evasion(
        id="nested_function",
        why=(
            "a closure defined inside the function that returns it. A "
            "scan that reads a module's top level and one level of "
            "function bodies stops exactly here."
        ),
        call="{SUBJECT}({ARGS})",
        context=(
            "def _nested_function():",
            "    def _inner():",
            "        {STMT}",
            "    return _inner",
        ),
        missed_by=frozenset({"shallow"}),
    ),
    Evasion(
        id="lambda_body",
        why=(
            "an expression body, which carries no statement for a "
            "statement-shaped scan to land on."
        ),
        call="{SUBJECT}({ARGS})",
        context=("def _lambda_body():", "    return lambda: {STMT}"),
        missed_by=frozenset({"shallow"}),
    ),
)

#: Ids in roster order, so a caller can name a subset without re-listing.
EVASION_IDS: tuple[str, ...] = tuple(evasion.id for evasion in EVASIONS)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_HEADER = (
    "from __future__ import annotations",
    "",
    "import pkg",
    "import pkg.sub",
    "from pkg import {SUBJECT}",
    "",
)


def _substitute(line: str, *, subject: str, args: str, kwarg: str) -> str:
    return line.format(SUBJECT=subject, ARGS=args, KWARG=kwarg, STMT="{STMT}")


def render_evasion_module(
    evasions: Sequence[Evasion] = EVASIONS,
    *,
    subject: str,
    args: str = "",
    kwarg: str = "gate",
    wrap: str = "{call}",
) -> tuple[str, dict[str, int]]:
    """Render *evasions* as one parseable module, with each shape's line.

    *wrap* lets a rule whose offence is a *composite* — ``console.print``
    carrying a serialized payload, say — reuse the spelling and placement
    axes without the roster having to model its subject. The roster
    supplies the call; the rule supplies what surrounds it.

    Returns the source and ``{evasion id: line number}``. Line numbers are
    computed from the assembled text rather than written down beside it,
    so removing an entry reflows the module without invalidating anything
    — the hand-written line tables in the existing rules are the thing
    that stops a roster from being edited.
    """
    lines: list[str] = [
        _substitute(line, subject=subject, args=args, kwarg=kwarg) for line in _HEADER
    ]
    for evasion in evasions:
        if not evasion.preamble:
            continue
        lines.extend(
            _substitute(line, subject=subject, args=args, kwarg=kwarg)
            for line in evasion.preamble
        )
        lines.append("")

    at: dict[str, int] = {}
    for evasion in evasions:
        call = _substitute(evasion.call, subject=subject, args=args, kwarg=kwarg)
        statement = f"{wrap.format(call=call)}  {MARKER}{evasion.id}"
        for line in evasion.context:
            rendered = _substitute(line, subject=subject, args=args, kwarg=kwarg)
            if "{STMT}" in rendered:
                at[evasion.id] = len(lines) + 1
                rendered = rendered.replace("{STMT}", statement)
            lines.append(rendered)
        lines.append("")

    return "\n".join(lines) + "\n", at


def marker_lines(source: str) -> dict[str, int]:
    """Recover ``{evasion id: line}`` from rendered *source* by tokenizing.

    The independent half of the harness's own cross-check, and the reason
    :data:`MARKER` is a machine-readable comment rather than prose.
    Tokenizing builds no tree, so a tree-shaped bug in
    :func:`render_evasion_module`'s accounting cannot reach it — the only
    kind of cross-check worth having is one that does not share the
    subject's method. Comments tokenize as ``COMMENT`` and never as
    ``NAME``, so a marker discussed inside a docstring is excluded
    structurally rather than by pattern.
    """
    found: dict[str, int] = {}
    stream = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in stream:
        if token.type != tokenize.COMMENT:
            continue
        text = token.string.strip()
        if not text.startswith(MARKER):
            continue
        found[text[len(MARKER) :].strip()] = token.start[0]
    return found


# ---------------------------------------------------------------------------
# The naive scanners the roster is measured against
# ---------------------------------------------------------------------------
#
# Each is a real under-collection or over-permission shape that has shipped
# in this repo. They exist so that every ``Evasion.missed_by`` claim is
# checkable by execution rather than by comment, and so a new rule can
# assert it beats them.


def _naive_name_only(source: str, subject: str, kwarg: str) -> set[int]:
    """#488: matches only ``ast.Name``, so every attribute call escapes."""
    return {
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == subject
    }


def _statements(node: ast.AST) -> Iterator[ast.stmt]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.stmt):
            yield child
            yield from _statements(child)


def _own_expression_calls(statement: ast.stmt) -> Iterator[ast.Call]:
    stack: list[ast.AST] = [
        child
        for child in ast.iter_child_nodes(statement)
        if not isinstance(child, (ast.stmt, ast.ExceptHandler))
    ]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Call):
            yield node
        stack.extend(
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, (ast.stmt, ast.ExceptHandler))
        )


def _naive_stmt_descent(source: str, subject: str, kwarg: str) -> set[int]:
    """#457: descends only ``ast.stmt``, so ``except`` bodies vanish."""
    tree = ast.parse(source)
    return {
        call.lineno
        for statement in _statements(tree)
        for call in _own_expression_calls(statement)
        if name_of(call.func) == subject
    }


def _shallow_calls(node: ast.AST, depth: int) -> Iterator[ast.Call]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Lambda):
            continue
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if depth >= 1:
                continue
            yield from _shallow_calls(child, depth + 1)
            continue
        if isinstance(child, ast.Call):
            yield child
        yield from _shallow_calls(child, depth)


def _naive_shallow(source: str, subject: str, kwarg: str) -> set[int]:
    """Reads a module's top level and one level of bodies, nothing deeper."""
    return {
        call.lineno
        for call in _shallow_calls(ast.parse(source), 0)
        if name_of(call.func) == subject
    }


def _naive_kwarg_presence(source: str, subject: str, kwarg: str) -> set[int]:
    """Correct collection, but judges on the keyword being *present*."""
    return {
        node.lineno
        for node in ast.walk(ast.parse(source))
        if is_call_to(node, subject)
        and kwarg not in {kw.arg for kw in node.keywords if kw.arg}
    }


def _naive_splat_tolerant(source: str, subject: str, kwarg: str) -> set[int]:
    """Correct collection, but treats ``**kwargs`` as satisfying the rule."""
    reported: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not is_call_to(node, subject):
            continue
        if any(kw.arg is None for kw in node.keywords):
            continue
        wired = any(
            kw.arg == kwarg
            and not any(
                isinstance(sub, ast.Constant) and sub.value is None
                for sub in ast.walk(kw.value)
            )
            for kw in node.keywords
        )
        if not wired:
            reported.add(node.lineno)
    return reported


#: Named under-collecting / over-permitting predicates, each a shape that
#: really shipped. ``Evasion.missed_by`` names members of this mapping.
NAIVE_SCANNERS: Mapping[str, Callable[[str, str, str], set[int]]] = {
    "name_only": _naive_name_only,
    "stmt_descent": _naive_stmt_descent,
    "shallow": _naive_shallow,
    "kwarg_presence": _naive_kwarg_presence,
    "splat_tolerant": _naive_splat_tolerant,
}


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------

#: An exemption reason shorter than this is not a reason. #443 declared
#: three control keys against six sites and #466 floored a roster at
#: ``> 0``; both read as decisions and neither was one.
_MIN_REASON_CHARS = 20


def assert_hand_read_floor(
    population: int,
    floor: int,
    *,
    subject: str,
    hint: str = "",
) -> None:
    """The population the rule reasons over is at least *floor*.

    *floor* is an argument and not a computation, and that is the whole
    design: #466's floor was ``len(SITES) > 0``, which three blind spots
    satisfied. A number a person read off the tree is the only thing a
    silently-narrowing scan cannot also satisfy, so zero and one are
    refused outright — a "floor" of one is satisfied by any scan that
    still matches a single thing.

    It is a floor rather than an equality on purpose: adding a new
    compliant site is ordinary and must not turn the suite red. What must
    turn it red is the scan finding *less than a person counted*.
    """
    assert floor >= 2, (
        f"a floor of {floor} for {subject} is not a floor: it is satisfied "
        f"by a scan that has stopped matching almost everything (#466 "
        f"shipped `len(SITES) > 0`). Count the sites by hand and write the "
        f"number down."
    )
    assert population >= floor, (
        f"the scan found {population} {subject} site(s), below the "
        f"hand-read floor of {floor}. Either the scan has drifted and is "
        f"no longer policing anything, or the tree really shrank and the "
        f"floor needs re-reading against it. {hint}".rstrip()
    )


def assert_scan_is_not_vacuous(
    shipped_predicate: Callable[[Path], Iterable[int]],
    evasions: Sequence[Evasion] = EVASIONS,
    *,
    subject: str,
    tmp_path: Path,
    live_population: int,
    floor: int,
    args: str = "",
    kwarg: str = "gate",
    wrap: str = "{call}",
    exempt: Mapping[str, str] | None = None,
) -> None:
    """Run the **shipped** predicate over the roster, and floor its population.

    Args:
        shipped_predicate: ``(root) -> line numbers reported``. Must be the
            function the rule itself calls, not a copy: a guard run against
            a re-implementation leaves the shipped one free to regress with
            the suite green, which is the failure ``test_machine_output_rule``
            recorded and fixed.
        evasions: The roster. Defaults to all of :data:`EVASIONS`; pass a
            subset only when a shape is not expressible for the subject.
        subject: The name the rule polices, substituted into every shape.
        tmp_path: pytest's, so the rendered module is thrown away.
        live_population: What the shipped scan finds in the real tree.
        floor: Hand-read. See :func:`assert_hand_read_floor`.
        args / kwarg / wrap: Rendering knobs — see
            :func:`render_evasion_module`.
        exempt: ``{evasion id: why this rule cannot catch it}``. An
            exemption is an upper bound on blindness, not a requirement to
            be blind: a rule that catches an exempt shape still passes.
            Unknown ids and one-word reasons are refused, because an
            exemption roster rots exactly like every other roster here.

    Two things this asserts that a hand-rolled guard usually does not.
    Every non-exempt shape must be **reported**, named by shape rather than
    by line — and nothing *unmarked* may be reported, which is what stops a
    predicate that returns every line number in the file from passing the
    first check trivially.
    """
    assert_hand_read_floor(
        live_population,
        floor,
        subject=subject,
        hint="assert_scan_is_not_vacuous cannot supply this number for you.",
    )

    declared = dict(exempt or {})
    by_id = {evasion.id: evasion for evasion in evasions}
    unknown = sorted(set(declared) - set(by_id))
    assert not unknown, (
        f"exempt names {unknown}, which are not shapes in this roster "
        f"(known: {sorted(by_id)}). A stale exemption silences a shape "
        f"nobody is checking."
    )
    thin = sorted(
        name
        for name, reason in declared.items()
        if len(reason.strip()) < _MIN_REASON_CHARS
    )
    assert not thin, (
        f"the exemption(s) for {thin} give no reason. Say what closes the "
        f"shape instead — another test, an inherent limit of a single-file "
        f"walk — or drop the exemption."
    )

    source, at = render_evasion_module(
        evasions, subject=subject, args=args, kwarg=kwarg, wrap=wrap
    )
    directory = tmp_path / f"ast_rules_{subject}"
    suffix = 0
    while directory.exists():
        suffix += 1
        directory = tmp_path / f"ast_rules_{subject}_{suffix}"
    directory.mkdir(parents=True)
    (directory / "evasions.py").write_text(source, encoding="utf-8")

    reported = set(shipped_predicate(directory))
    required = {name: line for name, line in at.items() if name not in declared}

    missed = sorted(name for name, line in required.items() if line not in reported)
    assert not missed, (
        f"the shipped scan for {subject} does not report "
        f"{missed}.\n"
        + "\n".join(f"  {name}: {by_id[name].why}" for name in missed)
        + "\n\nEither widen the scan, or exempt the shape with a reason "
        "saying what closes it instead. A blind spot nobody wrote down is "
        "how the last four of these shipped."
    )

    spurious = sorted(reported - set(at.values()))
    assert not spurious, (
        f"the shipped scan for {subject} reported line(s) {spurious} that "
        f"carry no {MARKER} marker. A predicate that reports everything "
        f"satisfies the check above without seeing anything, so the "
        f"rendered module's unmarked lines are asserted clean too."
    )
