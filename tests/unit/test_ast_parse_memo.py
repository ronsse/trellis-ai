"""The shared parse memo must hand rules trees nobody has altered.

``tests/ast_rules.py`` routes ``ast.parse`` calls made from test code through
one memo, so every rule that scans ``src/`` shares a tree per file instead
of re-parsing it. A shared tree is only safe while nobody edits it, and an
edit made by one rule would reach every rule that runs after it. This file
pins both halves: the memo is scoped so that nothing outside test code sees
a shared tree, and the check that notices an edited tree actually notices
one.

The check is proven the way ``docs/agent-guide/testing.md`` asks for any
guard: it is run against deliberately mutating subjects and watched to fail,
and against read-only subjects and watched to stay quiet. A guard that
passes on both proves nothing.
"""

from __future__ import annotations

import ast
import types

import pytest

from tests.ast_rules import (
    SHARED_PARSE_MEMO,
    ParseMemo,
    calls_named,
    construction_names,
)

SOURCE = "x = 1\n\n\ndef make():\n    return Subject(x)\n"


def _delete_a_statement(tree: ast.Module) -> None:
    del tree.body[0]


def _rewrite_a_field(tree: ast.Module) -> None:
    assign = tree.body[0]
    assert isinstance(assign, ast.Assign)
    assert isinstance(assign.value, ast.Constant)
    assign.value.value = 99


