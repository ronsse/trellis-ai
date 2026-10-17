"""The one place caller-supplied ``Command.metadata`` is checked for a
reserved key before it reaches ``Command(...)``.

``Command.metadata`` is the audit-trail channel: several keys on it are
read as authoritative by the executor, a handler, or the write-provenance
stamp rather than treated as opaque decoration --

* ``requested_by_claim`` -- the audit trail's record of what the caller
  *claimed* ``requested_by`` was, read by
  :func:`trellis.mutate.executor.MutationExecutor` (``executor.py``, the
  warning/rejection path) as the caller's own claim, distinct from the
  server-derived ``requested_by`` itself.
* ``key_id`` / ``key_name`` -- the acting API key's identity, read as
  authoritative by multiple classify-rule and schedule handlers in
  ``trellis.mutate.handlers`` and ``trellis.mutate.schedule_handlers``
  (e.g. ``SCHEDULE_UPDATED``, ``JOB_RUN_REQUESTED``) to attribute *who*
  changed a schedule or classify rule.
* ``write_provenance`` -- the per-process build/commit stamp
  (:data:`trellis.core.write_provenance.WRITE_PROVENANCE_KEY`).
  :func:`trellis.core.write_provenance.stamp_metadata` keeps a value
  already present on *event* metadata rather than overwriting it. No
  emit passes ``Command.metadata`` through today, so nothing reads this
  key off a command yet; it stays reserved so that a future pass-through
  cannot let a caller choose which build an event claims.
* ``handler_warnings`` -- read by
  :func:`trellis.mutate.executor._handler_warnings` and surfaced verbatim
  on ``CommandResult.warnings``; a caller-planted list would appear in the
  API response as a warning the handler never raised.

A caller that sets one of these directly -- even an admin caller -- can
forge who the audit trail blames, backdate which build wrote an event, or
inject a fabricated handler warning. :func:`reject_reserved_metadata` is
the shared guard every boundary that copies caller-supplied metadata onto
a ``Command`` must call *before* merging in its own legitimate stamps
(``requested_by_claim``, ``key_id``/``key_name`` from
``_audit_identity``-style helpers): it refuses with a named key rather
than silently stripping, because a silent strip would make a forged
request look identical to an honest one that simply omitted the key.

``tests/unit/api/test_reserved_metadata_rule.py`` statically enforces that
every ``Command``/``CommandBatch``/``build_evidence_ingest_command*``
construction built from caller metadata in ``src/trellis_api/routes/`` or
``src/trellis/mcp/`` passes its metadata through this guard first.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Keys the executor, a handler, or the write-provenance stamp read off
#: ``Command.metadata`` as authoritative rather than as opaque caller
#: decoration. See the module docstring for what reads each one and why
#: a caller-planted value is a forgery, not harmless noise.
RESERVED_COMMAND_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "requested_by_claim",
        "key_id",
        "key_name",
        "write_provenance",
        "handler_warnings",
    }
)


class ReservedMetadataKeyError(ValueError):
    """Caller-supplied ``Command.metadata`` named a reserved key.

    A plain :class:`ValueError`, not a :class:`trellis.errors.TrellisError`
    -- the boundaries that call :func:`reject_reserved_metadata` catch
    this locally and answer their own transport-shaped error (a 400 for
    REST ``/commands/batch``), the same pattern
    ``trellis_api.routes.mutations._parse_operation`` already uses for a
    bad operation string, rather than falling through to the generic
    ``TrellisError`` handler's 500.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(
            f"metadata key {key!r} is reserved for server-side audit "
            "stamping and may not be set by the caller"
        )


def reject_reserved_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """A plain copy of *metadata*, or a raised
    :class:`ReservedMetadataKeyError` naming the offending key.

    Call this on the metadata a caller actually sent -- a request body's
    ``item.metadata``, an MCP tool argument -- *before* a boundary merges
    its own server-derived stamp (``requested_by_claim``, ``key_id`` /
    ``key_name``) on top. Applying it to the already-merged dict would
    reject the server's own legitimate stamp along with a forged one;
    applying it first means the server-side merge always runs clean.

    Returns a new ``dict`` rather than *metadata* itself, so a caller
    cannot mutate the checked copy after the fact and so every call site
    gets the same "own this dict from here" guarantee ``dict(item.metadata)``
    gave before this guard existed.
    """
    for key in metadata:
        if key in RESERVED_COMMAND_METADATA_KEYS:
            raise ReservedMetadataKeyError(key)
    return dict(metadata)


__all__ = [
    "RESERVED_COMMAND_METADATA_KEYS",
    "ReservedMetadataKeyError",
    "reject_reserved_metadata",
]
