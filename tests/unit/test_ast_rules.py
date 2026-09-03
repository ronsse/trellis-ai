"""Proof that the shared vacuity harness is not itself vacuous.

:mod:`tests.ast_rules` exists because four AST rules shipped evadable for
the same reason — the vacuity guard divided by the scan's own output. A
*shared* harness with that property would be strictly worse than none,
because it launders confidence: every rule adopting it would inherit one
guard that passes for everybody.

So the harness is held to the standard it imposes. What that means in
practice was set by the #497 review gate, which found **six** predicates
that passed the first cut of :func:`assert_scan_is_not_vacuous` while
being plainly broken — report every call line; descend only into
``FunctionDef`` bodies; skip ``AsyncFunctionDef``; narrow file discovery
to one filename; report nothing at all with every shape exempted; and the
same with twenty ``x`` characters as each exemption's reason. Every one of
those is now a test that must fail, and each shaped something in the
module: the decoys, three of the naive scanners, the second file, and the
bounds on exemptions.

Five claims, each demonstrated rather than asserted:

#. The guard **fails** against each member of
   :data:`~tests.ast_rules.NAIVE_SCANNERS`, and the failure names the
   *shape* rather than a line number.
#. Every :data:`~tests.ast_rules.EVASIONS` entry is **individually
   load-bearing**: a predicate blind to exactly that one shape fails the
   full roster and passes the roster with the entry removed.
#. The roster's own claims are checkable — ``missed_by`` is **run**, the
   two ``residue`` shapes are *computed* rather than declared, and each
   marked line is confirmed to carry a real call. The corpus's line table
   is recovered a second time by tokenizing.
#. **Decoys are reported by nobody.** Without them, "a predicate that
   reports every line is refused" was true only because every
   ``ast.Call`` in the corpus sat on a marked line — literally true and
   materially misleading, which is the #497 gate's phrase for it.
#. **Exemptions cannot empty the guard.** The control is unexemptible, a
   resolvable shape is unexemptible, no axis may be emptied, and a reason
   must be prose.

The model for all of this is
``tests/unit/mcp/test_capture_surface_roster.py``, which cross-checks its
AST scan against a tokenizer scan over line numbers and then proves that
cross-check by re-introducing #457's bug.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import ClassVar

import pytest

from tests.ast_rules import (
    AXES,
    EVASION_IDS,
    EVASIONS,
    NAIVE_SCANNERS,
    PRIMARY_FILE,
    SECOND_FILE,
    Evasion,
    RenderedCorpus,
    assert_hand_read_floor,
    assert_scan_is_not_vacuous,
    calls_named,
    calls_to_any,
    construction_names,
    construction_sites,
    decoy_lines,
    is_call_to,
    iter_modules,
    marker_lines,
    name_of,
    render_evasion_corpus,
)

#: A stand-in name with no meaning to any rule, so nothing here passes
#: because it happened to match the real tree.
SUBJECT = "Widget"
KWARG = "gate"

#: Long enough and wordy enough to satisfy the guard, for the tests whose
#: subject is something other than reason hygiene.
REASON = "a written reason with enough words to read as a sentence"

#: The two shapes no scanner in the module reports, the resolver included.
#: Computed independently, not trusted — see
#: ``test_residue_is_exactly_the_shapes_no_scanner_reports`` below.
RESIDUE = {"partial_binding", "cross_module_subclass"}

#: What every adopting rule passes for the residue.
RESIDUE_EXEMPT = dict.fromkeys(RESIDUE, REASON)


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src"
    assert root.is_dir(), f"src/ not found at {root}"
    return root


def _corpus() -> RenderedCorpus:
    return render_evasion_corpus(subject=SUBJECT, kwarg=KWARG)


def _predicate_from(
    scanner: Callable[[RenderedCorpus, str, str], set[int]],
) -> Callable[[Path], Iterable[int]]:
    """Adapt a naive scanner to the ``(root) -> line numbers`` contract."""

    def predicate(root: Path) -> Iterable[int]:
        files = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(root.glob("*.py"))
        }
        return scanner(RenderedCorpus(files=files, lines={}, decoys={}), SUBJECT, KWARG)

    return predicate


def _perfect(root: Path) -> Iterable[int]:
    """Reports exactly the marked lines, by reading the markers.

    Not an AST scan at all, which is the point: the reference the
    leave-one-out proof subtracts from must not share a method with the
    thing being proved.
    """
    found: list[int] = []
    for path in sorted(root.glob("*.py")):
        found.extend(marker_lines(path.read_text(encoding="utf-8")).values())
    return found


def _resolving_scan(root: Path) -> Iterable[int]:
    """The harness's own shared predicate, run the way a rule runs it.

    This is the control that matters most: the resolver lifted from #488
    must itself pass the guard with exactly the two ``residue`` shapes
    exempted and nothing else. If it needed a third, the roster would be
    describing a limit the shared code does not have — which is the state
    the #497 gate found and rejected.
    """
    return [site.lineno for site in construction_sites(SUBJECT, root)]


def _blind_to(target: str) -> Callable[[Path], Iterable[int]]:
    """:func:`_perfect` minus exactly one shape.

    Computed from the files it is handed rather than from a line table, so
    it survives the reflow that removing a roster entry causes — the
    hand-written line tables in the existing rules are precisely what makes
    those corpora awkward to edit.
    """

    def predicate(root: Path) -> Iterable[int]:
        found: list[int] = []
        for path in sorted(root.glob("*.py")):
            table = marker_lines(path.read_text(encoding="utf-8"))
            found.extend(line for name, line in table.items() if name != target)
        return found

    return predicate


def _every_call_line(root: Path) -> Iterable[int]:
    """#497's predicate (a): report every ``ast.Call`` in the tree.

    Broken by construction — it constructs nothing and distinguishes
    nothing — and it passed the first cut of the guard, because the
    coverage check is a superset test and every call in a roster-only
    corpus sat on a marked line.
    """
    found: list[int] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.extend(
            node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
        )
    return found


def _resolved_lines(corpus: RenderedCorpus) -> set[int]:
    """Every line the shared resolving scan reports across the corpus."""
    reported: set[int] = set()
    for source in corpus.files.values():
        tree = ast.parse(source)
        reported |= {
            node.lineno
            for node in calls_to_any(construction_names(SUBJECT, tree), tree)
        }
    return reported


# ---------------------------------------------------------------------------
# The shared predicates
# ---------------------------------------------------------------------------


class TestNameOf:
    """One correct call-target predicate, replacing four spellings."""

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("f()", "f"),
            ("mod.f()", "f"),
            ("a.b.c.f()", "f"),
            ("(lambda: 1)()", None),
            ("registry['f']()", None),
            ("make()()", None),
        ],
        ids=["bare", "attribute", "dotted", "lambda", "subscript", "call_result"],
    )
    def test_reads_the_trailing_name(
        self, expression: str, expected: str | None
    ) -> None:
        call = ast.parse(expression).body[0].value  # type: ignore[attr-defined]
        assert name_of(call.func) == expected

    def test_agrees_with_the_getattr_spelling_across_src(self) -> None:
        """``test_machine_output_rule``'s pair, checked before it was retired.

        It read ``getattr(func, "attr", None)`` and ``getattr(func, "id",
        None)`` and tested both for membership. Equivalent for every node
        kind Python produces — no AST node carries both attributes — but
        "equivalent" is the claim that conversion rested on, so it is
        checked against the whole tree rather than reasoned about.
        """
        disagreements: list[str] = []
        for path, tree in iter_modules(_src_root()):
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                pair = {
                    getattr(node.func, "attr", None),
                    getattr(node.func, "id", None),
                } - {None}
                if pair != {name_of(node.func)} - {None}:
                    disagreements.append(f"{path.name}:{node.lineno}")
        assert not disagreements, disagreements[:10]

    def test_agrees_with_the_isinstance_spelling_across_src(self) -> None:
        """``test_capture_surface_roster``'s ``_calls_named``, same check.

        That module is deliberately unconverted — it is the most delicate
        enforcement file in the repo and its diff belongs in its own PR —
        so this pins the equivalence now, and the conversion when it comes
        needs no re-derivation.
        """

        def old(node: ast.AST, name: str) -> bool:
            if not isinstance(node, ast.Call):
                return False
            func = node.func
            if isinstance(func, ast.Name):
                return func.id == name
            return isinstance(func, ast.Attribute) and func.attr == name

        disagreements: list[str] = []
        for path, tree in iter_modules(_src_root()):
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = name_of(node.func)
                if name is not None and old(node, name) is not is_call_to(node, name):
                    disagreements.append(f"{path.name}:{node.lineno}")
        assert not disagreements, disagreements[:10]

    def test_calls_named_reaches_an_except_block(self) -> None:
        """#457's shape, pinned on the helper every adopter now shares."""
        tree = ast.parse("try:\n    pass\nexcept ValueError:\n    target()\n")
        assert [call.lineno for call in calls_named("target", tree)] == [4]

    def test_construction_sites_spans_files_and_spellings(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("Widget()\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("mod.Widget()\n", encoding="utf-8")
        sites = construction_sites("Widget", tmp_path)
        assert {site.path.name for site in sites} == {"a.py", "b.py"}


class TestConstructionNames:
    """The resolver lifted from #488, and exactly what it cannot reach."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("from pkg import Widget as W", {"Widget", "W"}),
            ("W = Widget", {"Widget", "W"}),
            ("A = Widget\nB = A", {"Widget", "A", "B"}),
            ("class Mine(Widget):\n    pass", {"Widget", "Mine"}),
            ("class Mine(Widget):\n    pass\nAlso = Mine", {"Widget", "Mine", "Also"}),
        ],
        ids=["alias", "rebind", "chain", "subclass", "subclass_then_rebind"],
    )
    def test_resolves_the_shapes_the_first_cut_called_unresolvable(
        self, source: str, expected: set[str]
    ) -> None:
        assert construction_names("Widget", ast.parse(source)) == expected

    @pytest.mark.parametrize(
        "source",
        [
            "import functools\nP = functools.partial(Widget)",
            "from other import Derived",
            "P = getattr(mod, 'Widget')",
        ],
        ids=["partial", "cross_module", "getattr"],
    )
    def test_does_not_resolve_a_binding_with_no_name_to_read(self, source: str) -> None:
        """The residue, as a property of the resolver rather than as prose."""
        assert construction_names("Widget", ast.parse(source)) == {"Widget"}

    def test_the_fixed_point_is_what_catches_a_chain(self) -> None:
        """One pass resolves ``A`` and stops; the loop is load-bearing.

        Written as an explicit comparison against a single-pass version, so
        deleting ``while changed`` is a change some assertion here can see
        rather than one only the roster notices.
        """
        tree = ast.parse("A = Widget\nB = A\nC = B")
        single_pass = {"Widget"} | {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and name_of(node.value) == "Widget"
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        assert single_pass == {"Widget", "A"}
        assert construction_names("Widget", tree) == {"Widget", "A", "B", "C"}


# ---------------------------------------------------------------------------
# The roster, pinned
# ---------------------------------------------------------------------------


class TestTheRosterIsPinned:
    """A roster nobody counts is the artefact this module replaces."""

    #: Hand-read. Adding a shape is a deliberate act and should show up in
    #: a diff here, not slide in under a ``len(...) > 0``.
    EXPECTED_IDS = (
        "bare_call",
        "attribute_call",
        "dotted_attribute",
        "aliased_import",
        "local_rebinding",
        "rebinding_chain",
        "subclass_then_construct",
        "partial_binding",
        "cross_module_subclass",
        "inside_except",
        "nested_function",
        "lambda_body",
        "module_scope",
        "class_body",
        "async_function",
        "decorator",
        "kwargs_splat",
        "none_keyword",
        "second_file_bare_call",
    )

    def test_the_roster_is_exactly_these_shapes(self) -> None:
        assert EVASION_IDS == self.EXPECTED_IDS

    def test_ids_are_distinct(self) -> None:
        assert len(set(EVASION_IDS)) == len(EVASION_IDS)

    def test_every_shape_names_a_known_axis(self) -> None:
        assert {evasion.axis for evasion in EVASIONS} <= set(AXES)

    def test_every_axis_is_populated(self) -> None:
        """An axis with no shapes makes the anti-emptying bound a no-op."""
        for axis in AXES:
            assert [e for e in EVASIONS if e.axis == axis], axis

    def test_every_shape_carries_a_reason(self) -> None:
        thin = [e.id for e in EVASIONS if len(e.why.strip()) < 40]
        assert not thin, f"these shapes say nothing about why they hide: {thin}"

    def test_the_corpus_spans_two_files(self) -> None:
        """#464's defect is file discovery, so one file cannot probe it."""
        corpus = _corpus()
        assert set(corpus.files) == {PRIMARY_FILE, SECOND_FILE}
        assert [e.id for e in EVASIONS if e.file == SECOND_FILE], (
            "no shape lives in the second file, so single_file is unprobed"
        )

    def test_each_shape_lands_on_its_own_line(self) -> None:
        corpus = _corpus()
        assert sorted(corpus.lines) == sorted(EVASION_IDS)
        assert len(set(corpus.lines.values())) == len(corpus.lines)

    def test_line_numbers_are_globally_unique_across_the_corpus(self) -> None:
        """The second file's padding is load-bearing, not cosmetic.

        The predicate contract is a flat set of integers, so a second-file
        line colliding with a primary-file one would silently satisfy the
        wrong shape's requirement.
        """
        corpus = _corpus()
        everything = list(corpus.lines.values()) + list(corpus.decoys.values())
        assert len(set(everything)) == len(everything)

    def test_the_line_table_agrees_with_a_tokenizer_scan(self) -> None:
        """The renderer's arithmetic, checked by a method it does not share.

        ``render_evasion_corpus`` counts lines as it assembles;
        ``marker_lines`` re-derives the table from the finished text by
        tokenizing. Compared as a mapping, so a divergence names the
        *shape* rather than reporting two integers.
        """
        corpus = _corpus()
        recovered: dict[str, int] = {}
        for source in corpus.files.values():
            recovered.update(marker_lines(source))
        assert recovered == corpus.lines

    def test_every_marked_line_carries_a_real_call(self) -> None:
        """A shape a scan misses is only interesting if there is a call there.

        An entry that rendered to a comment, or to a line the parser drops,
        would be missed by every predicate and would silently make the
        guard unsatisfiable — the mirror image of one that passes for
        everybody.
        """
        corpus = _corpus()
        call_lines = {
            node.lineno
            for source in corpus.files.values()
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
        }
        missing = sorted(
            name for name, line in corpus.lines.items() if line not in call_lines
        )
        assert not missing, f"{missing} rendered to something that is not a call"

    def test_the_corpus_parses_and_substitutes_everything(self) -> None:
        corpus = _corpus()
        for source in corpus.files.values():
            ast.parse(source)
            assert "{SUBJECT}" not in source
            assert "{STMT}" not in source
            assert "{KWARG}" not in source


class TestTheDecoys:
    """The negative control the first cut did not have (#497 finding (a))."""

    EXPECTED_IDS: ClassVar[set[str]] = {
        "super_init",
        "unrelated_call",
        "subject_as_argument",
        "attribute_of_subject",
        "annotation_only",
        "wrapper_without_the_subject",
    }

    def test_the_decoy_roster_is_pinned(self) -> None:
        assert set(_corpus().decoys) == self.EXPECTED_IDS

    def test_every_decoy_is_a_real_call(self) -> None:
        """Otherwise no predicate could trip on one even if it were broken."""
        corpus = _corpus()
        call_lines = {
            node.lineno
            for source in corpus.files.values()
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
        }
        missing = sorted(
            name for name, line in corpus.decoys.items() if line not in call_lines
        )
        assert not missing, f"{missing} are not calls, so no scan can trip on them"

    def test_no_decoy_shares_a_line_with_a_shape(self) -> None:
        corpus = _corpus()
        assert not set(corpus.decoys.values()) & set(corpus.lines.values())

    def test_decoys_render_into_the_primary_file(self) -> None:
        assert not decoy_lines(_corpus().files[SECOND_FILE])

    def test_the_resolving_scan_reports_none_of_them(self) -> None:
        """The shared predicate is not merely wide — it is also correct."""
        corpus = _corpus()
        assert not set(corpus.decoys.values()) & _resolved_lines(corpus)

    def test_a_guard_with_no_decoys_is_refused(self, tmp_path: Path) -> None:
        """Passing an empty decoy roster is how this control gets removed."""
        with pytest.raises(AssertionError, match="needs at least one decoy"):
            assert_scan_is_not_vacuous(
                _perfect,
                EVASIONS,
                (),
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt=RESIDUE_EXEMPT,
            )

    def test_a_wrapped_rule_still_has_a_negative_control(self, tmp_path: Path) -> None:
        """#497's finding (a), one level up: the ``wrap=`` path.

        Every other decoy renders unwrapped, so a rule passing ``wrap=``
        has a predicate that *cannot* report one however broken it is —
        the decoy check is structurally dead for it, and every wrapper
        occurrence in the corpus sits on a marked line. That is the exact
        condition finding (a) was about.

        The predicate here reports every ``console.print`` and judges its
        argument not at all. It satisfies coverage perfectly and
        distinguishes nothing; only ``wrapper_without_the_subject``
        separates it from a correct scan.
        """

        def every_wrapper(root: Path) -> Iterable[int]:
            found: list[int] = []
            for path in sorted(root.glob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                found.extend(
                    node.lineno
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "print"
                )
            return found

        with pytest.raises(AssertionError, match="construct nothing"):
            assert_scan_is_not_vacuous(
                every_wrapper,
                subject=SUBJECT,
                kwarg=KWARG,
                wrap="console.print({call})",
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt=RESIDUE_EXEMPT,
            )

    def test_the_wrapped_decoy_is_unwrapped_for_a_plain_rule(self) -> None:
        """With the default ``wrap`` it is an ordinary non-offence call.

        Otherwise adding it would make the three rules that police a bare
        construction start reporting a decoy — a negative control that
        breaks the positive one is worse than none.
        """
        corpus = _corpus()
        line = corpus.decoys["wrapper_without_the_subject"]
        rendered = corpus.primary.splitlines()[line - 1]
        assert rendered.strip().startswith("_helper()")
        assert SUBJECT not in rendered


class TestTheRosterIsMeasuredAgainstRealNaiveScanners:
    """Every ``missed_by`` claim is executed, not read."""

    @staticmethod
    def _actual_misses(evasion: Evasion) -> set[str]:
        corpus = _corpus()
        line = corpus.lines[evasion.id]
        return {
            name
            for name, scanner in NAIVE_SCANNERS.items()
            if line not in scanner(corpus, SUBJECT, KWARG)
        }

    @pytest.mark.parametrize("evasion", EVASIONS, ids=EVASION_IDS)
    def test_declared_misses_are_the_real_ones(self, evasion: Evasion) -> None:
        actual = self._actual_misses(evasion)
        assert actual == evasion.missed_by, (
            f"{evasion.id} declares it defeats {sorted(evasion.missed_by)} but "
            f"actually defeats {sorted(actual)}. A declaration nobody runs is "
            f"the roster rot this module exists to end."
        )

    def test_exactly_one_shape_is_the_control(self) -> None:
        controls = [e.id for e in EVASIONS if not e.missed_by]
        assert controls == ["bare_call"], (
            "a shape no naive scanner misses is not an evasion; it is a "
            f"control, and there should be exactly one. Got {controls}"
        )

    @pytest.mark.parametrize("scanner_name", sorted(NAIVE_SCANNERS))
    def test_every_naive_scanner_is_defeated_by_something(
        self, scanner_name: str
    ) -> None:
        """A scanner nothing defeats is not naive, or the roster lost a shape."""
        assert [e.id for e in EVASIONS if scanner_name in e.missed_by], scanner_name

    def test_residue_is_exactly_the_shapes_no_scanner_reports(self) -> None:
        """The claim ``construction_names`` makes about itself, executed.

        The first cut of this module asserted three shapes unresolvable and
        was wrong about all three — ``test_builder_factory``'s resolver,
        merged for #488, already handled every one. This computes the
        residue instead of declaring it: a shape is residue precisely when
        the shared resolving scan does not report it.
        """
        corpus = _corpus()
        reported = _resolved_lines(corpus)
        unreported = {
            name for name, line in corpus.lines.items() if line not in reported
        }
        assert unreported == RESIDUE
        assert {e.id for e in EVASIONS if e.residue} == RESIDUE


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


class TestTheGuardFailsWhenItShould:
    """The crux. A guard nobody has watched fail is a guard nobody knows works."""

    def test_the_perfect_predicate_passes(self, tmp_path: Path) -> None:
        """The control: the guard is satisfiable at all."""
        assert_scan_is_not_vacuous(
            _perfect,
            subject=SUBJECT,
            kwarg=KWARG,
            tmp_path=tmp_path,
            live_population=9,
            floor=4,
        )

    def test_the_shared_resolving_scan_passes_with_only_residue_exempt(
        self, tmp_path: Path
    ) -> None:
        """The control that matters: the harness's own predicate clears its bar."""
        assert_scan_is_not_vacuous(
            _resolving_scan,
            subject=SUBJECT,
            kwarg=KWARG,
            tmp_path=tmp_path,
            live_population=9,
            floor=4,
            exempt=RESIDUE_EXEMPT,
        )

    @pytest.mark.parametrize("scanner_name", sorted(NAIVE_SCANNERS))
    def test_an_under_collecting_predicate_is_rejected(
        self, scanner_name: str, tmp_path: Path
    ) -> None:
        """Each naive scanner is a shape that shipped; each must be caught.

        The failure message is asserted to name the *shapes*, because a
        guard that reports "expected 19, got 15" sends the next author to
        count rather than to look.
        """
        expected = sorted(
            e.id
            for e in EVASIONS
            if scanner_name in e.missed_by and e.id not in RESIDUE
        )
        with pytest.raises(AssertionError) as excinfo:
            assert_scan_is_not_vacuous(
                _predicate_from(NAIVE_SCANNERS[scanner_name]),
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt=RESIDUE_EXEMPT,
            )
        message = str(excinfo.value)
        for shape in expected:
            assert shape in message, f"the failure did not name {shape}"

    def test_a_predicate_that_reports_every_call_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """#497's finding (a), and the reason the decoys exist.

        This predicate satisfies the coverage check perfectly — it reports
        a superset of every marked line — and constructs nothing. Only the
        negative control separates it from a correct scan.
        """
        with pytest.raises(AssertionError, match="construct nothing"):
            assert_scan_is_not_vacuous(
                _every_call_line,
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt=RESIDUE_EXEMPT,
            )

    def test_a_predicate_that_reports_unmarked_lines_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """The third check, reachable only past the first two.

        ``range(1, 400)`` would be stopped by the decoy check first, which
        is correct but proves the wrong thing — so this reports every
        marked line *and* one line that is neither a shape nor a decoy.
        A predicate can only get here by inventing sites, which is the
        remaining way to satisfy a superset test without seeing anything.
        """

        def marked_plus_one(root: Path) -> Iterable[int]:
            return [*_perfect(root), 1]

        with pytest.raises(AssertionError, match="carry no"):
            assert_scan_is_not_vacuous(
                marked_plus_one,
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt=RESIDUE_EXEMPT,
            )

    def test_a_predicate_that_reports_nothing_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(AssertionError, match="bare_call"):
            assert_scan_is_not_vacuous(
                lambda root: (),
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt=RESIDUE_EXEMPT,
            )


class TestEveryShapeIsIndividuallyLoadBearing:
    """Removing any one entry lets an under-collecting predicate through.

    Demonstrated per shape rather than asserted once, and in both
    directions — the entry present must *fail* the blind predicate and the
    entry removed must *pass* it. Only the pair proves the entry is what
    did the catching; the first half alone is satisfied by a roster whose
    entries are all redundant with one another.
    """

    @pytest.mark.parametrize("target", EVASION_IDS)
    def test_the_shape_catches_a_predicate_blind_to_it(
        self, target: str, tmp_path: Path
    ) -> None:
        with pytest.raises(AssertionError) as excinfo:
            assert_scan_is_not_vacuous(
                _blind_to(target),
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt={name: REASON for name in RESIDUE if name != target},
            )
        assert target in str(excinfo.value)

    @pytest.mark.parametrize("target", EVASION_IDS)
    def test_dropping_the_shape_lets_that_predicate_through(
        self, target: str, tmp_path: Path
    ) -> None:
        assert_scan_is_not_vacuous(
            _blind_to(target),
            [e for e in EVASIONS if e.id != target],
            subject=SUBJECT,
            kwarg=KWARG,
            tmp_path=tmp_path,
            live_population=9,
            floor=4,
            exempt={name: REASON for name in RESIDUE if name != target},
            roster_reason=(
                "the leave-one-out proof removes exactly this shape on "
                "purpose, to show it is what did the catching"
            ),
        )


class TestExemptionsCannotEmptyTheGuard:
    """#497's findings (e) and (f): the guard passed while seeing nothing."""

    def test_the_whole_roster_cannot_be_exempted(self, tmp_path: Path) -> None:
        """(e) verbatim: report nothing, exempt everything, pass.

        The **control** rule is what refuses it, because
        ``_validate_exemptions`` checks the control before the residue
        rule and ``bare_call`` is in any whole-roster exemption. Matched
        explicitly rather than bare, so this cannot start passing for some
        unrelated reason; the residue rule has its own parametrised test
        over all sixteen resolvable shapes.
        """
        with pytest.raises(AssertionError, match="cannot be exempted"):
            assert_scan_is_not_vacuous(
                lambda root: (),
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt=dict.fromkeys(EVASION_IDS, REASON),
            )

    def test_twenty_x_characters_are_not_a_reason(self, tmp_path: Path) -> None:
        """(f) verbatim. Length alone was never enough; words are checked too."""
        with pytest.raises(AssertionError, match="give no reason"):
            assert_scan_is_not_vacuous(
                _perfect,
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt={"partial_binding": "x" * 20},
            )

    def test_a_short_multi_word_reason_is_refused(self, tmp_path: Path) -> None:
        """The length half of ``_reason_is_written``, pinned on its own.

        Survived the first mutation battery: every reason under test was
        either one word (``"x" * 20``) or zero words (whitespace), so both
        were killed by the *word* check and setting ``_MIN_REASON_CHARS``
        to zero changed nothing. Four words in seven characters separates
        them.
        """
        with pytest.raises(AssertionError, match="give no reason"):
            assert_scan_is_not_vacuous(
                _perfect,
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt={"partial_binding": "a b c d"},
            )

    def test_padding_cannot_buy_the_length(self, tmp_path: Path) -> None:
        """The ``strip`` in ``_reason_is_written``, pinned on its own.

        Also survived the first battery, and for the same reason: the only
        whitespace case was *all* whitespace, which the word check refuses
        either way. Stripping matters only when the content is real prose
        that is too short and the padding pushes it past the character
        floor — four words, seven characters, twenty trailing spaces.
        """
        with pytest.raises(AssertionError, match="give no reason"):
            assert_scan_is_not_vacuous(
                _perfect,
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt={"partial_binding": "a b c d" + " " * 20},
            )

    def test_whitespace_padding_is_not_a_reason(self, tmp_path: Path) -> None:
        """The ``strip`` in ``_reason_is_written``, pinned.

        It was the only thing stopping forty spaces and it was untested — a
        mutant measuring the reason *before* stripping survived the whole
        suite, because the one test that exercised the check used ``"n/a"``,
        which fails on length either way.
        """
        with pytest.raises(AssertionError, match="give no reason"):
            assert_scan_is_not_vacuous(
                _perfect,
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt={"partial_binding": " " * 40},
            )

    def test_the_control_cannot_be_exempted(self, tmp_path: Path) -> None:
        """``bare_call`` is what stops an empty result reading as green.

        The first cut *pinned this as allowed* — a defended design whose
        defence was reviewer attention, in a module whose premise is that
        reviewer attention missed this class four times.
        """
        with pytest.raises(AssertionError, match="cannot be exempted"):
            assert_scan_is_not_vacuous(
                _perfect,
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt={"bare_call": REASON},
            )

    @pytest.mark.parametrize(
        "target", [e.id for e in EVASIONS if not e.residue and e.missed_by]
    )
    def test_a_resolvable_shape_cannot_be_exempted(
        self, target: str, tmp_path: Path
    ) -> None:
        """The B1 fix, enforced rather than documented.

        The first cut let a rule declare blindness to an import alias — a
        shape ``construction_names`` resolves — which taught every future
        rule to write an exemption instead of lifting twenty lines that
        already existed on ``main``.
        """
        with pytest.raises(AssertionError, match="resolvable, not residue"):
            assert_scan_is_not_vacuous(
                _perfect,
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt={target: REASON},
            )

    def test_an_axis_cannot_be_emptied(self, tmp_path: Path) -> None:
        """An axis is the smallest unit of the roster that has to survive.

        Reachable only for a roster a caller *narrowed*, and that is worth
        stating precisely because the first version of this test did not
        reach it at all: on the full roster every exemptible (residue)
        shape sits on the ``binding`` axis, which has six members, so the
        residue rule refuses any whole-axis exemption first and a mutant
        deleting the axis check survived the entire suite.

        ``evasions=`` is a real parameter, so a residue-only subset is a
        real caller. Here it is the whole roster, and exempting both
        empties ``binding``.
        """
        subset = [e for e in EVASIONS if e.residue or e.id == "bare_call"]
        assert {e.axis for e in subset} == {"binding", "spelling"}
        with pytest.raises(AssertionError, match="'binding' axis"):
            assert_scan_is_not_vacuous(
                _perfect,
                subset,
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt=RESIDUE_EXEMPT,
                roster_reason=(
                    "a residue-only subset, narrowed to reach the axis "
                    "rule the full roster refuses before"
                ),
            )

    def test_the_axis_rule_is_unreachable_on_the_full_roster(self) -> None:
        """Why the test above narrows the roster, stated as an assertion.

        If a future edit makes some axis's only members residue, this fails
        and the reader is told the axis rule has become reachable by
        default — which is a change in what the guard means, not a
        housekeeping detail.
        """
        for axis in AXES:
            shapes = [e for e in EVASIONS if e.axis == axis]
            exemptible = [e for e in shapes if e.residue]
            assert len(exemptible) < len(shapes), (
                f"every shape on '{axis}' is now exemptible, so the axis "
                f"rule fires on the full roster; re-read its test"
            )

    def test_an_unknown_id_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(AssertionError, match="not shapes in this roster"):
            assert_scan_is_not_vacuous(
                _perfect,
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt={"typo_shape": REASON},
            )


class TestNarrowingTheRosterIsBoundedToo:
    """The #497 re-gate's finding: every bound above is on ``exempt=``.

    ``evasions=`` is a documented, positional parameter that reduces the
    same set with none of them — no control rule, no residue rule, no axis
    rule and no reason. Dropping a shape and exempting it are the same act
    with the same consequence, so the cheaper spelling must not be the
    unbounded one.
    """

    def test_an_empty_roster_is_refused(self, tmp_path: Path) -> None:
        """Total vacuity, previously reachable by one argument.

        ``evasions=[]`` with a predicate that reports nothing passed every
        check: there were no shapes to miss, and the decoy and spurious
        checks are satisfied by reporting nothing at all.
        """
        with pytest.raises(AssertionError, match="needs a roster"):
            assert_scan_is_not_vacuous(
                lambda root: (),
                [],
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
            )

    def test_a_narrowed_roster_without_a_reason_is_refused(
        self, tmp_path: Path
    ) -> None:
        """And the message names the shapes that were dropped."""
        with pytest.raises(AssertionError) as excinfo:
            assert_scan_is_not_vacuous(
                _perfect,
                [e for e in EVASIONS if e.id == "bare_call"],
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
            )
        message = str(excinfo.value)
        assert "attribute_call" in message
        assert "second_file_bare_call" in message

    def test_the_shape_that_motivated_this_is_refused(self, tmp_path: Path) -> None:
        """#488's own predicate, narrowed to the one shape it handles.

        This is the concrete escape: an ``ast.Name``-only scan — the
        defect #490 was filed about — cleared the guard by being measured
        against a roster containing only ``bare_call``. Nothing about it
        looked like an exemption.
        """

        def name_only(root: Path) -> Iterable[int]:
            found: list[int] = []
            for path in sorted(root.glob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                found.extend(
                    node.lineno
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == SUBJECT
                )
            return found

        with pytest.raises(AssertionError, match="drops"):
            assert_scan_is_not_vacuous(
                name_only,
                [e for e in EVASIONS if e.id == "bare_call"],
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
            )

    def test_a_thin_roster_reason_is_refused(self, tmp_path: Path) -> None:
        """Same prose bar as an exemption's, pinned on both halves and the strip.

        ``"x" * 30`` is long enough and one word; ``"a b c d"`` is four
        words and seven characters; the third is that same fragment padded
        to twenty-seven, which clears the length test only if the reason is
        measured before ``strip``.
        """
        for reason in ("x" * 30, "a b c d", "a b c d" + " " * 20):
            with pytest.raises(AssertionError, match="drops"):
                assert_scan_is_not_vacuous(
                    _perfect,
                    [e for e in EVASIONS if e.id != "decorator"],
                    subject=SUBJECT,
                    kwarg=KWARG,
                    tmp_path=tmp_path,
                    live_population=9,
                    floor=4,
                    exempt=RESIDUE_EXEMPT,
                    roster_reason=reason,
                )

    def test_a_narrowed_roster_with_a_written_reason_is_accepted(
        self, tmp_path: Path
    ) -> None:
        """The parameter still works; it just has to say why.

        A rule whose subject cannot render a shape at all is real — the
        roster models a call, not every rule's offence — so this is a
        reason requirement rather than a refusal, exactly as
        ``sole_site_reason`` is for a floor of one.
        """
        assert_scan_is_not_vacuous(
            _perfect,
            [e for e in EVASIONS if e.id != "decorator"],
            subject=SUBJECT,
            kwarg=KWARG,
            tmp_path=tmp_path,
            live_population=9,
            floor=4,
            exempt=RESIDUE_EXEMPT,
            roster_reason="this rule's offence cannot appear in a decorator",
        )

    def test_the_full_roster_needs_no_reason(self, tmp_path: Path) -> None:
        """The default path is unchanged, which is what keeps the bound cheap."""
        assert_scan_is_not_vacuous(
            _perfect,
            EVASIONS,
            subject=SUBJECT,
            kwarg=KWARG,
            tmp_path=tmp_path,
            live_population=9,
            floor=4,
        )


class TestTheFloorStaysHandRead:
    """#466's defect, refused structurally rather than by convention."""

    def test_a_zero_floor_is_refused(self) -> None:
        with pytest.raises(AssertionError, match="stopped matching"):
            assert_hand_read_floor(50, 0, subject="whatever")

    def test_a_floor_of_one_is_refused_without_a_reason(self) -> None:
        with pytest.raises(AssertionError, match="stopped matching"):
            assert_hand_read_floor(50, 1, subject="whatever")

    def test_a_floor_of_one_is_accepted_with_a_written_reason(self) -> None:
        """#466's floor was wrong because the *scan* computed it, not because
        it was small. A genuinely single-site rule is legitimate and a hard
        refusal would have blocked one — ``test_policy_gate_rule`` already
        floors at exactly two.
        """
        assert_hand_read_floor(
            3,
            1,
            subject="whatever",
            sole_site_reason="there is one factory and a second one is the defect",
        )

    def test_a_sole_site_reason_must_itself_be_prose(self) -> None:
        with pytest.raises(AssertionError, match="stopped matching"):
            assert_hand_read_floor(3, 1, subject="whatever", sole_site_reason="x" * 30)

    def test_a_population_below_the_floor_fails(self) -> None:
        with pytest.raises(AssertionError, match="below the hand-read floor"):
            assert_hand_read_floor(3, 12, subject="whatever")

    def test_a_population_one_below_the_floor_fails(self) -> None:
        """The off-by-one the #497 gate found unpinned.

        Only ``population == floor`` (12, 12) and a far-below case (3, 12)
        were covered, so a mutant comparing against ``floor // 2`` survived
        the entire suite. Nobody pinned ``floor - 1``.
        """
        with pytest.raises(AssertionError, match="below the hand-read floor"):
            assert_hand_read_floor(11, 12, subject="whatever")

    def test_a_halved_floor_would_be_visible(self) -> None:
        """The mutant stated directly: 6 must not clear a floor of 12."""
        with pytest.raises(AssertionError, match="below the hand-read floor"):
            assert_hand_read_floor(6, 12, subject="whatever")

    def test_a_population_at_the_floor_passes(self) -> None:
        assert_hand_read_floor(12, 12, subject="whatever")

    def test_the_guard_floors_before_it_renders(self, tmp_path: Path) -> None:
        """A drifted scan fails on its population, not on the synthetic tree.

        Order matters for the message: a rule whose scan has stopped
        matching the real tree should be told that, not handed a list of
        evasion shapes it also failed to find.
        """
        with pytest.raises(AssertionError, match="below the hand-read floor"):
            assert_scan_is_not_vacuous(
                _perfect,
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=1,
                floor=4,
            )
