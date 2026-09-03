"""Proof that the shared vacuity harness is not itself vacuous.

:mod:`tests.ast_rules` exists because four AST rules shipped evadable for
the same reason — the vacuity guard divided by the scan's own output. A
*shared* harness with that property would be strictly worse than none,
because it launders confidence: every rule adopting it would inherit one
guard that passes for everybody.

So the harness is held to the standard it imposes. Four claims, each
demonstrated rather than asserted:

#. :func:`~tests.ast_rules.assert_scan_is_not_vacuous` **fails** when
   handed a deliberately under-collecting predicate — one per member of
   :data:`~tests.ast_rules.NAIVE_SCANNERS`, each a shape that really
   shipped here, and the failure must name the shape rather than a line
   number.
#. Every :data:`~tests.ast_rules.EVASIONS` entry is **individually
   load-bearing**: a predicate blind to exactly that one shape fails the
   full roster and passes the roster with the entry removed. Removing any
   entry therefore lets an under-collecting predicate through, which is
   what "load-bearing" has to mean if it means anything.
#. The roster's own claims are checkable. Each entry declares which naive
   scanners it defeats and those declarations are **run**, not read; the
   marker line of each shape is recovered a second time by tokenizing, so
   a tree-shaped accounting bug in the renderer cannot hide behind the
   renderer's own arithmetic; and each marked line is confirmed to carry a
   real call.
#. The one predicate that would satisfy the "every shape is reported"
   check trivially — one that reports *every* line — is refused, which is
   why the harness also asserts that nothing unmarked was reported.

The model for all of this is
``tests/unit/mcp/test_capture_surface_roster.py``, which cross-checks its
AST scan against a tokenizer scan over line numbers and then proves that
cross-check by re-introducing #457's bug.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from tests.ast_rules import (
    EVASION_IDS,
    EVASIONS,
    NAIVE_SCANNERS,
    Evasion,
    assert_hand_read_floor,
    assert_scan_is_not_vacuous,
    calls_named,
    construction_sites,
    is_call_to,
    iter_modules,
    marker_lines,
    name_of,
    render_evasion_module,
)

#: A stand-in name with no meaning to any rule, so nothing here passes
#: because it happened to match the real tree.
SUBJECT = "Widget"
KWARG = "gate"


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src"
    assert root.is_dir(), f"src/ not found at {root}"
    return root


def _rendered() -> tuple[str, dict[str, int]]:
    return render_evasion_module(subject=SUBJECT, kwarg=KWARG)


def _predicate_from(
    scanner: Callable[[str, str, str], set[int]],
) -> Callable[[Path], Iterable[int]]:
    """Adapt a naive scanner to the ``(root) -> line numbers`` contract."""

    def predicate(root: Path) -> Iterable[int]:
        return scanner(
            (root / "evasions.py").read_text(encoding="utf-8"), SUBJECT, KWARG
        )

    return predicate


def _perfect(root: Path) -> Iterable[int]:
    """Reports exactly the marked lines, by reading the markers.

    Not an AST scan at all, which is the point: the reference the
    leave-one-out proof subtracts from must not share a method with the
    thing being proved.
    """
    return marker_lines((root / "evasions.py").read_text(encoding="utf-8")).values()


def _blind_to(target: str) -> Callable[[Path], Iterable[int]]:
    """:func:`_perfect` minus exactly one shape.

    Computed from the file it is handed rather than from a line table, so
    it survives the reflow that removing a roster entry causes — the
    hand-written line tables in the existing rules are precisely what
    makes those rosters awkward to edit.
    """

    def predicate(root: Path) -> Iterable[int]:
        source = (root / "evasions.py").read_text(encoding="utf-8")
        return [line for name, line in marker_lines(source).items() if name != target]

    return predicate


# ---------------------------------------------------------------------------
# The shared predicate
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
        """``test_machine_output_rule``'s pair, checked before it is retired.

        It reads ``getattr(func, "attr", None)`` and ``getattr(func, "id",
        None)`` and tests both for membership. Equivalent for every node
        kind Python produces — no AST node carries both attributes — but
        "equivalent" is the claim the conversion rests on, so it is checked
        against the whole tree rather than reasoned about.
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
                mine = {name_of(node.func)} - {None}
                if pair != mine:
                    disagreements.append(f"{path.name}:{node.lineno} {pair} != {mine}")
        assert not disagreements, disagreements[:10]

    def test_agrees_with_the_isinstance_spelling_across_src(self) -> None:
        """``test_capture_surface_roster``'s ``_calls_named``, same check."""

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
                if name is None:
                    continue
                if old(node, name) is not is_call_to(node, name):
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
        assert all(site.lineno == 1 for site in sites)


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
        "subclass_then_construct",
        "kwargs_splat",
        "none_keyword",
        "inside_except",
        "nested_function",
        "lambda_body",
    )

    def test_the_roster_is_exactly_these_shapes(self) -> None:
        assert EVASION_IDS == self.EXPECTED_IDS

    def test_ids_are_distinct(self) -> None:
        assert len(set(EVASION_IDS)) == len(EVASION_IDS)

    def test_every_shape_carries_a_reason(self) -> None:
        thin = [e.id for e in EVASIONS if len(e.why.strip()) < 40]
        assert not thin, f"these shapes say nothing about why they hide: {thin}"

    def test_each_shape_lands_on_its_own_line(self) -> None:
        _source, at = _rendered()
        assert sorted(at) == sorted(EVASION_IDS)
        assert len(set(at.values())) == len(at), "two shapes share a line"

    def test_the_renderers_line_table_agrees_with_a_tokenizer_scan(self) -> None:
        """The renderer's arithmetic, checked by a method it does not share.

        ``render_evasion_module`` counts lines as it assembles them.
        ``marker_lines`` re-derives the same table from the finished text
        by tokenizing. Compared as a mapping so a divergence names the
        *shape*, not two integers — the lesson
        ``test_capture_surface_roster`` records about count equality.
        """
        source, at = _rendered()
        assert marker_lines(source) == at

    def test_every_marked_line_carries_a_real_call(self) -> None:
        """ "A shape a scan misses" is only interesting if there is a call there.

        A roster entry that rendered to a comment, or to a line the parser
        drops, would be missed by every predicate and would silently make
        the guard unsatisfiable — the mirror image of a guard that passes
        for everybody.
        """
        source, at = _rendered()
        tree = ast.parse(source)
        calls_by_line = {
            node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
        }
        missing = sorted(name for name, line in at.items() if line not in calls_by_line)
        assert not missing, f"{missing} rendered to something that is not a call"

    def test_the_rendered_module_parses(self) -> None:
        source, _at = _rendered()
        ast.parse(source)

    def test_the_subject_is_substituted_everywhere(self) -> None:
        source, _at = _rendered()
        assert "{SUBJECT}" not in source
        assert "{STMT}" not in source
        assert "{KWARG}" not in source


