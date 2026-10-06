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

from collections.abc import Sequence
from typing import NamedTuple

from trellis.core.error_sanitize import sanitize_error_message
from trellis.errors import (
    ConfigError,
    IdempotencyError,
    PolicyViolationError,
    StoreError,
    ValidationError,
)
from trellis.mutate.commands import CommandResult, CommandStatus

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
    "BatchOutcome",
    "batch_outcome",
    "exit_code_for",
    "refusal_exit_code",
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
    if isinstance(exc, ValidationError):
        return EXIT_VALIDATION
    if isinstance(exc, PolicyViolationError):
        return EXIT_POLICY
    if isinstance(exc, IdempotencyError):
        return EXIT_IDEMPOTENCY
    if isinstance(exc, (StoreError, ConfigError)):
        return EXIT_STORE
    return EXIT_INTERNAL


def refusal_exit_code(result: CommandResult) -> int:
    """Return the exit code for a refused or failed write, per the exit-code ADR.

    A policy refusal exits ``EXIT_POLICY`` ("get approval, don't retry"),
    any other refusal ``EXIT_VALIDATION`` ("fix your input"), and a failure
    ``EXIT_STORE``. The executor names a refusal's cause in
    ``metadata["rejection_reason"]``, and a result without one is not a
    policy refusal. It returns rather than raises, so each caller's
    ``raise`` stays below its format branch
    (``tests/unit/test_format_exit_parity_rule.py``).
    """
    if result.status != CommandStatus.REJECTED:
        return EXIT_STORE
    if result.metadata.get("rejection_reason") == "policy_violation":
        return EXIT_POLICY
    return EXIT_VALIDATION


#: The answers under which a command wrote nothing. ``DUPLICATE`` is not one:
#: it answers the replay of a write that already landed.
_REFUSED = (CommandStatus.REJECTED, CommandStatus.FAILED)


class BatchOutcome(NamedTuple):
    """What a governed batch's results mean for the command that submitted it."""

    #: The first result when every command was refused or failed, else
    #: ``None``. The command exits by it, through :func:`refusal_exit_code`.
    refusal: CommandResult | None
    #: The first refused or failed result, whatever the others answered.
    first_failure: CommandResult | None
    #: The payload's ``status``, with a sanitized ``message`` when it names
    #: a failure.
    status: dict[str, str]


def batch_outcome(
    results: Sequence[CommandResult], *, done: str, name_partial: bool = False
) -> BatchOutcome:
    """Read a batch's results by the #687 rule ``ingest`` and ``extract`` share.

    A batch is refused only when it is non-empty and every command was
    refused or failed. It exits by its first result, and its payload reads
    ``"status": "error"`` with that result's message, so the payload and the
    exit code come from one flag. Any other batch reads *done* and exits
    ``0``: a duplicate replays a write that landed, and an empty batch refused
    nothing. *name_partial* names a partial batch's first failure in its
    payload too, as ``trellis extract`` names it in text.

    The payload's message passes through :func:`sanitize_error_message`: the
    payload is an artifact, and a store's error can quote its DSN. The
    results keep the raw text for the human form. It returns rather than
    raises, so each caller's ``raise`` stays below its format branch.
    """
    first = next((r for r in results if r.status in _REFUSED), None)
    refused = first is not None and all(r.status in _REFUSED for r in results)
    status = {"status": "error" if refused else done}
    if first is not None and (refused or name_partial):
        status["message"] = sanitize_error_message(first.message)
    return BatchOutcome(first if refused else None, first, status)
