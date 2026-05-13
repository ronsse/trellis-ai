"""Canonical CLI exit codes.

See `docs/design/adr-cli-exit-codes.md` for the rationale. The map is
intentionally small: five codes cover every actionable branch, anything
beyond falls back to ``EXIT_INTERNAL = 1``.

Operators script around these — for example::

    trellis ingest trace ./bad.json
    case $? in
        0) echo "ok" ;;
        2) echo "fix your input" ;;
        3) echo "policy denied — get approval" ;;
        4) echo "already committed — treat as success" ;;
        5) echo "backend down — page on-call" ;;
        *) echo "unexpected; file a bug" ;;
    esac

Mapping to the typed exception hierarchy in :mod:`trellis.errors`:

* :class:`~trellis.errors.ValidationError` -> :data:`EXIT_VALIDATION`
* :class:`~trellis.errors.PolicyViolationError` -> :data:`EXIT_POLICY`
* :class:`~trellis.errors.IdempotencyError` -> :data:`EXIT_IDEMPOTENCY`
* :class:`~trellis.errors.StoreError` -> :data:`EXIT_STORE`
* anything else -> :data:`EXIT_INTERNAL`
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_VALIDATION = 2
EXIT_POLICY = 3
EXIT_IDEMPOTENCY = 4
EXIT_STORE = 5

__all__ = [
    "EXIT_IDEMPOTENCY",
    "EXIT_INTERNAL",
    "EXIT_OK",
    "EXIT_POLICY",
    "EXIT_STORE",
    "EXIT_VALIDATION",
]