class TestTheRosterIsMeasuredAgainstRealNaiveScanners:
    """Every ``missed_by`` claim is executed, not read."""

    @staticmethod
    def _actual_misses(evasion: Evasion) -> set[str]:
        source, at = _rendered()
        line = at[evasion.id]
        return {
            name
            for name, scanner in NAIVE_SCANNERS.items()
            if line not in scanner(source, SUBJECT, KWARG)
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
        """A naive scanner nothing defeats is not naive, or the roster is thin."""
        defeated = [e.id for e in EVASIONS if scanner_name in e.missed_by]
        assert defeated, (
            f"no shape in the roster defeats {scanner_name}; either it is not "
            f"actually a naive predicate or the roster has lost the shape "
            f"that used to catch it"
        )


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

    @pytest.mark.parametrize("scanner_name", sorted(NAIVE_SCANNERS))
    def test_an_under_collecting_predicate_is_rejected(
        self, scanner_name: str, tmp_path: Path
    ) -> None:
        """Each naive scanner is a shape that shipped; each must be caught.

        The failure message is asserted to name the *shapes*, because a
        guard that reports "expected 11, got 8" sends the next author to
        count rather than to look.
        """
        expected = sorted(e.id for e in EVASIONS if scanner_name in e.missed_by)
        with pytest.raises(AssertionError) as excinfo:
            assert_scan_is_not_vacuous(
                _predicate_from(NAIVE_SCANNERS[scanner_name]),
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
            )
        message = str(excinfo.value)
        for shape in expected:
            assert shape in message, f"the failure did not name {shape}"

    def test_a_predicate_that_reports_every_line_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """The way a guard checking only "⊇ required" passes for nothing.

        Reporting every line satisfies the coverage half trivially, which
        is why the unmarked lines of the rendered module are asserted
        clean too.
        """
        with pytest.raises(AssertionError, match="carry no"):
            assert_scan_is_not_vacuous(
                lambda root: range(1, 400),
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
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
        )

    @pytest.mark.parametrize("target", EVASION_IDS)
    def test_exempting_the_shape_lets_that_predicate_through(
        self, target: str, tmp_path: Path
    ) -> None:
        """The supported way to keep a blind spot: name it and say why.

        Same effect as deleting the entry, but the exemption is a line in
        the adopting rule's source that a reviewer reads — which is the
        entire difference between this and the four rules that shipped
        with the blind spot simply unrendered.
        """
        assert_scan_is_not_vacuous(
            _blind_to(target),
            subject=SUBJECT,
            kwarg=KWARG,
            tmp_path=tmp_path,
            live_population=9,
            floor=4,
            exempt={target: "a reason long enough to be an actual sentence"},
        )


class TestExemptionsCannotRot:
    def test_an_unknown_id_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(AssertionError, match="not shapes in this roster"):
            assert_scan_is_not_vacuous(
                _perfect,
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt={"typo_shape": "a reason long enough to be a sentence"},
            )

    def test_a_one_word_reason_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(AssertionError, match="give no reason"):
            assert_scan_is_not_vacuous(
                _perfect,
                subject=SUBJECT,
                kwarg=KWARG,
                tmp_path=tmp_path,
                live_population=9,
                floor=4,
                exempt={"aliased_import": "n/a"},
            )


class TestTheFloorStaysHandRead:
    """#466's defect, refused structurally rather than by convention."""

    def test_a_zero_floor_is_refused(self) -> None:
        with pytest.raises(AssertionError, match="is not a floor"):
            assert_hand_read_floor(50, 0, subject="whatever")

    def test_a_floor_of_one_is_refused(self) -> None:
        with pytest.raises(AssertionError, match="is not a floor"):
            assert_hand_read_floor(50, 1, subject="whatever")

    def test_a_population_below_the_floor_fails(self) -> None:
        with pytest.raises(AssertionError, match="below the hand-read floor"):
            assert_hand_read_floor(3, 12, subject="whatever")

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
