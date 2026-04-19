"""Translation functions: core ↔ wire.

Wire enums carry the same string values as core enums (enforced by a
parity test — see ``tests/unit/wire/test_parity.py``), so translation
reduces to ``WireEnum(core.value)`` and vice versa.  Helpers are still
named explicitly so call sites read clearly and so future non-trivial
translations have an obvious home.

No DTO-level translators exist yet: routes currently consume wire DTOs
directly and convert to core :class:`Command` / :class:`Entity` /
:class:`Edge` objects at the handler boundary.  Add DTO translators
here when a route needs the same conversion twice.
"""

from __future__ import annotations

from trellis import mutate  # re-export surface for BatchStrategy
from trellis.schemas import enums as core_enums
from trellis_wire import enums as wire_enums


def batch_strategy_to_core(wire: wire_enums.BatchStrategy) -> mutate.BatchStrategy:
    """Wire :class:`BatchStrategy` → core :class:`BatchStrategy`.

    Trivial value-copy; values are kept identical by a parity test.
    """
    return mutate.BatchStrategy(wire.value)


def batch_strategy_to_wire(core: mutate.BatchStrategy) -> wire_enums.BatchStrategy:
    """Core :class:`BatchStrategy` → wire :class:`BatchStrategy`."""
    return wire_enums.BatchStrategy(core.value)


def node_role_to_core(wire: wire_enums.NodeRole) -> core_enums.NodeRole:
    """Wire :class:`NodeRole` → core :class:`NodeRole`."""
    return core_enums.NodeRole(wire.value)


def node_role_to_wire(core: core_enums.NodeRole) -> wire_enums.NodeRole:
    """Core :class:`NodeRole` → wire :class:`NodeRole`."""
    return wire_enums.NodeRole(core.value)


__all__ = [
    "batch_strategy_to_core",
    "batch_strategy_to_wire",
    "node_role_to_core",
    "node_role_to_wire",
]
