"""Wire-level enums.

Duplicates of the corresponding core enums, with **identical string
values**.  A parity test in ``tests/unit/wire/test_parity.py``
verifies the two sides stay in sync — if core adds or renames a value
without updating the wire side (or vice versa), the test fails.

Keeping values identical lets :mod:`trellis.wire.translate` do a
trivial ``WireEnum(core_enum.value)`` round-trip instead of a
hand-maintained lookup table.
"""

from __future__ import annotations

from enum import StrEnum


class BatchStrategy(StrEnum):
    """How a mutation batch handles per-item failures.

    Wire-level duplicate of :class:`trellis.mutate.commands.BatchStrategy`.
    """

    SEQUENTIAL = "sequential"
    STOP_ON_ERROR = "stop_on_error"
    CONTINUE_ON_ERROR = "continue_on_error"


class NodeRole(StrEnum):
    """Graph node role.

    Wire-level duplicate of :class:`trellis.schemas.enums.NodeRole`.
    """

    STRUCTURAL = "structural"
    SEMANTIC = "semantic"
    CURATED = "curated"


__all__ = ["BatchStrategy", "NodeRole"]
