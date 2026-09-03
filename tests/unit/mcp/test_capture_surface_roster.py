"""Roster guard: every boundary-rejection site must have a clearable accept.

Two capture-health labels shipped structurally unclearable (#309, fixed in
#461). The mechanism was not subtle — a warning clears on ``MUTATION_EXECUTED`` with
the surface's ``requested_by``, so any surface that never emits one warns
forever — but it survived because the roster of surfaces that *can* raise
the banner was never enumerated against the roster of surfaces that can
clear it. That is #443's shape run again: three control keys declared
against six actual sites.

This module derives the first roster from the source and checks it against
the second **by execution**:

#. :func:`_scan_sites` enumerates every ``_record_boundary_rejection`` call
   in ``trellis/mcp/server.py`` and reads its ``tool=`` literal. Nothing is
   hand-listed — a hand-list is the artefact that rots.
#. Every tool must be *classified*: either named in
   :data:`~trellis.ops.capture_health.NON_CAPTURE_SURFACES` (it captures no
   experience, so it never raises a capture banner) or paired with a
   recipe in :data:`SUCCESS_CALL` for a successful call.
#. For each capture surface, the recipe is **run** against a live registry
   seeded with rejections, and the banner must actually clear. That is what
   makes this more than a declaration: a roster entry naming an event the
   tool does not emit — exactly #461's ``save_memory`` half, whose only
   ``MUTATION_EXECUTED`` sits behind a default-off flag — fails here.

**Guarding the guard.** A scanner that under-collects turns every
assertion below vacuous, and #457 shipped three vacuity guards that all
stayed green while its scan dropped 148 branches to 123 — because the
ratio's denominator came from the population the bug had truncated. Four
things answer that here:

#. The AST walk is cross-checked against a **tokenizer** scan of the same
   calls (:func:`_token_site_lines`). The tokenizer never builds a tree, so
   a tree-shaped scanning bug cannot reach it — the cross-check does not
   share the subject's method, which is the only kind of cross-check worth
   having.
#. The comparison is over **line numbers, not counts**, so a divergence
   *names the sites it missed* rather than reporting two integers. A count
   equality would also be satisfied by a scan that dropped one site and
   invented another.
#. That cross-check is itself proved by re-introducing an under-collection
   bug — :func:`_scan_sites_blind_to_except_handlers`, #457's own shape —
   and asserting the check fails *and* names the dropped line
   (:class:`TestTheCrossCheckCatchesUnderCollection`). The injection is
   asserted to have actually dropped something first, so the proof cannot
   itself go vacuous.
#. A dynamic (non-literal) ``tool=`` is a hard failure rather than a silent
   skip, and the derived list is asserted non-empty before anything is
   parametrised over it.

**Coverage, stated exactly.** This module's roster covers the boundary
rejections raised through ``server.py``'s ``_record_boundary_rejection``
wrapper. :class:`TestNoUnwatchedRejectionProducer` is what stops that from
being a smaller claim than it looks: it scans **all of** ``src/`` for
``record_write_rejection`` calls and fails on any producing module that is
not either this wrapper or explicitly classified. Without it, a new
producer in a new module would raise a banner no roster had ever looked at
— the silent false negative this whole check exists to prevent.

What this does **not** prove: that a declared accept event is emitted on
every successful path of its tool, only on the one the recipe exercises. A
``save_memory`` whose every call deduplicates emits nothing, for instance.
That residue is named in the issue and is bounded — a surface with no
successful calls at all has no rejections to clear either.
"""

from __future__ import annotations

import ast
import inspect
import io
import tokenize
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.mutate.policy_source import POLICY_GATE_SURFACE
from trellis.ops.capture_health import (
    NON_CAPTURE_SURFACES,
    accept_events_for,
    check_capture_health,
    is_capture_surface,
)
from trellis.stores.advisory_source import ADVISORY_WRITER_SURFACE
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

save_experience = unwrap_tool(server_mod.save_experience)
save_knowledge = unwrap_tool(server_mod.save_knowledge)
save_memory = unwrap_tool(server_mod.save_memory)

#: The helper whose call sites define the roster.
RECORDER = "_record_boundary_rejection"

#: The function :data:`RECORDER` wraps. Any call to *this* raises a capture
#: banner, wrapper or no wrapper, so the producer sweep looks for it.
EMITTER = "record_write_rejection"

