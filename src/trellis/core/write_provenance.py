"""Write provenance — which build, under which write environment, wrote this.

Every event emitted through :meth:`trellis.stores.base.event_log.EventLog.emit`
carries a stamp under ``metadata["write_provenance"]``:

.. code-block:: json

    {"version": "0.9.1.dev156+gd7c3e7ace",
     "version_source": "dist-metadata",
     "commit": "d7c3e7ace",
     "dirty": false,
     "env_flags": {"classify_on_ingest": true, "...": "..."},
     "env_flags_digest": "1f0a2b3c"}

**Two more keys appear, and only when something is wrong.**  ``commit``
is frozen at *install* time, so an editable install off a working tree
that has since moved on attributes every write to code it is no longer
running.  When :func:`~trellis.core.version.resolve_stamp_staleness`
catches that, the stamp gains ``"stamp_stale": true`` and
``"source_tree_commit": "<40-char sha>"`` — the tree's live ``HEAD``.
``commit`` is never overwritten: the row still records what the metadata
said, which is the thing an analyst is bucketing by.  A healthy editable
install, and every container image, emit a stamp byte-identical to the
one they emitted before the probe existed, so a deployment with nothing
to report pays nothing and "no staleness keys" stays readable as "fine".
``trellis admin write-config`` is where "checked and fine" is told apart
from "never checked".

**What ``env_flags`` is, precisely.**  The write-behaviour environment the
*process* was launched with — not a per-write record of what actually ran.
The distinction bites in one place today: ``memory_extraction`` is ANDed
with a caller-supplied ``opt_in`` (``trellis ingest corpus`` passes
``--extract``), so ``memory_extraction: true`` means "the environment
allowed it", and a run without ``--extract`` stamps ``true`` while
extracting nothing.  The field is named for the environment rather than
for the behaviour so an analyst bucketing rows by ``env_flags_digest``
reads it as the coarse grouping it is.

**Why metadata and not the payload.**  Payloads are per-event-type and
several are typed models with ``extra="forbid"``; adding a key to all of
them would mean touching every payload model and would reject historical
rows on read-back.  :attr:`~trellis.stores.base.event_log.Event.metadata`
is already a free-form ``dict[str, Any]`` on every backend, so the stamp
is additive by construction: old rows carry no ``write_provenance`` key
and parse exactly as they always did.  Nothing in the system *requires*
the stamp — an emitter that cannot supply one (a direct
``Event(...) + append()``, a replayed historical row) is still valid.

**Cost.**  The stamp is resolved once per process: the version cannot
change under a live interpreter, and neither can the environment a process
was launched with.  Each event gets its own copy (~400 bytes, 0.6 µs) so a
consumer mutating one event's metadata cannot poison the memo or any other
in-flight event.  Call ``get_write_provenance.cache_clear()`` when a test
mutates the flag environment and then asserts on a stamp — the autouse
fixture in ``tests/conftest.py`` already does, along with
``resolve_stamp_staleness.cache_clear()`` for the git-backed half.

**Where an operator reads it.**  ``trellis admin write-config`` reports the
stamp for the process the operator is standing in; ``GET /api/version``
reports it for a running API container.  For the stdio MCP server — spawned
per session, gone before anyone can ask it anything — the per-event stamp
*is* the surface: the rows it wrote are the only durable record that the
process ever existed.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import structlog

from trellis.core.version import resolve_code_version, resolve_stamp_staleness
from trellis.core.write_config import WriteBehaviourConfig

logger = structlog.get_logger(__name__)

#: Metadata key the stamp is written under.  Consumers filtering events by
#: build should key off this constant, not the literal.
WRITE_PROVENANCE_KEY = "write_provenance"


def _guess_stores_dir() -> Path:
    """Guess ``stores_dir`` from the environment alone, never raising.

    Mirrors the env-var half of
    :meth:`trellis.stores.registry.StoreRegistry.from_config_dir`
    (``TRELLIS_CONFIG_DIR`` → ``config_dir``, default ``~/.trellis``;
    ``TRELLIS_DATA_DIR`` → ``data_dir``, default ``<config_dir>/data``;
    ``stores_dir = data_dir / "stores"``) but deliberately skips that
    method's ``config.yaml`` ``data_dir`` override: this is a zero-arg hot
    path called with no registry in hand (:func:`get_write_provenance`
    takes no arguments), so there is no ``config_dir`` to read a YAML file
    from without adding a disk read — and a filesystem miss there — to
    every process's first write. A deployment that overrides ``data_dir``
    only via ``config.yaml`` (not the env var) is not reached by the
    settings-aware stamp; its ``env_flags`` still falls back correctly to
    :meth:`WriteBehaviourConfig.from_env`, the same answer it got before
    this function existed.
    """
    config_default = str(Path.home() / ".trellis")
    config_dir = Path(os.environ.get("TRELLIS_CONFIG_DIR", config_default))
    data_dir = Path(os.environ.get("TRELLIS_DATA_DIR", str(config_dir / "data")))
    return data_dir / "stores"


def _resolve_write_behaviour_for_stamp() -> WriteBehaviourConfig:
    """The merged (env > settings > default) config for the process stamp.

    ``env_flags`` records effective permission, so once a settings
    override can make a knob live, the stamp must reflect the merged
    value rather than freezing it at the environment alone. Advisory,
    like :func:`trellis.core.version.resolve_stamp_staleness`: this rides
    every write via :func:`get_write_provenance`, so a settings-store read
    that raises must degrade to the environment-only answer, never fail
    the write.
    """
    try:
        return _resolve_via_guessed_stores_dir()
    except Exception:  # advisory probe; never fail a write
        logger.warning("write_provenance_settings_resolve_failed", exc_info=True)
        return WriteBehaviourConfig.from_env()


def _resolve_via_guessed_stores_dir() -> WriteBehaviourConfig:
    from trellis.stores.settings_store import load_settings_values  # noqa: PLC0415

    settings = load_settings_values(_guess_stores_dir())
    return WriteBehaviourConfig.from_env_and_settings(settings=settings)


def _env_flags_digest(flags: dict[str, Any]) -> str:
    """Short stable digest of a flag set, for cheap ``GROUP BY``.

    Analysts bucketing "which write environment produced these rows" want
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

    The default (``config is None``) resolves env > settings > default
    via :func:`_resolve_write_behaviour_for_stamp`, guessing ``stores_dir``
    from the environment alone (:func:`_guess_stores_dir`) since this
    function takes no registry. A caller that already has one should pass
    ``WriteBehaviourConfig`` from
    :func:`trellis.core.write_config.resolve_write_behaviour` explicitly
    instead, to also honour a ``config.yaml`` ``data_dir`` override.
    """
    flags = (config or _resolve_write_behaviour_for_stamp()).as_dict()
    stamp = resolve_code_version().as_dict()
    stamp["env_flags"] = flags
    stamp["env_flags_digest"] = _env_flags_digest(flags)
    stamp.update(resolve_stamp_staleness().as_stamp_fields())
    return stamp


def _copy_stamp(stamp: dict[str, Any]) -> dict[str, Any]:
    """Copy a stamp deeply enough that no dict is shared with the memo.

    A shallow ``dict()`` would hand every stamped event the memo's own
    ``env_flags`` object, so one consumer mutating an event's metadata
    would poison the memo and every other in-flight event.
    :func:`copy.deepcopy` also fixes that but costs 6 µs against 0.6 µs
    here, on a path that runs per emitted event — and the stamp's shape is
    owned by this module: primitives plus one dict of primitives, pinned
    by ``test_stamp_nests_at_most_one_level``.
    """
    return {k: dict(v) if isinstance(v, dict) else v for k, v in stamp.items()}


@functools.lru_cache(maxsize=1)
def get_write_provenance() -> dict[str, Any]:
    """Return the process's write-provenance stamp, resolved once.

    The returned mapping is shared and must be treated as read-only;
    :func:`stamp_metadata` copies it before handing it to an event.
    ``get_write_provenance.cache_clear()`` re-reads the environment —
    test-facing only, since a production process never changes its write
    semantics mid-flight.
    """
    return build_write_provenance()


def stamp_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return ``metadata`` with the write-provenance stamp merged in.

    A caller that supplied its own ``write_provenance`` wins — replay and
    backfill tools re-emit rows on behalf of a *different* build, and
    overwriting their attribution with the replaying process's would be
    exactly the drift this stamp exists to prevent.  That path also skips
    building a copy it would only discard.
    """
    stamped = dict(metadata) if metadata else {}
    if WRITE_PROVENANCE_KEY not in stamped:
        stamped[WRITE_PROVENANCE_KEY] = _copy_stamp(get_write_provenance())
    return stamped


__all__ = [
    "WRITE_PROVENANCE_KEY",
    "build_write_provenance",
    "get_write_provenance",
    "stamp_metadata",
]
