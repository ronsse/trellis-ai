"""Write provenance — which build, under which write semantics, wrote this.

Every event emitted through :meth:`trellis.stores.base.event_log.EventLog.emit`
carries a stamp under ``metadata["write_provenance"]``:

.. code-block:: json

    {"version": "0.9.1.dev156+gd7c3e7ace",
     "version_source": "dist-metadata",
     "commit": "d7c3e7ace",
     "dirty": false,
     "flags": {"classify_on_ingest": true, "...": "..."},
     "flags_digest": "1f0a2b3c"}

**Why metadata and not the payload.**  Payloads are per-event-type and
several are typed models with ``extra="forbid"``; adding a key to all of
them would mean touching every payload model and would reject historical
rows on read-back.  :attr:`~trellis.stores.base.event_log.Event.metadata`
is already a free-form ``dict[str, Any]`` on every backend, so the stamp
is additive by construction: old rows carry no ``write_provenance`` key
and parse exactly as they always did.  Nothing in the system *requires*
the stamp — an emitter that cannot supply one (a direct
``Event(...) + append()``, a replayed historical row) is still valid.

**Cost.**  The stamp is resolved once per process and reused by reference
count, not rebuilt per event: the version cannot change under a live
interpreter, and neither can the write semantics a process was launched
with.  Call :func:`reset_write_provenance_cache` when a test mutates the
flag environment and then asserts on a stamp.

**Where an operator reads it.**  ``trellis admin write-config`` reports the
stamp for the process the operator is standing in; ``GET /api/version``
reports it for a running API container.  For the stdio MCP server — spawned
per session, gone before anyone can ask it anything — the per-event stamp
*is* the surface: the rows it wrote are the only durable record that the
process ever existed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from trellis.core.version import resolve_code_version
from trellis.core.write_config import WriteBehaviourConfig

#: Metadata key the stamp is written under.  Consumers filtering events by
#: build should key off this constant, not the literal.
WRITE_PROVENANCE_KEY = "write_provenance"

#: Process-wide memo.  ``None`` until first use; cleared by
#: :func:`reset_write_provenance_cache`.
_CACHED_STAMP: dict[str, Any] | None = None


def _flags_digest(flags: dict[str, Any]) -> str:
    """Short stable digest of a flag set, for cheap ``GROUP BY``.

    Analysts bucketing "which write semantics produced these rows" want
    one comparable token, not an eight-key JSON object; the full map stays
    alongside it for when the bucket needs explaining.
    """
    canonical = json.dumps(flags, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


def build_write_provenance(
    config: WriteBehaviourConfig | None = None,
) -> dict[str, Any]:
    """Build a stamp from ``config`` (default: the live environment).

    Uncached — :func:`get_write_provenance` is the hot-path entry point.
    Exposed so the CLI / API surfaces can report a stamp for an explicitly
    supplied configuration.
    """
    flags = (config or WriteBehaviourConfig.from_env()).as_dict()
    stamp = resolve_code_version().as_dict()
    stamp["flags"] = flags
    stamp["flags_digest"] = _flags_digest(flags)
    return stamp


def get_write_provenance() -> dict[str, Any]:
    """Return the process's write-provenance stamp, resolved once.

    The returned mapping is shared and must be treated as read-only;
    :func:`stamp_metadata` copies it before handing it to an event.
    """
    global _CACHED_STAMP  # noqa: PLW0603 — process-wide memo, see module docs
    if _CACHED_STAMP is None:
        _CACHED_STAMP = build_write_provenance()
    return _CACHED_STAMP


def reset_write_provenance_cache() -> None:
    """Drop the memoized stamp so the next call re-reads the environment.

    Test-facing.  Production processes never change their write semantics
    mid-flight, so nothing on a live path calls this.
    """
    global _CACHED_STAMP  # noqa: PLW0603 — process-wide memo, see module docs
    _CACHED_STAMP = None


def stamp_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return ``metadata`` with the write-provenance stamp merged in.

    A caller that supplied its own ``write_provenance`` wins — replay and
    backfill tools re-emit rows on behalf of a *different* build, and
    overwriting their attribution with the replaying process's would be
    exactly the drift this stamp exists to prevent.
    """
    stamped = dict(metadata) if metadata else {}
    stamped.setdefault(WRITE_PROVENANCE_KEY, dict(get_write_provenance()))
    return stamped


__all__ = [
    "WRITE_PROVENANCE_KEY",
    "build_write_provenance",
    "get_write_provenance",
    "reset_write_provenance_cache",
    "stamp_metadata",
]