SERVER_SOURCE_PATH = Path(inspect.getsourcefile(server_mod) or "")
#: ``src/`` — ``.../src/trellis/mcp/server.py`` up three.
SRC_ROOT = SERVER_SOURCE_PATH.parents[2]

#: The module whose rejections this file's tool roster covers.
MCP_WRAPPER_MODULE = "trellis/mcp/server.py"

#: Every other module allowed to emit a boundary rejection, mapped to the
#: surface label it emits under (#461).
#:
#: A producer outside :data:`MCP_WRAPPER_MODULE` raises a banner that the
#: tool roster above has never looked at, so it has to be classified here or
#: :class:`TestNoUnwatchedRejectionProducer` fails. ``config:policy_file``
#: is a *global* surface — the policy file failing to load blocks every
#: write, so it can have no accept of its own and is cleared instead by any
#: accepted write after its last rejection
#: (``capture_health._GLOBAL_SURFACE_PREFIX``).
#: ``config:advisory_file`` is global for the same reason and a different
#: one: nothing emits an *accepted* write under it at all — the advisory
#: store is not the governed pipeline — so a per-surface label there could
#: fire and never clear (#448, #461).
NON_MCP_REJECTION_PRODUCERS: dict[str, str] = {
    "trellis/mutate/policy_source.py": POLICY_GATE_SURFACE,
    "trellis_cli/worker.py": ADVISORY_WRITER_SURFACE,
}


class _Site(NamedTuple):
    lineno: int
    tool: str | None  # ``None`` when ``tool=`` is not a plain string literal