def _inline_a_name_in_place(tree: ast.Module) -> None:
    """The shape a rule shipped with: ``NodeTransformer.visit`` edits its input."""

    class Inliner(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.expr:
            return ast.Constant(value=0) if node.id == "x" else node

    Inliner().visit(tree)


def _annotate_every_node(tree: ast.Module) -> None:
    """A back-reference no ``ast.dump`` prints, hung on every node."""
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent  # type: ignore[attr-defined]


def _scrub_annotations(tree: ast.Module) -> None:
    """Undo :func:`_annotate_every_node`, which leaks past the tree it was given.

    ``Load``, ``Store`` and ``Del`` are single objects shared by every tree the
    interpreter parses, so an attribute left on one stays on it for the rest of
    the session and reaches every other tree.
    """
    for node in ast.walk(tree):
        node.__dict__.pop("parent", None)


def _shift_every_line(tree: ast.Module) -> None:
    ast.increment_lineno(tree, 3)


MUTATIONS = [
    _delete_a_statement,
    _rewrite_a_field,
    _inline_a_name_in_place,
    _shift_every_line,
]


def _walk_it(tree: ast.Module) -> None:
    list(ast.walk(tree))


def _unparse_it(tree: ast.Module) -> None:
    ast.unparse(tree)


def _dump_it(tree: ast.Module) -> None:
    ast.dump(tree, include_attributes=True)


def _scan_it(tree: ast.Module) -> None:
    calls_named("Subject", tree)
    construction_names("Subject", tree)


def _compile_it(tree: ast.Module) -> None:
    compile(tree, "<memo-proof>", "exec")


READ_ONLY = [_walk_it, _unparse_it, _dump_it, _scan_it, _compile_it]


# -- the check notices an edited tree, and only an edited tree ---------------


@pytest.mark.parametrize("mutate", MUTATIONS, ids=lambda fn: fn.__name__)
def test_the_check_reports_a_tree_a_rule_edited(mutate) -> None:
    memo = ParseMemo()
    tree = memo.parse(SOURCE)
    assert memo.altered() == [], "a tree nobody has touched must read as pristine"

    mutate(tree)

    report = memo.altered()
    assert len(report) == 1, f"{mutate.__name__} went unnoticed"
    assert "x = 1" in report[0], "the report must name the source it found altered"


def test_an_annotation_on_every_node_is_reported_once_not_once_per_tree() -> None:
    """The context nodes are shared by every tree, so one annotation reaches all.

    Listing every tree would bury the cause under a thousand lines.
    """
    memo = ParseMemo()
    tree = memo.parse(SOURCE)
    for n in range(8):
        memo.parse(f"other_{n} = {n}\n")
    try:
        _annotate_every_node(tree)
        report = memo.altered()
    finally:
        _scrub_annotations(tree)

    assert report, "an annotation on every node went unnoticed"
    assert len(report) <= 3, "one line per shared context node, never one per tree"
    assert all("shared by every tree" in line for line in report), report
    assert memo.altered() == [], "scrubbing the annotation must restore every tree"


@pytest.mark.parametrize("read", READ_ONLY, ids=lambda fn: fn.__name__)
def test_the_check_stays_quiet_for_a_rule_that_only_reads(read) -> None:
    memo = ParseMemo()
    read(memo.parse(SOURCE))
    assert memo.altered() == []


def test_the_check_reads_every_tree_not_just_the_first() -> None:
    memo = ParseMemo()
    clean = memo.parse("untouched = 1\n")
    dirty = memo.parse(SOURCE)
    _delete_a_statement(dirty)
    report = memo.altered()
    assert len(report) == 1
    assert "x = 1" in report[0]
    assert memo.altered(sources=["untouched = 1\n"]) == []
    assert clean is memo.parse("untouched = 1\n")


def test_a_tree_too_deep_to_fingerprint_is_still_parsed_and_shared() -> None:
    """The guard fails open on such a tree; it must not fail the parse."""
    memo = ParseMemo()
    source = "+".join(["deep_operand"] * 500) + "\n"
    tree = memo.parse(source)
    assert memo.parse(source) is tree
    assert memo.altered() == []


# -- the memo is wired in, and scoped to test code ---------------------------


def test_a_mutating_rule_trips_the_check_through_the_installed_wrapper() -> None:
    """End to end: conftest installs the wrapper, a rule edits a tree it got."""
    source = "def memo_wiring_probe():\n    return [1, 2]\n"
    try:
        tree = ast.parse(source)
        assert ast.parse(source) is tree
        assert SHARED_PARSE_MEMO.altered(sources=[source]) == []

        tree.body[0].body.clear()

        assert len(SHARED_PARSE_MEMO.altered(sources=[source])) == 1
    finally:
        SHARED_PARSE_MEMO.drop(source)  # leave the session-end check clean


def test_every_tree_a_rule_was_handed_this_session_is_pristine() -> None:
    """The live guard: any rule that ran before this one and edited a tree fails here.

    The session fixture in ``tests/conftest.py`` runs the same check after the
    last test, which is what covers a rule that runs after this one.
    """
    assert SHARED_PARSE_MEMO.altered() == [], (
        "a test mutated a tree returned by ast.parse; those trees are shared, so "
        "copy.deepcopy the subtree before changing it (see tests/ast_rules.py)"
    )


def test_distinct_arguments_do_not_share_a_tree() -> None:
    source = "a.b\n"
    assert ast.parse(source) is ast.parse(source)
    assert isinstance(ast.parse(source, mode="eval"), ast.Expression)
    assert isinstance(ast.parse(source), ast.Module)
    assert ast.parse(source, mode="eval") is not ast.parse(source)
    assert ast.parse(source, filename="one.py") is not ast.parse(source)


def test_a_syntax_error_is_raised_every_time_and_never_cached() -> None:
    held = len(SHARED_PARSE_MEMO)
    for _ in range(2):
        with pytest.raises(SyntaxError):
            ast.parse("def (:\n")
    assert len(SHARED_PARSE_MEMO) == held


def test_sources_other_than_text_are_not_shared() -> None:
    held = len(SHARED_PARSE_MEMO)
    assert ast.parse(b"y = 2\n") is not ast.parse(b"y = 2\n")
    assert len(SHARED_PARSE_MEMO) == held


def _parse_text(source: str) -> ast.AST:
    return ast.parse(source)


def test_a_caller_outside_test_code_always_gets_a_fresh_tree() -> None:
    """The scoping that keeps pytest's assertion rewriter and src/ out of the memo.

    The same function, run with a ``__name__`` that is not under ``tests``, is
    a stand-in for any other module that calls ``ast.parse``: it gets the real
    one.
    """
    source = "outsider_probe = 1\n"
    held = len(SHARED_PARSE_MEMO)
    for module in ("trellis.synthetic", "_pytest.assertion.rewrite", "scripts.audit"):
        outsider = types.FunctionType(
            _parse_text.__code__, {"__name__": module, "ast": ast}
        )
        assert outsider(source) is not outsider(source), module
    assert len(SHARED_PARSE_MEMO) == held

    insider = types.FunctionType(
        _parse_text.__code__, {"__name__": "tests.synthetic", "ast": ast}
    )
    try:
        assert insider(source) is insider(source)
        assert len(SHARED_PARSE_MEMO) == held + 1
    finally:
        SHARED_PARSE_MEMO.drop(source)


def test_the_standard_library_parsing_for_itself_is_untouched() -> None:
    """``ast.literal_eval`` looks ``parse`` up in ``ast``'s own globals."""
    held = len(SHARED_PARSE_MEMO)
    assert ast.literal_eval("[1, {2: 3}]") == [1, {2: 3}]
    assert len(SHARED_PARSE_MEMO) == held
