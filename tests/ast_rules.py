"""The shared table the AST-derived rules argue over.

This repo derives invariants by walking the AST of ``src/`` (and, for the
subprocess rule, of ``tests/``). Four of those rules have shipped
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
spelled, which local names resolve to the policed one, how a tree is
walked, and which shapes are known to hide from a scan. Every rule's
*subject* stays its own, and so does every judgement it encodes — several
of those are prose for good reasons and no predicate derives them.

Four properties are deliberate, because getting any of them wrong
reproduces the defect this module exists to end.

**Blindness is the last resort, not the default.** The first cut of this
module declared three shapes — import alias, local rebinding, subclass —
unresolvable by "a single-file AST walk", and required every adopting rule
to exempt them. That was **false**, and refuted by code already on
``main``: ``tests/unit/retrieve/test_builder_factory.py::_construction_names``
resolves exactly those three, in one file, iterated to a fixed point, and
it was written for **#488 — the very rule this issue was filed about**. A
harness that institutionalises three mandatory exemptions teaches every
future rule to declare blindness instead of lifting a resolver that
already exists, which is #490's thesis inverted. That resolver is now
:func:`construction_names` and lives here; the exemptions are gone. What
remains is **residue that is demonstrated rather than asserted** —
``partial_binding`` and ``cross_module_subclass``, the only two shapes in
:data:`EVASIONS` that no scanner here reports, the resolving one included.

**The floor stays per-rule and hand-read.** :func:`assert_hand_read_floor`
takes the number as an argument. A floor a scan can compute for itself is
#466 verbatim — and note *why* #466's floor was wrong: not because it was
small, but because the scan computed it. A genuinely single-site rule is
legitimate and says so with ``sole_site_reason``.

**Exemptions are bounded, and cannot empty the roster.** The control
cannot be exempted; a shape that is not ``residue`` cannot be exempted,
because the answer to a resolvable blind spot is to resolve it; a reason
must be prose; and every axis present in the roster a rule passes must
keep at least one *required* shape. Without those bounds a rule exempting
all eleven shapes passed the guard while reporting nothing — verified by
the #497 review gate, not feared.

**And narrowing the roster is exempting, so it is bounded the same way.**
Every bound above is on ``exempt=``; the #497 *re*-gate walked past all
four by passing ``evasions=`` instead — ``evasions=[]`` with a predicate
that reported nothing cleared the guard, and so did ``evasions=[bare_call]``
against #488's own ``ast.Name``-only scan, the exact predicate this module
exists to reject. Nothing about either looked like an exemption. Dropping
a shape and exempting one are the same act with the same consequence, so
the cheaper spelling must not be the unbounded one:
:func:`_validate_roster` refuses an empty roster outright and requires a
written ``roster_reason`` for any other narrowing. It is a *reason* rather
than the residue rule, because a rule whose subject cannot render a shape
at all is real — the roster models a call, not every rule's offence — but
it has to say so in a sentence, in the diff.

**The roster's own claims are checkable.** Each :class:`Evasion` declares
which of the :data:`NAIVE_SCANNERS` it defeats, and
``tests/unit/test_ast_rules.py`` verifies every one of those claims **by
running them**, then demonstrates that removing any single entry lets some
under-collecting predicate through. A shared vacuity harness that is
itself vacuous is strictly worse than none, because it launders
confidence.

**What is *not* in the roster, and the rule for adding it.** A shape earns
its place only when some scanner in :data:`NAIVE_SCANNERS` actually misses
it; otherwise it inflates the roster without strengthening any guard, and
``test_ast_rules`` fails it. #501 proposed four placements against that
rule and the rule disposed of three of them, which is the clearest thing
anyone has yet said in its favour.

*Measured over the 16,023 calls in* ``src/`` *at* ``8ec879c``. Run every
shape through all the scanners before writing a word about it:

* A call in a **comprehension element** (``[Subject() for x in y]``, 321
  calls) is an ordinary ``ast.expr`` child and **every scanner here
  reports it**. Not a shape. The half that does hide is the
  comprehension's ``iter``/``ifs`` *clause* (315 calls), because
  ``ast.comprehension`` is neither a statement nor an expression — so
  ``comprehension_clause`` is the entry, and the 612 calls with any
  comprehension ancestor were never one population. (No count of this is
  worth quoting without its definition: 612 distinct calls under a
  comprehension, 650 by a walk that double-counts nested ones, 321 in the
  element and 315 in the clause overlapping through nesting. #501 and an
  earlier draft of this docstring disagreed at 633 and 635 and neither
  said which it meant.)
* A call **inside a walrus** (``if (x := Subject()):``) is likewise plain
  expression ground that nothing misses. The walrus hides on the
  **binding** axis instead — ``S := Subject`` then ``S(...)`` — which is
  also what the live evidence said, since the local fix that motivated
  #501 is a *name resolver*. ``walrus_rebinding`` and its annotated
  sibling are the entries, and neither needed a new scanner: all eight
  scanners that predate this change already miss them.
* ``with`` **items** (167) do hide, and needed the ninth scanner —
  :func:`_naive_typed_descent`, the repair a #457 author writes.
* ``match`` needed **nothing**: ``src/`` holds 0 ``ast.Match`` nodes, so
  there is nothing for a scanner to miss. That is evidence *for* the rule
  and is recorded as such rather than filled.

The one new scanner brought three more placements with it, and the roster
carries all five members of the family it is blind to rather than
mentioning the leftovers here — a gap written into a docstring instead of
into the roster is the artefact #501 was filed about.

**The corpus is flat, and the discovery axis is thinner than it looks.**
:data:`SECOND_FILE` is a sibling of :data:`PRIMARY_FILE`, so
``single_file`` is probed but ``glob`` versus ``rglob`` is not, and
neither is a walk narrowed to a subdirectory. :func:`iter_modules` is
covered, but by the hand-read floors rather than by the roster. A third
corpus file should therefore live in a **subdirectory**, with a
discovery-narrowing scanner beside it. That is deliberately not done here:
every adopter's predicate, and this suite's own :func:`_perfect`
reference, discovers with ``root.glob("*.py")``, so moving a shape into a
subdirectory would fail four rules at once for a reason that has nothing
to do with any of them. It is a change to the corpus contract, not an
entry in the roster.

**The class is not confined to AST rules, and #495 is the proof.** Found
the same night this module was written: 21 CLI tests fail under
``FORCE_COLOR=1``, and **four of them are tests written specifically to
prove Rich does not mangle operator output** — each asserting that a
recovery command an operator must copy survives rendering, each blind to
the coloured path, which is the path CI and a real terminal actually take.
A guard whose blind spot is the thing it was built to watch, with no AST
anywhere near it. So read the split carefully when borrowing:
:data:`EVASIONS` is AST-specific and does not transfer, but the two
properties around it do. **A guard must be run against a deliberately
defective subject and watched to fail**, and **the population floor must
come from outside the measurement**. Colour is #495's to fix and is
deliberately out of scope here; the pattern is what transfers.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "AXES",
    "DECOYS",
    "DECOY_MARKER",
    "EVASIONS",
    "EVASION_IDS",
    "MARKER",
    "NAIVE_SCANNERS",
    "PRIMARY_FILE",
    "SECOND_FILE",
    "CallSite",
    "Decoy",
    "Evasion",
    "RenderedCorpus",
    "assert_hand_read_floor",
    "assert_scan_is_not_vacuous",
    "calls_named",
    "calls_to_any",
    "construction_names",
    "construction_sites",
    "decoy_lines",
    "is_call_to",
    "iter_modules",
    "marker_lines",
    "name_of",
    "render_evasion_corpus",
]


# ---------------------------------------------------------------------------
# The predicates every rule re-implemented
# ---------------------------------------------------------------------------


def name_of(node: ast.AST) -> str | None:
    """Bare name of a call target: ``f``, ``mod.f``, ``a.b.f`` all give ``f``.

    Lifted from ``tests/unit/test_policy_gate_rule.py``, which had it
    right; ``test_capture_surface_roster.py``, ``test_builder_factory.py``
    and ``test_subprocess_pythonpath_rule.py`` each solved the same
    sub-problem independently, and #488 was the site that needed it and got
    it wrong. Measured 2026-09-03: ``src/`` writes **563** class-like
    ``module.Attr(...)`` calls against 1,536 bare-name ones, so an
    ``ast.Name``-only predicate is blind to the repo's own second-most
    common call style — not to an exotic shape.

    Returns ``None`` for anything that is neither, which is the honest
    answer for ``f()()``'s outer target, a subscript, or a lambda: those
    have no *name*, and inventing one is how an over-collecting predicate
    starts.
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
    classification while under-collecting costs an unwatched site.
    """
    return isinstance(node, ast.Call) and name_of(node.func) == name


def construction_names(subject: str, tree: ast.AST) -> set[str]:
    """Every local name in *tree* that reaches *subject*.

    Adapted from ``test_builder_factory.py::_construction_names``, written
    for #488 and merged as ``268daa9``. Five rebindings produce the
    subject under another name and would otherwise walk past a literal
    match:

    * ``from pkg import Subject as Alias`` → ``Alias``
    * ``S = Subject`` → ``S``
    * ``S: type[Subject] = Subject`` → ``S``
    * ``if (S := Subject) is not None:`` → ``S``
    * ``class Mine(Subject)`` → ``Mine``, because a subclass constructor
      runs the same ``__init__`` and takes the same argument list

    The annotated and walrus forms are #501's, and they were read off a
    *local* fix rather than invented: ``test_machine_output_rule``'s
    ``_serialized_names`` already handles ``ast.AnnAssign`` and
    ``ast.NamedExpr``, in one ``isinstance`` tuple, in the module that is
    now a roster adopter. Someone had been bitten by the walrus and closed
    it where it bit them, while the shared resolver every other rule
    inherits still read ``ast.Assign`` alone. That is the whole of #501's
    argument: a fix that stays local is a blind spot that only looks
    closed. Both are pinned by the ``walrus_rebinding`` and
    ``annassign_rebinding`` shapes, which all nine naive scanners miss.

    Iterated to a fixed point, so a chain (``A = Subject``; ``B = A``) is
    caught — pinned by the ``rebinding_chain`` shape, which that loop is
    the only thing that catches.

    **What it cannot do, demonstrated rather than asserted.** A binding
    whose value is a *call* (``functools.partial(Subject)``) has no name to
    read, and a subclass defined in *another* module is not in this tree at
    all. Those are :data:`EVASIONS`' two residue shapes; every scanner
    here misses them, which is why a rule may exempt exactly those two and
    nothing else.
    """
    names = {subject}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(
                alias.asname
                for alias in node.names
                if alias.name == subject and alias.asname
            )
    changed = True
    while changed:
        rebound: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and name_of(node.value) in names:
                rebound.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif (
                isinstance(node, (ast.AnnAssign, ast.NamedExpr))
                and node.value is not None
                and name_of(node.value) in names
                and isinstance(node.target, ast.Name)
            ):
                rebound.add(node.target.id)
            elif isinstance(node, ast.ClassDef) and any(
                name_of(base) in names for base in node.bases
            ):
                rebound.add(node.name)
        changed = not rebound <= names
        names |= rebound
    return names


def calls_to_any(names: Collection[str], tree: ast.AST) -> list[ast.Call]:
    """Every call in *tree* whose target's bare name is in *names*.

    ``ast.walk`` rather than a descent over statements: #457's scanner
    descended only ``ast.stmt`` and so never entered an
    ``ast.ExceptHandler``. ``walk`` visits every node regardless of kind,
    which is why the roster carries ``inside_except``, ``nested_function``,
    ``lambda_body``, ``module_scope``, ``class_body``, ``async_function``
    and ``decorator`` — so that a rule adopting this helper has *pinned*
    the property rather than inherited it by luck.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and name_of(node.func) in names
    ]


