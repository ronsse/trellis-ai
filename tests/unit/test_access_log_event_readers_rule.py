"""Every event read in ``src/`` names its event type, bar the rostered few.

The per-key access log adds event types that an ordinary read does not want:
``access.key_read`` is written once per authenticated read, so a reader that
counts or lists events without naming a type counts each read as whatever it
measures. Every aggregate reader in ``src/`` passes ``event_type`` or
``event_types``, so no new type can reach one. The readers that do not are
rostered below with why each is acceptable; a new one fails here until it is
read and added.

A read is a call to ``get_events``, ``iter_events`` or ``scan_events`` on any
receiver (or bare), or ``count`` on an event-looking receiver. It is typed only
by an ``event_type`` / ``event_types`` keyword whose value is not ``None`` or an
empty literal; ``event_type=None`` is typeless.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from tests.ast_rules import assert_hand_read_floor, iter_modules

SRC = Path(__file__).resolve().parents[2] / "src"
ANY_RECEIVER = frozenset({"get_events", "iter_events", "scan_events"})
EVENT_RECEIVER_ONLY = frozenset({"count"})
TYPE_KEYWORDS = frozenset({"event_type", "event_types"})

_BASE_GET_EVENT_WHY = (
    "the base fallback for a lookup by event id: it matches one id in the "
    "newest page and aggregates nothing (the SQLite and Postgres backends "
    "override it with an indexed lookup); the store ABC is not changed here"
)
_EVENTS_TAB_WHY = (
    "the events tab lists every type when no filter is chosen; access rows "
    "displace its tail. Documented, not changed (operations.md, Not covered)"
)

#: ``path::function::receiver.method`` -> (calls, why it is acceptable).
UNFILTERED: dict[str, tuple[int, str]] = {
    "trellis_api/routes/admin.py::stats::registry.operational.event_log.count": (
        1,
        "the stats route's total row count, labelled as a total of all events",
    ),
    "trellis_cli/admin.py::stats::get_event_log().count": (
        1,
        "the CLI's total row count, labelled as a total of all events",
    ),
    "trellis_api/routes/classify_rules.py::_find_candidate::event_log.get_events": (
        1,
        "reads one candidate's own events by entity id, then looks for its type",
    ),
    "trellis/stores/base/event_log.py::get_event::self.get_events": (
        1,
        _BASE_GET_EVENT_WHY,
    ),
}

#: Reads whose ``event_type`` is a variable that is ``None`` when the caller
#: sent no filter, so at runtime they are typeless. The rule trusts a type held
#: in a variable; these are the known Optional ones, read by hand. The UI
#: events tab is the case: with the log on, its newest rows include access
#: rows, and viewing it adds rows for the key that views it.
RUNTIME_TYPELESS: dict[str, tuple[int, str]] = {
    "trellis_api/routes/explore.py::list_events::event_log.get_events": (
        1,
        _EVENTS_TAB_WHY,
    ),
    "trellis_api/routes/explore.py::list_events::event_log.count": (
        1,
        "the same tab's total, over the same optional filter",
    ),
}


def _is_event_receiver(receiver: str) -> bool:
    lowered = receiver.lower()
    return "event" in lowered or "log" in lowered


def _names_a_type(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg not in TYPE_KEYWORDS:
            continue
        value = kw.value
        if isinstance(value, ast.Constant) and value.value is None:
            return False
        return not (
            isinstance(value, ast.List | ast.Tuple | ast.Set) and not value.elts
        )
    return False


class _Scan(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        #: ``(function, receiver.method, typed)`` per read.
        self.reads: list[tuple[str, str, bool]] = []

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _function  # noqa: N815
    visit_AsyncFunctionDef = _function  # noqa: N815

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        method = receiver = None
        if isinstance(func, ast.Attribute):
            method, receiver = func.attr, ast.unparse(func.value)
            ok = method in ANY_RECEIVER or (
                method in EVENT_RECEIVER_ONLY and _is_event_receiver(receiver)
            )
        elif isinstance(func, ast.Name):
            method, receiver = func.id, ""
            ok = method in ANY_RECEIVER
        else:
            ok = False
        if ok:
            where = self.stack[-1] if self.stack else "<module>"
            name = f"{receiver}.{method}" if receiver else str(method)
            self.reads.append((where, name, _names_a_type(node)))
        self.generic_visit(node)


def reads_in(tree: ast.AST) -> list[tuple[str, str, bool]]:
    scan = _Scan()
    scan.visit(tree)
    return scan.reads


def _reads() -> tuple[Counter[str], Counter[str]]:
    """``(typeless reads by key, typed reads by key)`` over ``src/``."""
    typeless: Counter[str] = Counter()
    typed: Counter[str] = Counter()
    for path, tree in iter_modules(SRC):
        for function, name, is_typed in reads_in(tree):
            key = f"{path.relative_to(SRC).as_posix()}::{function}::{name}"
            (typed if is_typed else typeless)[key] += 1
    return typeless, typed


def test_every_typeless_event_read_is_rostered() -> None:
    typeless, _ = _reads()
    assert dict(typeless) == {k: n for k, (n, _) in UNFILTERED.items()}, (
        "an event read with no event_type was added or removed; read it, then "
        f"edit UNFILTERED. found: {dict(typeless)}"
    )


def test_the_runtime_typeless_sites_are_still_there() -> None:
    _, typed = _reads()
    assert {k: typed[k] for k in RUNTIME_TYPELESS} == {
        k: n for k, (n, _) in RUNTIME_TYPELESS.items()
    }, "a rostered Optional-typed read moved or went; re-read it"


def test_the_scan_still_sees_the_typed_readers() -> None:
    _, typed = _reads()
    assert_hand_read_floor(
        sum(typed.values()),
        40,
        subject="typed event read",
        hint="the receiver test or the method list may have narrowed",
    )


class TestTheScanCatchesTheEvasions:
    """Each is a way a typeless read slipped past the first version of the rule."""

    @staticmethod
    def _typeless(source: str) -> list[str]:
        return [n for _, n, typed in reads_in(ast.parse(source)) if not typed]

    def test_a_typed_read_is_not_flagged(self) -> None:
        assert self._typeless("def f(log):\n    log.get_events(event_type=T)") == []
        assert self._typeless("def f(l):\n    scan_events(l, event_types=[T])") == []

    def test_event_type_none_is_typeless(self) -> None:
        assert self._typeless("def f(log):\n    log.get_events(event_type=None)") == [
            "log.get_events"
        ]
        assert self._typeless("def f(l):\n    scan_events(l, event_types=[])") == [
            "scan_events"
        ]

    def test_a_receiver_without_event_or_log_in_its_name_is_scanned(self) -> None:
        assert self._typeless("def f(store):\n    store.get_events(limit=5)") == [
            "store.get_events"
        ]

    def test_a_bare_scan_events_call_is_scanned(self) -> None:
        assert self._typeless("def f(event_log):\n    scan_events(event_log)") == [
            "scan_events"
        ]

    def test_a_splat_does_not_name_a_type(self) -> None:
        assert self._typeless("def f(log, kw):\n    log.get_events(**kw)") == [
            "log.get_events"
        ]
