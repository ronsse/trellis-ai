"""Roster guard: every boundary-rejection site must have a clearable accept.

#461 shipped two structurally-unclearable capture-health labels. The
mechanism was not subtle — a warning clears on ``MUTATION_EXECUTED`` with
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
ratio's denominator came from the population the bug had truncated. So the
AST scan is cross-checked against a *textually* independent count of the
same calls, a dynamic (non-literal) ``tool=`` is a hard failure rather than
a silent skip, and the derived list is asserted non-empty before anything
is parametrised over it.

What this does **not** prove: that a declared accept event is emitted on
every successful path of its tool, only on the one the recipe exercises. A
``save_memory`` whose every call deduplicates emits nothing, for instance.
That residue is named in the issue and is bounded — a surface with no
successful calls at all has no rejections to clear either.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.ops.capture_health import (
    NON_CAPTURE_SURFACES,
    accept_events_for,
    check_capture_health,
    is_capture_surface,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

save_experience = unwrap_tool(server_mod.save_experience)
save_knowledge = unwrap_tool(server_mod.save_knowledge)
save_memory = unwrap_tool(server_mod.save_memory)

#: The helper whose call sites define the roster.
RECORDER = "_record_boundary_rejection"

SERVER_SOURCE_PATH = Path(inspect.getsourcefile(server_mod) or "")


class _Site(NamedTuple):
    lineno: int
    tool: str | None  # ``None`` when ``tool=`` is not a plain string literal


def _scan_sites(source: str) -> list[_Site]:
    """Every ``_record_boundary_rejection(...)`` call, with its ``tool=``.

    ``ast.walk`` over the whole module rather than a recursive descent over
    statements: #457's scanner skipped every ``ast.ExceptHandler`` by
    descending only ``ast.stmt``, and three of these call sites live inside
    ``except`` blocks.
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


def _textual_call_count(source: str) -> int:
    """Independent count of the same calls, by text rather than by AST.

    Deliberately not another tree walk: a cross-check that shares the
    subject's method cannot detect the method being wrong. Excludes the
    ``def`` line and any bare mention in prose.
    """
    return len(re.findall(rf"(?<!def ){RECORDER}\s*\(", source))


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


@pytest.fixture(autouse=True)
def _clear_capture_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_CAPTURE_WARN_THRESHOLD", raising=False)
    monkeypatch.delenv("TRELLIS_CAPTURE_WARN_WINDOW_HOURS", raising=False)


class TestTheScanIsNotVacuous:
    """The guard's own denominator, checked before anything rests on it."""

    def test_ast_scan_agrees_with_an_independent_textual_count(self) -> None:
        assert len(SITES) == _textual_call_count(SERVER_SOURCE)

    def test_the_scan_found_something(self) -> None:
        # Parametrising over an empty list passes silently; this is what
        # stops that from reading as a green roster.
        assert len(SITES) > 0
        assert len(TOOLS) > 0

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