def calls_named(name: str, tree: ast.AST, *, resolve: bool = False) -> list[ast.Call]:
    """Every call to *name* in *tree*.

    ``resolve=True`` follows aliases, rebindings and subclasses through
    :func:`construction_names` first. Pass a whole module for that —
    resolution over a function subtree sees only that function's bindings,
    which is conservative but narrower than a rule usually wants.
    """
    names = construction_names(name, tree) if resolve else {name}
    return calls_to_any(names, tree)


def iter_modules(root: Path) -> Iterator[tuple[Path, ast.Module]]:
    """Every ``*.py`` under *root*, parsed, in path order.

    One walker, so a rule's two counters cannot drift apart the way #464's
    did — but note what that does *not* buy: two counters sharing this
    discovery still shrink together if it narrows. That is why the roster
    carries shapes in a *second file*, and why the floor below is
    hand-read.
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


def construction_sites(cls: str, root: Path, *, resolve: bool = True) -> list[CallSite]:
    """Every ``cls(...)`` under *root*, however the target is spelled or bound.

    Resolving by default is the whole lesson of #488: the bare-name-only
    version reproduced *inside the rule* the defect the rule removed from
    the code.
    """
    sites: list[CallSite] = []
    for path, tree in iter_modules(root):
        names = construction_names(cls, tree) if resolve else {cls}
        sites.extend(
            CallSite(path=path, node=node) for node in calls_to_any(names, tree)
        )
    return sites


# ---------------------------------------------------------------------------
# The roster of known evasion shapes
# ---------------------------------------------------------------------------

#: Prefix of the comment marking the one line of a shape a scan must
#: report. Machine-readable on purpose: the id rides in the comment, so a
#: failure names the *shape*, and the table can be recovered by tokenizing
#: as well as by parsing.
MARKER = "#!evade:"

#: Prefix marking a call that must **never** be reported. The first cut of
#: this module had none, and "a predicate that reports every line is
#: refused" was true only because every ``ast.Call`` in the corpus sat on a
#: marked line — literally true and materially misleading. #488's own
#: corpus ships a negative control; this one now does too.
DECOY_MARKER = "#!decoy:"

#: The two files the corpus renders to. Two, not one, because #464's
#: defect was file *discovery*: a scan narrowed to a single filename
#: satisfies every guard computed over its own output.
PRIMARY_FILE = "evasions.py"
SECOND_FILE = "evasions_second.py"

#: The axes the roster models. Exemptions may not empty one — see
#: :func:`_validate_exemptions`.
AXES: tuple[str, ...] = (
    "spelling",
    "binding",
    "placement",
    "judgement",
    "discovery",
)


@dataclass(frozen=True)
class Evasion:
    """One way a call site hides from — or is waved through by — a scan.

    Attributes:
        id: Names the shape, so a failure says ``aliased_import`` rather
            than ``line 34``.
        axis: Which kind of blindness it probes. Every axis present in a
            roster must keep at least one required shape.
        why: What makes it invisible, and it must be **true of what is
            rendered** — an earlier version described reaching a
            constructor through ``super().__init__(...)`` in a corpus
            containing no such call, in a module whose thesis is that
            reasons are executed rather than read.
        call: The call expression, with ``{SUBJECT}``, ``{ARGS}`` and
            ``{KWARG}`` placeholders, so one roster serves rules policing
            different names.
        context: The lines the call sits in. Exactly one contains
            ``{STMT}``, at the indentation it needs.
        preamble: Module-level lines this shape needs. Always rendered into
            :data:`PRIMARY_FILE`, even for a shape whose context lives in
            the second file — that is what makes ``cross_module_subclass``
            a genuine cross-module reference.
        file: Which file the context renders into.
        missed_by: Ids of the :data:`NAIVE_SCANNERS` this shape defeats.
            **Verified by execution**, not trusted.
        residue: True when *no* scanner here reports it, the resolving one
            included. Only a residue shape may be exempted by a rule;
            everything else is a resolvable blind spot whose answer is to
            resolve it.
    """

    id: str
    axis: str
    why: str
    call: str
    context: tuple[str, ...]
    preamble: tuple[str, ...] = ()
    file: str = PRIMARY_FILE
    missed_by: frozenset[str] = field(default_factory=frozenset)
    residue: bool = False


@dataclass(frozen=True)
class Decoy:
    """A call that is *not* a construction of the subject.

    Without these the "nothing unmarked was reported" check is vacuous:
    every ``ast.Call`` in a roster-only corpus sits on a marked line, so a
    predicate reporting every call line passes. Each decoy names the
    predicate error it catches.

    ``{WRAPPED}`` in :attr:`lines` renders the *wrapper* a composite rule
    polices, carrying something that is not the subject. That is the same
    argument one level up: for a rule passing ``wrap=``, every unwrapped
    decoy is a shape its predicate structurally cannot report, so without
    a wrapped one the negative control cannot fire at all — literally
    true and materially misleading, exactly as #497's finding (a) was.
    """

    id: str
    why: str
    lines: tuple[str, ...]


#: Every naive scanner. Named once so the shapes that defeat all of them do
#: not have to re-list it and drift.
_ALL_NAIVE = frozenset(
    {
        "name_only",
        "stmt_descent",
        "typed_descent",
        "shallow",
        "function_bodies_only",
        "sync_only",
        "single_file",
        "kwarg_presence",
        "splat_tolerant",
    }
)


EVASIONS: tuple[Evasion, ...] = (
    # ── spelling ──────────────────────────────────────────────────────
    Evasion(
        id="bare_call",
        axis="spelling",
        why=(
            "the control: the one spelling every scan in this repo already "
            "handles. Present so a predicate that reports nothing at all "
            "fails loudly rather than by arithmetic, which is why it is "
            "the one shape a rule may never exempt."
        ),
        call="{SUBJECT}({ARGS})",
        context=("def _bare_call():", "    {STMT}"),
    ),
    Evasion(
        id="attribute_call",
        axis="spelling",
        why=(
            "reached through its module (`mod.Subject(...)`). An "
            "`ast.Name`-only match walks straight past it — #488 verbatim, "
            "covering 563 class-like calls in src/."
        ),
        call="pkg.{SUBJECT}({ARGS})",
        context=("def _attribute_call():", "    {STMT}"),
        missed_by=frozenset({"name_only"}),
    ),
    Evasion(
        id="dotted_attribute",
        axis="spelling",
        why=(
            "two levels of attribute access. A predicate that checks the "
            "receiver through `func.value.id` rather than reading the "
            "trailing `func.attr` sees `sub` and gives up."
        ),
        call="pkg.sub.{SUBJECT}({ARGS})",
        context=("def _dotted_attribute():", "    {STMT}"),
        missed_by=frozenset({"name_only"}),
    ),
    # ── binding ───────────────────────────────────────────────────────
    Evasion(
        id="aliased_import",
        axis="binding",
        why=(
            "bound under another name at import time. Resolvable — "
            "`construction_names` reads `ImportFrom.asname` — and the "
            "first cut of this module wrongly declared it unresolvable."
        ),
        call="_Aliased({ARGS})",
        preamble=("from pkg import {SUBJECT} as _Aliased",),
        context=("def _aliased_import():", "    {STMT}"),
        missed_by=_ALL_NAIVE,
    ),
    Evasion(
        id="local_rebinding",
        axis="binding",
        why=(
            "rebound to a local name one line before the call "
            "(`PB = PackBuilder; PB(...)`). Resolvable: the assignment's "
            "value is a bare name."
        ),
        call="_Rebound({ARGS})",
        context=(
            "def _local_rebinding():",
            "    _Rebound = {SUBJECT}",
            "    {STMT}",
        ),
        missed_by=_ALL_NAIVE,
    ),
    Evasion(
        id="rebinding_chain",
        axis="binding",
        why=(
            "two hops (`A = Subject; B = A; B(...)`). Only the fixed-point "
            "iteration in `construction_names` catches it — a single pass "
            "resolves `A` and stops — so this shape is what makes the "
            "`while changed` loop load-bearing rather than decorative."
        ),
        call="_Chain2({ARGS})",
        context=(
            "def _rebinding_chain():",
            "    _Chain1 = {SUBJECT}",
            "    _Chain2 = _Chain1",
            "    {STMT}",
        ),
        missed_by=_ALL_NAIVE,
    ),
    Evasion(
        id="walrus_rebinding",
        axis="binding",
        why=(
            "rebound by a walrus in an `if` test (`if (S := Subject) is "
            "not None: S(...)`). #501's live half: "
            "`test_machine_output_rule._serialized_names` already reads "
            "`ast.NamedExpr`, so someone had been bitten and fixed it "
            "*there*, while the shared resolver read `ast.Assign` alone. "
            "Note that the walrus is a **binding** shape and not a "
            "placement one — a call sitting inside a walrus is an "
            "ordinary expression that no scanner here misses."
        ),
        call="_Walrus({ARGS})",
        context=(
            "def _walrus_rebinding():",
            "    if (_Walrus := {SUBJECT}) is not None:",
            "        {STMT}",
        ),
        missed_by=_ALL_NAIVE,
    ),
    Evasion(
        id="annassign_rebinding",
        axis="binding",
        why=(
            "rebound by an *annotated* assignment (`S: type = Subject`), "
            "which is an `ast.AnnAssign` and not an `ast.Assign`. The "
            "other half of the same local fix — `_serialized_names` names "
            "both in one isinstance tuple — and lifting one without the "
            "other is how a half-closed blind spot reads as closed."
        ),
        call="_Ann({ARGS})",
        context=(
            "def _annassign_rebinding():",
            "    _Ann: type = {SUBJECT}",
            "    {STMT}",
        ),
        missed_by=_ALL_NAIVE,
    ),
    Evasion(
        id="subclass_then_construct",
        axis="binding",
        why=(
            "constructed under the subclass's own name; the subclass runs "
            "the policed constructor through the `super().__init__()` its "
            "preamble really contains, so the argument list this kind of "
            "rule polices is copied one indirection out."
        ),
        call="_Subclassed({ARGS})",
        preamble=(
            "class _Subclassed({SUBJECT}):",
            "    def __init__(self):",
            "        super().__init__()  " + DECOY_MARKER + "super_init",
        ),
        context=("def _subclass_then_construct():", "    {STMT}"),
        missed_by=_ALL_NAIVE,
    ),
    Evasion(
        id="partial_binding",
        axis="binding",
        why=(
            "RESIDUE. The binding's value is a *call* "
            "(`functools.partial(Subject)`), which has no name to read, so "
            "the fixed point never admits it. Demonstrated rather than "
            "asserted: no scanner in this module reports it."
        ),
        call="_Partial({ARGS})",
        context=(
            "def _partial_binding():",
            "    _Partial = functools.partial({SUBJECT})",
            "    {STMT}",
        ),
        missed_by=_ALL_NAIVE,
        residue=True,
    ),
    Evasion(
        id="cross_module_subclass",
        axis="binding",
        why=(
            "RESIDUE. The subclass is defined in the *other* file and "
            "imported, so a per-module fixed point has nothing to resolve "
            "against. Closing it needs a whole-tree pass, which is a "
            "different rule rather than a wider predicate."
        ),
        call="_Exported({ARGS})",
        preamble=("class _Exported({SUBJECT}):", "    pass"),
        context=("def _cross_module_subclass():", "    {STMT}"),
        file=SECOND_FILE,
        missed_by=_ALL_NAIVE,
        residue=True,
    ),
    # ── placement ─────────────────────────────────────────────────────
    Evasion(
        id="inside_except",
        axis="placement",
        why=(
            "#457's own shape. `ast.ExceptHandler` is neither an "
            "`ast.stmt` nor an `ast.expr`, so a descent over statements "
            "never enters it — and neither does the descent over "
            "statements *and* expressions that is #457's natural repair. "
            "584 calls in src/ sit behind one."
        ),
        call="{SUBJECT}({ARGS})",
        context=(
            "def _inside_except():",
            "    try:",
            "        pass",
            "    except ValueError:",
            "        {STMT}",
        ),
        missed_by=frozenset({"stmt_descent", "typed_descent"}),
    ),
    Evasion(
        id="nested_function",
        axis="placement",
        why=(
            "a closure defined inside the function that returns it. A scan "
            "reading a module's top level and one level of function bodies "
            "stops exactly here."
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
        axis="placement",
        why=(
            "an expression body, which carries no statement for a "
            "statement-shaped scan to land on."
        ),
        call="{SUBJECT}({ARGS})",
        context=("def _lambda_body():", "    return lambda: {STMT}"),
        missed_by=frozenset({"shallow"}),
    ),
    Evasion(
        id="module_scope",
        axis="placement",
        why=(
            "at module level, outside any function. A scan that iterates "
            "function bodies — the natural shape when the rule is about "
            "what a function does — never looks here, and module scope "
            "holds 842 of the 15,977 calls in src/."
        ),
        call="{SUBJECT}({ARGS})",
        context=("{STMT}",),
        missed_by=frozenset({"function_bodies_only"}),
    ),
    Evasion(
        id="class_body",
        axis="placement",
        why=(
            "a class attribute initialised at definition time. Neither a "
            "function body nor module scope, so it falls between the two "
            "shapes a hand-rolled walker usually handles."
        ),
        call="{SUBJECT}({ARGS})",
        context=("class _InClassBody:", "    attribute = {STMT}"),
        missed_by=frozenset({"function_bodies_only"}),
    ),
    Evasion(
        id="async_function",
        axis="placement",
        why=(
            "inside `async def`. `ast.AsyncFunctionDef` is a separate node "
            "type from `ast.FunctionDef`, so every isinstance check naming "
            "only the latter goes blind to 379 calls in src/ — and this "
            "repo's stores, routes and MCP tools are largely async."
        ),
        call="{SUBJECT}({ARGS})",
        context=("async def _async_function():", "    {STMT}"),
        missed_by=frozenset({"sync_only"}),
    ),
    Evasion(
        id="decorator",
        axis="placement",
        why=(
            "in a decorator expression, which hangs off the function node "
            "rather than sitting in its body — 275 calls in src/, where "
            "`@app.get(...)` and `@mcp.tool()` are the house style."
        ),
        call="{SUBJECT}({ARGS})",
        context=("@{STMT}", "def _decorated():", "    pass"),
        missed_by=frozenset({"function_bodies_only"}),
    ),
    Evasion(
        id="with_item",
        axis="placement",
        why=(
            "the context manager of a `with` statement. `ast.withitem` is "
            "neither a statement nor an expression — the same node class "
            "as #457's `ast.ExceptHandler` — so the descent that repairs "
            "#457 by adding expressions still walks past all 167 such "
            "calls in src/."
        ),
        call="{SUBJECT}({ARGS})",
        context=(
            "def _with_item():",
            "    with (",
            "        {STMT}",
            "    ) as _held:",
            "        del _held",
        ),
        missed_by=frozenset({"typed_descent"}),
    ),
    Evasion(
        id="comprehension_clause",
        axis="placement",
        why=(
            "the iterable of a comprehension (`[row for row in "
            "Subject()]`). `ast.comprehension` is the same neither-nor "
            "node class, and it hides 315 calls in src/. Note which half "
            "of a comprehension this is: the *element* is an ordinary "
            "`ast.expr` child, no scanner here misses it, and it is "
            "deliberately not a shape."
        ),
        call="{SUBJECT}({ARGS})",
        context=(
            "def _comprehension_clause():",
            "    return [",
            "        _row",
            "        for _row in {STMT}",
            "    ]",
        ),
        missed_by=frozenset({"typed_descent"}),
    ),
    Evasion(
        id="keyword_argument",
        axis="placement",
        why=(
            "constructed inline as another call's keyword argument. "
            "`ast.keyword` is the largest member of the neither-statement-"
            "nor-expression family: 1,314 calls in src/ sit behind one, "
            "more than the other four put together."
        ),
        call="{SUBJECT}({ARGS})",
        context=(
            "def _keyword_argument():",
            "    return _helper(",
            "        _wrapped={STMT}",
            "    )",
        ),
        missed_by=frozenset({"typed_descent"}),
    ),
    Evasion(
        id="default_argument",
        axis="placement",
        why=(
            "a mutable default evaluated once at definition time — the "
            "placement whose whole reputation is for surprising people. "
            "It hangs off `ast.arguments`, neither statement nor "
            "expression, and covers 437 calls in src/. It is the one "
            "member of that family a *second* scanner also misses: "
            "iterating `node.body` never reaches the signature, which is "
            "the same reason `decorator` hides from that scanner."
        ),
        call="{SUBJECT}({ARGS})",
        context=(
            "def _default_argument(",
            "    _x={STMT}",
            "):",
            "    return _x",
        ),
        missed_by=frozenset({"function_bodies_only", "typed_descent"}),
    ),
    # ── judgement ─────────────────────────────────────────────────────
    Evasion(
        id="kwargs_splat",
        axis="judgement",
        why=(
            "wiring passed as `**kwargs`, opaque to a static scan. A "
            "judgement that treats the splat as 'probably fine' is the "
            "obvious way a rule is turned off without being deleted."
        ),
        call="{SUBJECT}(**_kwargs)",
        context=("def _kwargs_splat(**_kwargs):", "    {STMT}"),
        missed_by=frozenset({"splat_tolerant"}),
    ),
    Evasion(
        id="none_keyword",
        axis="judgement",
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
    # ── discovery ─────────────────────────────────────────────────────
    Evasion(
        id="second_file_bare_call",
        axis="discovery",
        why=(
            "#464's shape: the plainest possible call, in the *other* "
            "file. A scan whose file discovery narrows — to one basename, "
            "one package, one directory — keeps reporting everything it "
            "still sees, so every guard computed over its own output stays "
            "green while the population shrinks."
        ),
        call="{SUBJECT}({ARGS})",
        context=("def _second_file_bare_call():", "    {STMT}"),
        file=SECOND_FILE,
        missed_by=frozenset({"single_file"}),
    ),
)

#: Ids in roster order.
EVASION_IDS: tuple[str, ...] = tuple(evasion.id for evasion in EVASIONS)

#: Calls that must never be reported. Each names the predicate error it
#: catches. One more, ``super_init``, is rendered by
#: ``subclass_then_construct``'s own preamble rather than here, so that
#: shape's stated reason is executable rather than decorative.
DECOYS: tuple[Decoy, ...] = (
    Decoy(
        id="unrelated_call",
        why="a call to something else entirely — the baseline false positive",
        lines=(
            "def _decoy_unrelated():",
            "    _helper()  " + DECOY_MARKER + "unrelated_call",
        ),
    ),
    Decoy(
        id="subject_as_argument",
        why=(
            "the subject *passed*, not called. A predicate keyed on the "
            "name appearing in the line — or on any name among the call's "
            "arguments — reports it"
        ),
        lines=(
            "def _decoy_argument():",
            "    _helper({SUBJECT})  " + DECOY_MARKER + "subject_as_argument",
        ),
    ),
    Decoy(
        id="attribute_of_subject",
        why=(
            "a method *on* the subject, not a construction of it. A scan "
            "matching the receiver rather than the trailing name reports it"
        ),
        lines=(
            "def _decoy_method():",
            "    {SUBJECT}.helper()  " + DECOY_MARKER + "attribute_of_subject",
        ),
    ),
    Decoy(
        id="annotation_only",
        why=(
            "a mention in an annotation and a return type, constructing "
            "nothing. #488's docstring makes this point: the class is "
            "named in a dozen docstrings and annotations and neither "
            "constructs anything"
        ),
        lines=(
            "def _decoy_annotation(arg: {SUBJECT} = None) -> {SUBJECT}:",
            "    return _helper()  " + DECOY_MARKER + "annotation_only",
        ),
    ),
    Decoy(
        id="wrapper_without_the_subject",
        why=(
            "the wrapper a composite rule polices, carrying something "
            "that is not the subject — `console.print(_helper())` for a "
            "rule whose offence is a Rich render of a serialized payload. "
            "Every other decoy renders unwrapped, so a wrapped rule's "
            "predicate cannot report one however broken it is, and the "
            "negative control never fires: found by the #497 re-gate, "
            "which passed the guard with a predicate reporting every "
            "console.print and judging its argument not at all"
        ),
        lines=(
            "def _decoy_wrapper():",
            "    {WRAPPED}  " + DECOY_MARKER + "wrapper_without_the_subject",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_PRIMARY_HEADER = (
    "from __future__ import annotations",
    "",
    "import functools",
    "import pkg",
    "import pkg.sub",
    "from pkg import {SUBJECT}",
    "",
    "",
    "def _helper(*args, **kwargs):",
    "    return None",
    "",
)

_SECOND_HEADER = (
    "from evasions import _Exported",
    "from pkg import {SUBJECT}",
    "",
)


@dataclass(frozen=True)
class RenderedCorpus:
    """The synthetic tree a guard runs a shipped predicate over.

    Attributes:
        files: ``{filename: source}``, in render order.
        lines: ``{evasion id: line number}``.
        decoys: ``{decoy id: line number}`` — calls that must never be
            reported.

    Line numbers are **globally unique across the corpus**: the second file
    opens with a banner padded to the primary file's length. That keeps the
    predicate contract — ``(root) -> line numbers`` — the same flat shape
    every existing rule already produces, instead of making each of them
    return a ``(file, line)`` pair for the benefit of the guard. The
    padding is real text and says so in its own banner.
    """

    files: dict[str, str]
    lines: dict[str, int]
    decoys: dict[str, int]

    @property
    def primary(self) -> str:
        return self.files[PRIMARY_FILE]

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        for name, source in self.files.items():
            (directory / name).write_text(source, encoding="utf-8")
        return directory


def _substitute(line: str, *, subject: str, args: str, kwarg: str) -> str:
    return line.format(SUBJECT=subject, ARGS=args, KWARG=kwarg, STMT="{STMT}")


def _render_context(
    lines: list[str],
    evasion: Evasion,
    *,
    subject: str,
    args: str,
    kwarg: str,
    wrap: str,
) -> int | None:
    call = _substitute(evasion.call, subject=subject, args=args, kwarg=kwarg)
    statement = f"{wrap.format(call=call)}  {MARKER}{evasion.id}"
    at: int | None = None
    for line in evasion.context:
        rendered = _substitute(line, subject=subject, args=args, kwarg=kwarg)
        if "{STMT}" in rendered:
            at = len(lines) + 1
            rendered = rendered.replace("{STMT}", statement)
        lines.append(rendered)
    lines.append("")
    return at


def render_evasion_corpus(
    evasions: Sequence[Evasion] = EVASIONS,
    decoys: Sequence[Decoy] = DECOYS,
    *,
    subject: str,
    args: str = "",
    kwarg: str = "gate",
    wrap: str = "{call}",
) -> RenderedCorpus:
    """Render *evasions* and *decoys* as a two-file corpus.

    *wrap* lets a rule whose offence is a *composite* — ``console.print``
    carrying a serialized payload, say — reuse the spelling, binding and
    placement axes without the roster modelling its subject. The roster
    supplies the call; the rule supplies what surrounds it. Decoys render
    **unwrapped**, so they stay non-offences for every rule — with the one
    exception that has to exist. ``wrapper_without_the_subject`` renders
    *through* ``wrap`` carrying ``_helper()``, because otherwise a wrapped
    rule's predicate can report no decoy at all, however broken it is, and
    the negative control is structurally dead. It is still a non-offence:
    the wrapper is there and the subject is not.

    Line numbers are computed from the assembled text rather than written
    down beside it, so removing an entry reflows the corpus without
    invalidating anything — the hand-written line tables in the existing
    rules are what makes those corpora awkward to edit.
    """
    primary: list[str] = [
        _substitute(line, subject=subject, args=args, kwarg=kwarg)
        for line in _PRIMARY_HEADER
    ]

    # Every preamble lands in the primary file, including one belonging to
    # a shape whose context is in the second — that is what makes
    # ``cross_module_subclass`` a real cross-module reference rather than a
    # story about one.
    for evasion in evasions:
        if not evasion.preamble:
            continue
        primary.extend(
            _substitute(line, subject=subject, args=args, kwarg=kwarg)
            for line in evasion.preamble
        )
        primary.append("")

    # ``{WRAPPED}`` is the one placeholder a decoy resolves against *wrap*.
    # It is substituted first, so the wrapper text a rule supplies goes
    # through ``_substitute`` exactly like anything else and a wrapper
    # mentioning ``{SUBJECT}`` would still resolve.
    wrapped = wrap.format(call="_helper()")
    for decoy in decoys:
        primary.extend(
            _substitute(
                line.replace("{WRAPPED}", wrapped),
                subject=subject,
                args=args,
                kwarg=kwarg,
            )
            for line in decoy.lines
        )
        primary.append("")

    at: dict[str, int] = {}
    for evasion in evasions:
        if evasion.file != PRIMARY_FILE:
            continue
        line = _render_context(
            primary, evasion, subject=subject, args=args, kwarg=kwarg, wrap=wrap
        )
        if line is not None:
            at[evasion.id] = line

    primary_source = "\n".join(primary) + "\n"
    offset = len(primary)

    banner = [
        "# Padded to the primary file's length so that every marker line",
        "# number in this corpus is globally unique — see RenderedCorpus.",
        "# The padding is the price of keeping the predicate contract a",
        "# flat set of integers rather than (file, line) pairs.",
    ]
    second: list[str] = list(banner)
    second.extend("#" for _ in range(offset - len(banner) - len(_SECOND_HEADER)))
    second.extend(
        _substitute(line, subject=subject, args=args, kwarg=kwarg)
        for line in _SECOND_HEADER
    )
    assert len(second) == offset, (
        f"second-file padding is {len(second)} lines against a primary of "
        f"{offset}; marker line numbers would collide across the corpus"
    )

    for evasion in evasions:
        if evasion.file == PRIMARY_FILE:
            continue
        line = _render_context(
            second, evasion, subject=subject, args=args, kwarg=kwarg, wrap=wrap
        )
        if line is not None:
            at[evasion.id] = line

    files = {PRIMARY_FILE: primary_source, SECOND_FILE: "\n".join(second) + "\n"}
    found_decoys: dict[str, int] = {}
    for source in files.values():
        found_decoys.update(_comment_lines(source, DECOY_MARKER))

    return RenderedCorpus(files=files, lines=at, decoys=found_decoys)


def _comment_lines(source: str, prefix: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        text = token.string.strip()
        if not text.startswith(prefix):
            continue
        found[text[len(prefix) :].strip()] = token.start[0]
    return found


def marker_lines(source: str) -> dict[str, int]:
    """Recover ``{evasion id: line}`` from rendered *source* by tokenizing.

    The independent half of the harness's own cross-check, and the reason
    :data:`MARKER` is a machine-readable comment rather than prose.
    Tokenizing builds no tree, so a tree-shaped bug in
    :func:`render_evasion_corpus`'s accounting cannot reach it. Comments
    tokenize as ``COMMENT`` and never as ``NAME``, so a marker discussed
    inside a docstring is excluded structurally rather than by pattern.
    """
    return _comment_lines(source, MARKER)


def decoy_lines(source: str) -> dict[str, int]:
    """Recover ``{decoy id: line}`` the same way."""
    return _comment_lines(source, DECOY_MARKER)


# ---------------------------------------------------------------------------
# The naive scanners the roster is measured against
# ---------------------------------------------------------------------------
#
# Each is a real under-collection or over-permission shape. They exist so
# every ``Evasion.missed_by`` claim is checkable by execution rather than by
# comment, and so a new rule can assert it beats them. Three of the nine
# came from the #497 review gate, which found them by mutating the guard
# rather than by reading it.


def _sources(corpus: RenderedCorpus) -> list[str]:
    return list(corpus.files.values())


def _naive_name_only(corpus: RenderedCorpus, subject: str, kwarg: str) -> set[int]:
    """#488: matches only ``ast.Name``, so every attribute call escapes."""
    return {
        node.lineno
        for source in _sources(corpus)
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


def _naive_stmt_descent(corpus: RenderedCorpus, subject: str, kwarg: str) -> set[int]:
    """#457: descends only ``ast.stmt``, so ``except`` bodies vanish."""
    return {
        call.lineno
        for source in _sources(corpus)
        for statement in _statements(ast.parse(source))
        for call in _own_expression_calls(statement)
        if name_of(call.func) == subject
    }


def _typed_calls(node: ast.AST) -> Iterator[ast.Call]:
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.stmt, ast.expr)):
            continue
        if isinstance(child, ast.Call):
            yield child
        yield from _typed_calls(child)


