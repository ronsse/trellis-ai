"""Every event read in ``src/`` names its event type, bar three counted sites.

The per-key access log adds event types that every ordinary read does not
want: ``access.key_read`` is written once per authenticated read, so a reader
that counts or lists events without naming a type would count each read as
whatever it measures. Every aggregate reader in ``src/`` passes ``event_type``
or ``event_types``, so no new type can reach one. The readers that do not are
listed below with why each is safe; a new one fails here until it is read and
added.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from tests.ast_rules import assert_hand_read_floor, iter_modules

SRC = Path(__file__).resolve().parents[2] / "src"
READ_METHODS = frozenset({"get_events", "count", "iter_events", "scan_events"})

#: ``path::receiver.method`` of each typeless read, and why it is safe.
UNFILTERED: dict[str, str] = {
    "trellis_api/routes/admin.py::registry.operational.event_log.count": (
        "the stats route's total row count, labelled as a total of all events"
    ),
    "trellis_cli/admin.py::get_event_log().count": (
        "the CLI's total row count, labelled as a total of all events"
    ),
    "trellis_api/routes/classify_rules.py::event_log.get_events": (
        "reads one candidate's own events by entity id, then looks for its type"
    ),
}


def _is_event_receiver(receiver: str) -> bool:
    lowered = receiver.lower()
    return "event" in lowered or "log" in lowered


def _reads() -> tuple[Counter[str], int]:
    """``(typeless reads by key, count of typed reads)`` over ``src/``."""
    typeless: Counter[str] = Counter()
    typed = 0
    for path, tree in iter_modules(SRC):
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in READ_METHODS
                and _is_event_receiver(ast.unparse(node.func.value))
            ):
                continue
            names = {kw.arg for kw in node.keywords}
            if {"event_type", "event_types"} & names:
                typed += 1
            else:
                key = (
                    f"{path.relative_to(SRC).as_posix()}::"
                    f"{ast.unparse(node.func.value)}.{node.func.attr}"
                )
                typeless[key] += 1
    return typeless, typed


def test_every_typeless_event_read_is_listed() -> None:
    typeless, _ = _reads()
    assert set(typeless) == set(UNFILTERED), (
        "an event read with no event_type was added or removed; read it, then "
        f"edit UNFILTERED. found: {sorted(typeless)}"
    )
    assert all(n == 1 for n in typeless.values())


def test_the_scan_still_sees_the_typed_readers() -> None:
    _, typed = _reads()
    assert_hand_read_floor(
        typed,
        40,
        subject="typed event read",
        hint="the receiver test or the method list may have narrowed",
    )
