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
* :class:`~trellis.errors.ConfigError` -> :data:`EXIT_STORE` (see
  :func:`exit_code_for` for why it is not ``EXIT_VALIDATION``)
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
    "exit_code_for",
]


def exit_code_for(exc: BaseException) -> int:
    """Map a typed Trellis exception to the exit code an operator scripts on.

    The map above, made executable. It lived only as prose in this module's
    docstring, so the boundary that renders an uncaught
    :class:`~trellis.errors.TrellisError` had nothing to call and every such
    failure left as a traceback with exit ``1`` — "unexpected; file a bug"
    for a damaged config file the operator can fix in one edit (#459).

    Ordered most-specific first, because the hierarchy nests:
    ``NotFoundError`` is a ``StoreError``, ``PolicyViolationError`` and
    ``IdempotencyError`` are ``MutationError``\\ s, and
    ``BackendNotInstalledError`` is a ``ConfigError``.

    ``ConfigError`` -> :data:`EXIT_STORE` is the one addition to the
    documented map, and it is a deliberate choice between two defensible
    codes. :data:`EXIT_VALIDATION` ("fix your input") is wrong: the
    command's *own* input was fine, and a wrapper that retries with
    corrected arguments on ``2`` would loop forever against a malformed
    ``policies.json``. :data:`EXIT_STORE` says what is true — the
    deployment's state is wrong and a human has to change it — and it is
    what ``trellis policy list`` already exits when it meets *the same
    file* damaged the same way (``policy._exit_if_degraded``,
    ``policy._exit_on_refused_write``). One root cause, one code.

    Anything that is not a :class:`~trellis.errors.TrellisError` keeps
    :data:`EXIT_INTERNAL`: an untyped exception escaping to the boundary
    really is "unexpected; file a bug", and dressing it up as an
    actionable code would be the lie this function exists to remove.
    """
    from trellis.errors import (  # noqa: PLC0415 - avoid an import cycle at module load
        ConfigError,
        IdempotencyError,
        PolicyViolationError,
        StoreError,
        ValidationError,
    )

    if isinstance(exc, ValidationError):
        return EXIT_VALIDATION
    if isinstance(exc, PolicyViolationError):
        return EXIT_POLICY
    if isinstance(exc, IdempotencyError):
        return EXIT_IDEMPOTENCY
    if isinstance(exc, (StoreError, ConfigError)):
        return EXIT_STORE
    return EXIT_INTERNAL