def _naive_typed_descent(corpus: RenderedCorpus, subject: str, kwarg: str) -> set[int]:
    """#457's *fix*, still wrong: descends ``ast.stmt`` and ``ast.expr``.

    The natural repair for #457 — whose scanner descended only ``ast.stmt``
    and so never entered an ``ast.ExceptHandler`` — is to descend
    expressions too. It reads as exhaustive and is not: the grammar has a
    third family of nodes that are **neither**, and every call behind one
    is invisible. Measured over ``src/`` at ``8ec879c``, this predicate
    finds 13,206 of 16,023 calls and loses **2,817 (17.6%)**, gated by five
    node types — ``ast.keyword`` (1,314), ``ast.ExceptHandler`` (584),
    ``ast.arguments`` (437), ``ast.comprehension`` (315) and
    ``ast.withitem`` (167).

    The roster carries a shape for each of those five, and
    ``test_ast_rules`` derives that requirement from ``src/`` rather than
    from a list here — which is also what disposes of ``ast.match_case``,
    the sixth member: ``src/`` contains 0 ``ast.Match`` nodes, so the
    derived requirement asks for nothing, and it starts asking the day one
    is written.
    """
    return {
        call.lineno
        for source in _sources(corpus)
        for call in _typed_calls(ast.parse(source))
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


def _naive_shallow(corpus: RenderedCorpus, subject: str, kwarg: str) -> set[int]:
    """Reads a module's top level and one level of bodies, nothing deeper."""
    return {
        call.lineno
        for source in _sources(corpus)
        for call in _shallow_calls(ast.parse(source), 0)
        if name_of(call.func) == subject
    }


def _naive_function_bodies_only(
    corpus: RenderedCorpus, subject: str, kwarg: str
) -> set[int]:
    """Iterates function bodies, so module scope, class scope and decorator
    expressions are never looked at."""
    found: set[int] = set()
    for source in _sources(corpus):
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for statement in node.body:
                found.update(
                    call.lineno
                    for call in ast.walk(statement)
                    if isinstance(call, ast.Call) and name_of(call.func) == subject
                )
    return found


def _naive_sync_only(corpus: RenderedCorpus, subject: str, kwarg: str) -> set[int]:
    """Names ``ast.FunctionDef`` and forgets ``ast.AsyncFunctionDef``."""
    found: set[int] = set()
    for source in _sources(corpus):
        tree = ast.parse(source)
        skipped = {
            id(inner)
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            for inner in ast.walk(node)
        }
        found.update(
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and id(node) not in skipped
            and name_of(node.func) == subject
        )
    return found


def _naive_single_file(corpus: RenderedCorpus, subject: str, kwarg: str) -> set[int]:
    """#464: correct within one file, and there is more than one file."""
    return {
        node.lineno
        for node in ast.walk(ast.parse(corpus.primary))
        if isinstance(node, ast.Call) and name_of(node.func) == subject
    }


def _naive_kwarg_presence(corpus: RenderedCorpus, subject: str, kwarg: str) -> set[int]:
    """Correct collection, but judges on the keyword being *present*."""
    return {
        node.lineno
        for source in _sources(corpus)
        for node in ast.walk(ast.parse(source))
        if is_call_to(node, subject)
        and kwarg not in {kw.arg for kw in node.keywords if kw.arg}
    }


def _naive_splat_tolerant(corpus: RenderedCorpus, subject: str, kwarg: str) -> set[int]:
    """Correct collection, but treats ``**kwargs`` as satisfying the rule."""
    reported: set[int] = set()
    for source in _sources(corpus):
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
#: really shipped or that the #497 review gate produced by mutation.
NAIVE_SCANNERS: Mapping[str, Callable[[RenderedCorpus, str, str], set[int]]] = {
    "name_only": _naive_name_only,
    "stmt_descent": _naive_stmt_descent,
    "typed_descent": _naive_typed_descent,
    "shallow": _naive_shallow,
    "function_bodies_only": _naive_function_bodies_only,
    "sync_only": _naive_sync_only,
    "single_file": _naive_single_file,
    "kwarg_presence": _naive_kwarg_presence,
    "splat_tolerant": _naive_splat_tolerant,
}


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------

#: A reason shorter than this many characters, or with fewer than
#: :data:`_MIN_REASON_WORDS` words, is not a reason. Both halves are
#: load-bearing and each is pinned separately: the #497 gate passed this
#: guard with twenty ``x`` characters (long enough, one word), and a
#: four-word fragment like "not worth doing now" is four words and not a
#: reason either.
_MIN_REASON_CHARS = 20
_MIN_REASON_WORDS = 4


def _reason_is_written(reason: str) -> bool:
    """A reason is prose, not padding.

    ``strip`` first, and that is not decoration: without it a short reason
    padded to twenty characters with trailing spaces clears the length
    test. Pinned by ``test_padding_cannot_buy_the_length``.
    """
    stripped = reason.strip()
    return (
        len(stripped) >= _MIN_REASON_CHARS
        and len(stripped.split()) >= _MIN_REASON_WORDS
    )


def assert_hand_read_floor(
    population: int,
    floor: int,
    *,
    subject: str,
    hint: str = "",
    sole_site_reason: str | None = None,
) -> None:
    """The population the rule reasons over is at least *floor*.

    *floor* is an argument and not a computation, and that is the design:
    #466's floor was ``len(SITES) > 0``, which three blind spots satisfied.
    A number a person read off the tree is the only thing a
    silently-narrowing scan cannot also satisfy.

    But note *why* #466's floor was wrong — the scan computed it, not that
    it was small. A genuinely single-site rule is legitimate, so a floor
    below two is accepted when the caller writes down why there is only
    one, rather than refused outright.

    It is a floor rather than an equality on purpose: adding a new
    compliant site is ordinary and must not turn the suite red. What must
    turn it red is the scan finding *less than a person counted* — so the
    comparison is ``>=`` against the number as given, and a mutant that
    halves it before comparing is pinned by test.
    """
    if floor < 2:
        written = sole_site_reason is not None and _reason_is_written(sole_site_reason)
        assert written, (
            f"a floor of {floor} for {subject} is satisfied by a scan that "
            f"has stopped matching almost everything (#466 shipped "
            f"`len(SITES) > 0`). If the tree really holds one site, pass "
            f"sole_site_reason= saying so in a sentence; otherwise count "
            f"the sites by hand and write that number down."
        )
    assert population >= floor, (
        f"the scan found {population} {subject} site(s), below the "
        f"hand-read floor of {floor}. Either the scan has drifted and is "
        f"no longer policing anything, or the tree really shrank and the "
        f"floor needs re-reading against it. {hint}".rstrip()
    )


def _validate_exemptions(
    declared: Mapping[str, str],
    by_id: Mapping[str, Evasion],
    evasions: Sequence[Evasion],
) -> None:
    """Exemptions cannot rot, reach the control, or empty an axis.

    Four bounds, and they are not redundant even though the third mostly
    subsumes the fourth on the **default** roster — there every exemptible
    (``residue``) shape sits on the ``binding`` axis, which has six
    members, so the axis rule can only ever fire for a roster a caller
    *narrowed*. That case is real (``evasions=`` is a parameter) and is
    reached by test with a residue-only subset; it is defence in depth
    rather than the thing that catches the #497 gate's whole-roster
    exemption, which the residue rule refuses first.
    """
    unknown = sorted(set(declared) - set(by_id))
    assert not unknown, (
        f"exempt names {unknown}, which are not shapes in this roster "
        f"(known: {sorted(by_id)}). A stale exemption silences a shape "
        f"nobody is checking."
    )

    thin = sorted(
        name for name, reason in declared.items() if not _reason_is_written(reason)
    )
    assert not thin, (
        f"the exemption(s) for {thin} give no reason — a reason is at "
        f"least {_MIN_REASON_CHARS} characters and {_MIN_REASON_WORDS} "
        f"words of prose, because the #497 gate passed this guard with "
        f"twenty 'x' characters per shape. Say what closes the shape "
        f"instead, or drop the exemption."
    )

    controls = sorted(name for name in declared if not by_id[name].missed_by)
    assert not controls, (
        f"{controls} cannot be exempted: a shape no naive scanner misses "
        f"is the control, present so a predicate reporting nothing fails "
        f"loudly. Exempting it removes the only thing standing between an "
        f"empty result and a green guard."
    )

    resolvable = sorted(name for name in declared if not by_id[name].residue)
    assert not resolvable, (
        f"{resolvable} are resolvable, not residue: "
        f"tests.ast_rules.construction_names follows import aliases, "
        f"rebindings (to a fixed point) and subclasses within a module, "
        f"and ast.walk reaches every scope. Widen the scan instead of "
        f"declaring blindness — institutionalising these exemptions is "
        f"#490's thesis inverted. Only shapes marked RESIDUE are "
        f"exemptible."
    )

    for axis in AXES:
        in_axis = [e.id for e in evasions if e.axis == axis]
        if not in_axis:
            continue
        assert [name for name in in_axis if name not in declared], (
            f"every shape on the '{axis}' axis is exempted, so the guard "
            f"no longer probes it at all. The #497 gate passed this guard "
            f"by exempting the whole roster at once; an axis is the "
            f"smallest unit that has to survive."
        )


def _validate_roster(evasions: Sequence[Evasion], roster_reason: str | None) -> None:
    """Narrowing the roster is exempting, and is bounded the same way.

    Every bound in :func:`_validate_exemptions` is on ``exempt=``, and the
    #497 re-gate walked past all four by narrowing ``evasions=`` instead:
    ``evasions=[]`` with a predicate returning nothing passed, and so did
    ``evasions=[bare_call]`` against #488's own ``ast.Name``-only scan —
    the exact predicate this module exists to reject. A dropped shape and
    an exempted one are the same act with the same consequence, so the
    cheaper spelling must not be the unbounded one.

    Deliberately a *reason* rather than the residue rule ``exempt=``
    applies. A rule whose subject cannot render a shape at all is real —
    the roster models a call, not every rule's offence — but it has to say
    so in a sentence, which is what makes the omission legible in a diff
    and re-askable later. The harness's own leave-one-out proof is such a
    caller and now says so.
    """
    assert evasions, (
        "assert_scan_is_not_vacuous needs a roster. An empty one makes "
        "every coverage check vacuously true, which is the whole failure "
        "this module exists to end — a scan that under-collects satisfies "
        "every guard computed over its own output."
    )
    omitted = sorted(set(EVASION_IDS) - {evasion.id for evasion in evasions})
    if not omitted:
        return
    assert _reason_is_written(roster_reason or ""), (
        f"this call drops {omitted} from the shared roster. That is an "
        f"exemption by another name and it bypasses every bound on "
        f"exempt= — the control, the residue rule, the axis rule and the "
        f"written reason. Pass the whole of EVASIONS and exempt what you "
        f"genuinely cannot reach, or pass roster_reason= saying in a "
        f"sentence why these shapes cannot be rendered for this rule."
    )


def assert_scan_is_not_vacuous(
    shipped_predicate: Callable[[Path], Iterable[int]],
    evasions: Sequence[Evasion] = EVASIONS,
    decoys: Sequence[Decoy] = DECOYS,
    *,
    subject: str,
    tmp_path: Path,
    live_population: int,
    floor: int,
    args: str = "",
    kwarg: str = "gate",
    wrap: str = "{call}",
    exempt: Mapping[str, str] | None = None,
    sole_site_reason: str | None = None,
    roster_reason: str | None = None,
) -> None:
    """Run the **shipped** predicate over the roster, and floor its population.

    Args:
        shipped_predicate: ``(root) -> line numbers reported``. Must be the
            function the rule itself calls, not a copy: a guard run against
            a re-implementation leaves the shipped one free to regress with
            the suite green.
        evasions: The roster. Defaults to all of :data:`EVASIONS`. A
            narrowed roster is an exemption by another name and is bounded
            the same way — see *roster_reason* and :func:`_validate_roster`.
        decoys: Calls that must never be reported. Defaults to
            :data:`DECOYS`; an empty sequence is refused, because without a
            negative control "nothing unmarked was reported" is satisfied
            by any predicate reporting every call line.
        subject: The name the rule polices, substituted into every shape.
        tmp_path: pytest's, so the corpus is thrown away.
        live_population: What the shipped scan finds in the real tree.
        floor: Hand-read. See :func:`assert_hand_read_floor`.
        exempt: ``{evasion id: why this rule cannot catch it}``. Bounded by
            :func:`_validate_exemptions`. An exemption is an *upper bound*
            on blindness: a rule that catches an exempt shape still passes.
        roster_reason: Required whenever *evasions* is not the whole of
            :data:`EVASIONS`, and held to the same prose bar as an
            exemption's. Dropping a shape and exempting it are the same
            act; only the exempt path was bounded, and the #497 re-gate
            passed this guard with ``evasions=[]`` and a predicate that
            reported nothing.

    Three things this asserts that a hand-rolled guard usually does not.
    Every non-exempt shape must be **reported**, named by shape rather than
    by line; no **decoy** may be reported; and nothing unmarked may be
    reported at all.
    """
    assert_hand_read_floor(
        live_population,
        floor,
        subject=subject,
        hint="assert_scan_is_not_vacuous cannot supply this number for you.",
        sole_site_reason=sole_site_reason,
    )

    assert decoys, (
        "assert_scan_is_not_vacuous needs at least one decoy. Without a "
        "call that must never be reported, every ast.Call in the corpus "
        "sits on a marked line and a predicate reporting every call line "
        "passes the spurious check — true, and materially misleading."
    )

    _validate_roster(evasions, roster_reason)

    declared = dict(exempt or {})
    by_id = {evasion.id: evasion for evasion in evasions}
    _validate_exemptions(declared, by_id, evasions)

    corpus = render_evasion_corpus(
        evasions, decoys, subject=subject, args=args, kwarg=kwarg, wrap=wrap
    )
    directory = tmp_path / f"ast_rules_{subject}"
    suffix = 0
    while directory.exists():
        suffix += 1
        directory = tmp_path / f"ast_rules_{subject}_{suffix}"
    corpus.write(directory)

    reported = set(shipped_predicate(directory))
    required = {
        name: line for name, line in corpus.lines.items() if name not in declared
    }

    missed = sorted(name for name, line in required.items() if line not in reported)
    assert not missed, (
        f"the shipped scan for {subject} does not report {missed}.\n"
        + "\n".join(f"  {name}: {by_id[name].why}" for name in missed)
        + "\n\nWiden the scan — tests.ast_rules.construction_names resolves "
        "aliases, rebindings and subclasses, and ast.walk reaches every "
        "scope — or, for a shape marked RESIDUE, exempt it with a reason "
        "saying what closes it instead. A blind spot nobody wrote down is "
        "how the last four of these shipped."
    )

    hit = sorted(name for name, line in corpus.decoys.items() if line in reported)
    assert not hit, (
        f"the shipped scan for {subject} reported {hit}, which construct "
        f"nothing: {'; '.join(_decoy_why(decoys, evasions, hit))}. "
        f"Over-collection is cheaper than under-collection, but a rule "
        f"that flags an annotation will be turned off by the first author "
        f"it inconveniences."
    )

    known = set(corpus.lines.values()) | set(corpus.decoys.values())
    spurious = sorted(reported - known)
    assert not spurious, (
        f"the shipped scan for {subject} reported line(s) {spurious} that "
        f"carry no {MARKER} or {DECOY_MARKER} marker. A predicate that "
        f"reports everything satisfies the coverage check above without "
        f"seeing anything."
    )


def _decoy_why(
    decoys: Sequence[Decoy], evasions: Sequence[Evasion], names: Sequence[str]
) -> list[str]:
    """Why each named decoy constructs nothing.

    Reasons come from :data:`DECOYS` *and* from any shape whose preamble
    renders one — ``subclass_then_construct``'s ``super().__init__()`` is a
    decoy belonging to its shape, and deriving its reason from the shape
    keeps the two from drifting apart the way a second roster would.
    """
    reasons = {decoy.id: decoy.why for decoy in decoys}
    for evasion in evasions:
        for line in evasion.preamble:
            marker = line.find(DECOY_MARKER)
            if marker >= 0:
                reasons[line[marker + len(DECOY_MARKER) :].strip()] = (
                    f"part of the {evasion.id} shape, not a construction: {evasion.why}"
                )
    fallback = "a call that constructs nothing"
    return [f"{name} is {reasons.get(name, fallback)}" for name in names]