def _scan_sites(source: str) -> list[_Site]:
    """Every ``_record_boundary_rejection(...)`` call, with its ``tool=``.

    ``ast.walk`` over the whole module rather than a recursive descent over
    statements: #457's scanner skipped every ``ast.ExceptHandler`` by
    descending only ``ast.stmt``, and one of these call sites (the
    ``save_experience`` validation-error handler) lives inside an ``except``
    block. One is enough — dropping it would have taken ``save_experience``
    off the roster while leaving the guard green.
    """
    sites: list[_Site] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == RECORDER):
            continue
        tool: str | None = None
        for kw in node.keywords:
            if (
                kw.arg == "tool"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                tool = kw.value.value
        sites.append(_Site(lineno=node.lineno, tool=tool))
    return sites


def _token_site_lines(source: str) -> set[int]:
    """The same calls, found by tokenizing instead of parsing.

    The independent half of the vacuity cross-check. Deliberately *not*
    another tree walk: a cross-check that shares the subject's method
    cannot detect the method being wrong, and the bug this is aimed at
    (#457's) was a tree-descent bug. The tokenizer builds no tree, so it
    cannot inherit one.

    Preferred over a regex, which would need lookaround to skip the ``def``
    line and could still match the name inside a docstring — this module's
    subject discusses itself in prose at several points. Comments and
    strings tokenize as ``COMMENT`` / ``STRING``, never ``NAME``, so they
    are excluded structurally rather than by pattern.

    Returns line numbers, not a count: a count equality is satisfied by a
    scan that drops one site and invents another, and — more to the point —
    a count cannot say *which* site went missing.
    """
    stream = tokenize.generate_tokens(io.StringIO(source).readline)
    significant = [
        tok
        for tok in stream
        if tok.type
        not in (
            tokenize.COMMENT,
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
        )
    ]
    lines: set[int] = set()
    for index, tok in enumerate(significant):
        if tok.type != tokenize.NAME or tok.string != RECORDER:
            continue
        previous = significant[index - 1] if index else None
        following = significant[index + 1] if index + 1 < len(significant) else None
        if previous is not None and previous.string == "def":
            continue  # the definition, not a call
        if following is not None and following.string == "(":
            lines.add(tok.start[0])
    return lines


def _assert_scans_agree(ast_lines: Iterable[int], token_lines: Iterable[int]) -> None:
    """Fail naming the sites one scan saw and the other did not.

    Shared by the live cross-check and by the injected-bug proof, so the
    message an under-collection would produce is the message the proof
    inspects — not a second implementation of it.
    """
    seen_by_ast = set(ast_lines)
    seen_by_tokens = set(token_lines)
    if seen_by_ast == seen_by_tokens:
        return
    missed_by_ast = sorted(seen_by_tokens - seen_by_ast)
    missed_by_tokens = sorted(seen_by_ast - seen_by_tokens)
    message = (
        f"the two independent scans of {RECORDER} disagree. "
        f"Lines the AST scan missed: {missed_by_ast}. "
        f"Lines only the AST scan saw: {missed_by_tokens}. "
        "One of the two scanners is under-collecting; every roster "
        "assertion in this module rests on the AST one, so a missing "
        "line is an unwatched write surface."
    )
    raise AssertionError(message)


def _scan_sites_blind_to_except_handlers(source: str) -> list[_Site]:
    """:func:`_scan_sites` with #457's under-collection bug put back.

    #457's scanner descended only ``ast.stmt`` and so never entered an
    ``ast.ExceptHandler``; the effect was that calls inside ``except``
    blocks vanished from the population every one of its guards divided by.
    Reproducing the *effect* (drop exactly those sites) rather than the code
    keeps the injection small and unambiguous.

    This exists so the cross-check has been *watched to fail*. A guard
    nobody has seen fail is a guard nobody knows works.
    """
    excluded: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for statement in node.body:
            for descendant in ast.walk(statement):
                lineno = getattr(descendant, "lineno", None)
                if lineno is not None:
                    excluded.add(lineno)
    return [site for site in _scan_sites(source) if site.lineno not in excluded]


SERVER_SOURCE = SERVER_SOURCE_PATH.read_text(encoding="utf-8")
SITES = _scan_sites(SERVER_SOURCE)
TOOLS = sorted({site.tool for site in SITES if site.tool is not None})

#: How to make a **successful** call to each capture surface, so the guard
#: can prove the accept event exists by emitting it rather than by asserting
#: that someone wrote it down. A tool missing from here and from
#: ``NON_CAPTURE_SURFACES`` fails
#: :func:`test_every_rejection_site_is_classified` — which is the roster-drift
#: alarm: adding a boundary rejection to a new tool forces its author to say,
#: in executable form, how the banner it can now raise is supposed to clear.
SUCCESS_CALL: dict[str, Callable[[], object]] = {
    "save_experience": lambda: save_experience(_minimal_trace_json()),
    "save_knowledge": lambda: save_knowledge(name="Roster Guard Entity"),
    "save_memory": lambda: save_memory("a memory stored by the roster guard"),
}


def _minimal_trace_json() -> str:
    """The minimal valid trace from ``save_experience``'s own docstring."""
    doc = inspect.getdoc(save_experience)
    assert doc is not None
    marker = "Example minimal valid trace:\n"
    assert marker in doc
    return doc.split(marker, maxsplit=1)[1]


def _unclassified(source: str) -> set[str]:
    """Tools with a rejection site but no declared way to clear the banner."""
    tools = {site.tool for site in _scan_sites(source) if site.tool is not None}
    return {
        tool
        for tool in tools
        if is_capture_surface(f"mcp:{tool}") and tool not in SUCCESS_CALL
    }


def _calls_named(node: ast.AST, name: str) -> bool:
    """Is ``node`` a call to ``name``, written bare *or* through a module?

    Both spellings, because both reach the same function and only one of
    them used to be seen. ``from ... import record_write_rejection`` is what
    the two producers in the tree happen to use today, but
    ``write_health.record_write_rejection(...)`` is at least as idiomatic —
    and it escaped the sweep completely: a new module calling it that way
    left all 22 tests green while raising a banner under a label no roster
    had ever classified. Matching on the trailing name over-collects at
    worst (a same-named method on an unrelated object), and over-collecting
    costs a spurious classification while under-collecting costs an
    unwatched write surface.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == name
    return isinstance(func, ast.Attribute) and func.attr == name


def _modules_calling(name: str, root: Path) -> set[str]:
    """Every module under ``root`` containing a call to ``name``.

    Paths are ``root``-relative posix strings so the roster below reads as
    source paths rather than as machine-local absolutes.
    """
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if name not in source:  # cheap pre-filter; the AST decides
            continue
        for node in ast.walk(ast.parse(source)):
            if _calls_named(node, name):
                found.add(path.relative_to(root).as_posix())
                break
    return found


def _emitter_calls_outside_the_wrapper(source: str) -> list[int]:
    """``record_write_rejection`` calls in ``server.py`` that skip the wrapper.

    The producer sweep exempts :data:`MCP_WRAPPER_MODULE` wholesale, on the
    assumption that everything in it routes through :data:`RECORDER`. Nothing
    checked that assumption, and a direct call here raises a banner under a
    label the tool roster never scans for — ``_scan_sites`` looks only for
    :data:`RECORDER`, and the module-level sweep has already waved this file
    through. Verified by injection: adding one such call left all 18 tests
    green.
    """
    tree = ast.parse(source)
    wrapper_span: tuple[int, int] | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == RECORDER:
            wrapper_span = (node.lineno, node.end_lineno or node.lineno)
            break
    assert wrapper_span is not None, (
        f"{RECORDER} is not defined in {MCP_WRAPPER_MODULE}; the sweep's "
        "exemption for that module rests on this wrapper existing"
    )
    start, end = wrapper_span
    return [
        node.lineno
        for node in ast.walk(tree)
        if _calls_named(node, EMITTER) and not (start <= node.lineno <= end)
    ]


def _unclassified_producers(root: Path) -> set[str]:
    """Modules emitting boundary rejections that no roster accounts for."""
    return (
        _modules_calling(EMITTER, root)
        - {MCP_WRAPPER_MODULE}
        - set(NON_MCP_REJECTION_PRODUCERS)
    )


@pytest.fixture(autouse=True)
def _clear_capture_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_CAPTURE_WARN_THRESHOLD", raising=False)
    monkeypatch.delenv("TRELLIS_CAPTURE_WARN_WINDOW_HOURS", raising=False)


class TestTheScanIsNotVacuous:
    """The guard's own denominator, checked before anything rests on it."""

    def test_ast_scan_agrees_with_an_independent_tokenizer_scan(self) -> None:
        _assert_scans_agree(
            (site.lineno for site in SITES), _token_site_lines(SERVER_SOURCE)
        )

    def test_the_scan_found_something(self) -> None:
        # Parametrising over an empty list passes silently; this is what
        # stops that from reading as a green roster.
        assert len(SITES) > 0
        assert len(TOOLS) > 0

    def test_the_tokenizer_scan_is_not_itself_empty(self) -> None:
        """Both halves of a cross-check can be zero and still agree."""
        assert _token_site_lines(SERVER_SOURCE)

    def test_every_site_names_its_tool_with_a_literal(self) -> None:
        """A computed ``tool=`` would be dropped by the scan without a word.

        Failing loudly is the point: the alternative is a rejection site the
        roster silently does not cover.
        """
        dynamic = [site.lineno for site in SITES if site.tool is None]
        assert dynamic == [], (
            f"{RECORDER} called with a non-literal tool= at lines {dynamic}; "
            "the roster guard cannot classify it — pass a string literal"
        )


class TestTheRoster:
    def test_every_rejection_site_is_classified(self) -> None:
        assert _unclassified(SERVER_SOURCE) == set()

    def test_a_new_site_with_no_accept_event_fails_the_guard(self) -> None:
        """The drift alarm, proved by adding a site rather than asserting a list.

        A synthetic tree, so the proof does not depend on the real module
        ever being wrong.
        """
        synthetic = SERVER_SOURCE + (
            "\n\ndef _roster_guard_probe() -> None:\n"
            f'    {RECORDER}(tool="brand_new_capture_tool", rejections=[])\n'
        )
        synthetic_sites = _scan_sites(synthetic)
        assert len(synthetic_sites) == len(SITES) + 1
        assert "brand_new_capture_tool" in {site.tool for site in synthetic_sites}
        assert _unclassified(synthetic) == {"brand_new_capture_tool"}

    def test_a_non_capture_surface_declares_no_accept_events(self) -> None:
        for label in NON_CAPTURE_SURFACES:
            assert accept_events_for(label) == ()

    def test_non_capture_surfaces_are_all_real_rejection_sites(self) -> None:
        """Nothing is excluded from the banner that could not have raised it.

        A stale entry here is a surface silently unwatched — the failure
        direction that costs most — so the deny-list is pinned to the scan
        in both directions.
        """
        assert set(NON_CAPTURE_SURFACES).issubset({f"mcp:{tool}" for tool in TOOLS})

    def test_every_hand_written_recipe_still_matches_a_scanned_site(self) -> None:
        """The one floor a scan cannot compute — a hand count, asserted.

        Every other guard in this module divides by the scan's own output, so
        a scan that *shrinks* satisfies all of them: the two cross-checked
        scans agree (they share ``SERVER_SOURCE`` and :data:`RECORDER`), the
        non-vacuity floor is ``> 0``, and the parametrised clear-test simply
        runs fewer cases. Verified by injection — aliasing ``save_knowledge``'s
        one call site out of the scanned population dropped it from the roster
        with all 17 remaining tests green, and the collected count fell 18 → 17
        without a word.

        :data:`SUCCESS_CALL` is the hand count. Its keys were written by a
        human who knew these tools raise the banner, so the scan losing one is
        a fact about the scan, not about the tool.
        """
        missing = sorted(set(SUCCESS_CALL) - set(TOOLS))
        assert missing == [], (
            f"{missing} have a success recipe but no scanned rejection site. "
            "Either the tool genuinely stopped raising boundary rejections "
            "(delete its SUCCESS_CALL entry in the same commit) or the scan "
            "is under-collecting and these surfaces are no longer watched."
        )


class TestAcceptIsDemonstratedByExecution:
    """Run the real tool; the banner it can raise must actually clear."""

    @staticmethod
    def _seed_rejections(registry: StoreRegistry, tool: str, n: int = 3) -> None:
        for _ in range(n):
            registry.operational.event_log.emit(
                EventType.WRITE_REJECTED,
                f"mcp:{tool}",
                payload={"tool": tool, "stage": "boundary", "rejections": []},
            )

    @pytest.mark.parametrize("tool", TOOLS)
    def test_surface_clears_after_a_successful_call(
        self, tool: str, temp_registry: StoreRegistry
    ) -> None:
        label = f"mcp:{tool}"
        event_log = temp_registry.operational.event_log
        self._seed_rejections(temp_registry, tool)

        warning = check_capture_health(event_log, threshold=3)
        if not is_capture_surface(label):
            # A non-capture surface must never raise the banner at all —
            # there is nothing for a successful call to clear.
            assert warning is None or label not in warning.failing_surfaces
            return

        assert warning is not None, f"{label} did not raise the banner"
        assert label in warning.failing_surfaces

        SUCCESS_CALL[tool]()

        cleared = check_capture_health(event_log, threshold=3)
        assert cleared is None or label not in cleared.failing_surfaces, (
            f"{label} stayed dark after a successful call: none of "
            f"{[e.value for e in accept_events_for(label)]} was emitted with "
            f"requested_by={label!r}"
        )


class TestTheCrossCheckCatchesUnderCollection:
    """The vacuity guard, watched failing.

    #457's three vacuity guards all stayed green while its scanner dropped
    148 branches to 123. The reason they could is that nobody had made one
    of them fail on purpose. This does.
    """

    def test_the_injected_bug_actually_drops_a_site(self) -> None:
        """Guard the proof, before the proof guards the guard.

        If ``server.py`` ever stops raising a boundary rejection inside an
        ``except`` block, the injection below stops injecting anything and
        the next test passes for the wrong reason. Fail here instead, so the
        proof has to be re-pointed at a bug that still bites.
        """
        assert self._lines_dropped_by_the_bug(), (
            "the re-introduced under-collection bug dropped no sites, so "
            "the cross-check proof below would pass vacuously — move the "
            "injection to a shape that still drops something"
        )

    def test_the_cross_check_fails_and_names_the_missed_site(self) -> None:
        dropped = self._lines_dropped_by_the_bug()
        under_collected = {
            site.lineno for site in _scan_sites_blind_to_except_handlers(SERVER_SOURCE)
        }

        with pytest.raises(AssertionError) as excinfo:
            _assert_scans_agree(under_collected, _token_site_lines(SERVER_SOURCE))

        message = str(excinfo.value)
        for lineno in sorted(dropped):
            assert str(lineno) in message, (
                f"the cross-check failed but did not name line {lineno}; a "
                "guard that reports two integers cannot tell you which "
                "write surface stopped being watched"
            )

    @staticmethod
    def _lines_dropped_by_the_bug() -> set[int]:
        return {site.lineno for site in SITES} - {
            site.lineno for site in _scan_sites_blind_to_except_handlers(SERVER_SOURCE)
        }


class TestNoUnwatchedRejectionProducer:
    """Nothing may raise a capture banner from outside the known rosters.

    The tool roster above reads ``server.py``. A ``record_write_rejection``
    call added anywhere else would raise a banner under a label no roster
    has ever classified — and, if it never emits a matching accept, one that
    cannot clear. That is the silent false negative this module exists to
    prevent, so the sweep is over all of ``src/``, not over one file.
    """

    def test_the_sweep_finds_the_producers_we_know_about(self) -> None:
        """Non-vacuity: an empty sweep would classify everything."""
        producers = _modules_calling(EMITTER, SRC_ROOT)
        assert MCP_WRAPPER_MODULE in producers
        assert set(NON_MCP_REJECTION_PRODUCERS).issubset(producers)

    def test_no_unclassified_producer(self) -> None:
        assert _unclassified_producers(SRC_ROOT) == set()

    def test_the_exempted_module_routes_every_rejection_through_the_wrapper(
        self,
    ) -> None:
        """``server.py``'s blanket exemption, checked instead of assumed.

        The sweep waves this one module through because the tool roster
        covers it — but the tool roster scans for :data:`RECORDER`, so a
        direct :data:`EMITTER` call here is covered by *neither*. It would
        raise a banner under an unclassified label with no accept declared,
        which is the silent false negative this class exists to prevent.
        """
        stray = _emitter_calls_outside_the_wrapper(SERVER_SOURCE)
        assert stray == [], (
            f"{MCP_WRAPPER_MODULE} calls {EMITTER} directly at lines {stray}, "
            f"bypassing {RECORDER}. The tool roster scans for {RECORDER} and "
            "the producer sweep exempts this module, so such a call is in no "
            f"roster at all — route it through {RECORDER}."
        )

    def test_a_direct_emitter_call_in_the_exempted_module_is_caught(self) -> None:
        """Proved by adding one, on a synthetic tree (#443's standard)."""
        synthetic = SERVER_SOURCE + (
            "\n\ndef _roster_guard_direct_probe() -> None:\n"
            f'    {EMITTER}(None, tool="brand_new_capture_tool")\n'
        )
        assert _emitter_calls_outside_the_wrapper(synthetic) != []

    def test_the_wrapper_actually_contains_an_emitter_call(self) -> None:
        """Non-vacuity: an empty ``stray`` must mean routed, not absent."""
        inside = [
            node.lineno
            for node in ast.walk(ast.parse(SERVER_SOURCE))
            if _calls_named(node, EMITTER)
        ]
        assert inside, (
            f"{MCP_WRAPPER_MODULE} calls {EMITTER} nowhere at all; the "
            "wrapper-routing check above would pass vacuously"
        )

    def test_an_attribute_style_call_is_a_producer_too(self, tmp_path: Path) -> None:
        """``write_health.record_write_rejection(...)`` counts, not just the bare name.

        Verified by injection: before :func:`_calls_named` matched the
        attribute spelling, a new module calling the emitter through its
        module object left every test in this file green.
        """
        root = tmp_path / "src"
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "rogue.py").write_text(
            "from trellis.ops import write_health\n\n\n"
            f"def go():\n    write_health.{EMITTER}(None, tool='rogue_surface')\n",
            encoding="utf-8",
        )

        assert _unclassified_producers(root) == {"pkg/rogue.py"}

    def test_an_undeclared_producer_in_a_new_module_fails(self, tmp_path: Path) -> None:
        """Proved by adding one, not by asserting today's list (#443)."""
        root = tmp_path / "src"
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "rogue.py").write_text(
            f"def go():\n    {EMITTER}(None, tool='rogue_surface')\n",
            encoding="utf-8",
        )

        assert _unclassified_producers(root) == {"pkg/rogue.py"}

    def test_declared_non_mcp_surfaces_can_actually_clear(
        self, temp_registry: StoreRegistry
    ) -> None:
        """Executed, not declared — the same standard as the tool roster.

        Each declared label is driven to the banner and then cleared, so a
        roster entry naming a surface with no route out of the banner fails
        here rather than in production.
        """
        event_log = temp_registry.operational.event_log
        for label in NON_MCP_REJECTION_PRODUCERS.values():
            assert is_capture_surface(label), (
                f"{label} is excluded from the banner, so it does not "
                "belong in a roster of surfaces that raise one"
            )
            for _ in range(3):
                event_log.emit(
                    EventType.WRITE_REJECTED,
                    label,
                    payload={"tool": label, "stage": "boundary", "rejections": []},
                )
            warning = check_capture_health(event_log, threshold=3)
            assert warning is not None
            assert label in warning.failing_surfaces

            event_log.emit(
                EventType.MUTATION_EXECUTED,
                "mutation_executor",
                payload={"requested_by": "mcp:save_experience", "status": "success"},
            )

            cleared = check_capture_health(event_log, threshold=3)
            assert cleared is None or label not in cleared.failing_surfaces, (
                f"{label} stayed dark after an accepted write"
            )
