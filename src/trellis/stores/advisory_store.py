"""Advisory store — JSON file-based persistence for advisories.

Failure posture — read leniently, refuse to write
-------------------------------------------------
This store whole-file-rewrites on every write: :meth:`AdvisoryStore._save`
serialises ``self._advisories.values()`` over the path it loaded from. That
makes a *lenient read* and a *lenient write* two very different promises,
and until #393 the module made the first while accidentally making the
second.

The read stays lenient, and deliberately so. :mod:`trellis.stores.advisory_source`
argues it and is right: advisories are hint-only guidance attached to a
pack, and a corrupt advisory file must not take retrieval down with it —
unlike :mod:`trellis.mutate.policy_source`, which raises, because degrading
an *access-control* file to "no policies" silently disables a guarantee the
caller believes it has. The asymmetry is real and it survives here.

What does not survive is applying that leniency to the write. Degrading an
unreadable file to an empty set and then rewriting the path deletes it, and
the deletion is the *quiet* half of the failure:

1. ``_load`` fails, logs, leaves ``self._advisories == {}``.
2. The nightly ``AdvisoryGenerator.generate`` calls ``put_many(...)``.
3. ``_save`` writes a file containing only the new rows.

Since stable advisory ids (#394) that is worse than data loss. The
generator's ``_carry_forward_status`` reads each finding's prior row to
decide what survives a replacing write; against an empty store every
``get`` returns ``None``, so every regenerated row is written fresh —
``status=ACTIVE``, ``suppressed_at=None``. Suppression is the only
mechanism that takes a bad advisory out of circulation, and a corrupt file
would silently reverse every suppression the fitness loop had made, while
the run reported an ordinary ``advisories_generated: N``. Losing data shows
up on the next read; reversing a curation decision looks like normal
operation.

So: **the read degrades and the write refuses.** A store that could not
read its file in full serves what it did read (retrieval keeps working) and
raises :class:`~trellis.errors.DegradedStoreWriteError` from every write
path. The corrupt bytes stay on disk, where an operator can look at them.

Per-row, not per-file
---------------------
Validation is per row. One unparseable entry — a hand-edit, a renamed
field — costs that entry rather than the file, so a pack still carries the
advisories that *are* readable. That leniency is only safe because the
write refuses: a partial load followed by a permitted write would rewrite
the file without the skipped rows, which is the same data loss at a
narrower granularity. The two halves are a pair; neither is safe alone.

Recovery is the operator's, not this module's
---------------------------------------------
#393 suggested moving the unreadable file aside automatically before
continuing empty. That is the right *recovery* and the wrong *reflex*, for
two reasons. It is a filesystem mutation performed at read time, and the
readers here are pack assembly on the MCP and REST surfaces — a
``get_context`` call would rename an operator's file. And it converts a
loud corrupt state into a quiet greenfield one: after the rename the
canonical path is absent, which every surface reports as "normal for a
deployment that has never run generate-advisories", and the next write then
lands the un-suppressed rows anyway. Nothing here moves, copies or rewrites
the file; the refusal carries the ``mv`` an operator would run.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from trellis.errors import DegradedStoreWriteError
from trellis.schemas.advisory import Advisory, AdvisoryStatus

logger = structlog.get_logger(__name__)

#: How many per-row failures to name in the degradation detail. Enough to
#: recognise a pattern (one renamed field shows up identically on every
#: row), short enough that a cron log line stays readable.
_MAX_REPORTED_ROWS = 3

#: Mode for a *newly created* advisory file. Matches what ``write_text``
#: produced under the common ``umask 022`` before writes became atomic —
#: ``mkstemp`` creates ``0600``, and silently narrowing the live file would
#: break a container reader bind-mounting it under a different uid. When the
#: destination already exists its own mode is preserved instead.
_NEW_FILE_MODE = 0o644


@dataclass(frozen=True, slots=True)
class AdvisoryLoadDegradation:
    """What a load could not read, and what an operator should do about it.

    Constructed only when something was unreadable — a store whose file
    parsed in full (or was simply absent) has ``degradation is None``, so
    the presence of this record is itself the signal.
    """

    #: The file that could not be read in full.
    path: str
    #: Machine-readable cause: ``unreadable_file``, ``malformed_json``,
    #: ``malformed_envelope``, ``invalid_rows`` or ``load_failed``.
    reason: str
    #: Human-readable specifics — the exception text, or the first few
    #: per-row validation failures.
    detail: str
    #: Rows that *did* parse and are being served.
    rows_loaded: int = 0
    #: Rows that did not. Zero for a whole-file failure, where the row
    #: count is unknowable.
    rows_skipped: int = 0

    @property
    def recovery(self) -> str:
        """The shell command that clears the degraded state.

        Deliberately concrete. An operator meets this in a 03:00 cron log,
        where a diagnosis is worth much less than the fix. Moving the file
        aside rather than deleting it keeps the bytes for inspection; the
        next generation run rebuilds the findings, though suppression
        decisions the file held are not recoverable from it by machine.
        """
        return f"mv {self.path} {self.path}.corrupt"

    def to_dict(self) -> dict[str, Any]:
        """Flat view for ``--format json`` payloads and structured logs."""
        return {
            "path": self.path,
            "reason": self.reason,
            "detail": self.detail,
            "rows_loaded": self.rows_loaded,
            "rows_skipped": self.rows_skipped,
            "recovery": self.recovery,
        }


class AdvisoryStore:
    """Load and save advisories from a JSON file.

    Follows the same lightweight pattern as :class:`PolicyStore`.
    Advisories are small, infrequently updated, and loaded in full —
    a JSON file is the right weight class.

    File format::

        {"advisories": [<Advisory.model_dump()>, ...]}

    A store whose file could not be read in full is **degraded**: reads
    serve what parsed, writes raise
    :class:`~trellis.errors.DegradedStoreWriteError`. See the module
    docstring for why those two are not the same decision.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._advisories: dict[str, Advisory] = {}
        self._degradation: AdvisoryLoadDegradation | None = None
        if self._path.exists():
            self._load()

    # -- Public API --

    @property
    def path(self) -> Path:
        """The file this store reads and writes."""
        return self._path

    @property
    def degradation(self) -> AdvisoryLoadDegradation | None:
        """What the load could not read, or ``None`` when it read cleanly.

        An *absent* file is not degradation — a deployment that has never
        generated an advisory is a normal empty store, and conflating the
        two is the distinction #393 asks for ("no file" vs "unreadable
        file").
        """
        return self._degradation

    @property
    def is_degraded(self) -> bool:
        """Whether this store refuses writes because its load was partial."""
        return self._degradation is not None

    def list(
        self,
        *,
        scope: str | None = None,
        min_confidence: float = 0.0,
        include_suppressed: bool = False,
    ) -> list[Advisory]:
        """Return advisories, optionally filtered by scope and confidence.

        Suppressed advisories are excluded by default — they stay in the
        store so the fitness loop can restore them when evidence warrants
        (see :meth:`restore`), but retrieval callers only see active ones.
        Pass ``include_suppressed=True`` to inspect the full set (tools,
        audits, fitness-loop scoring).

        Results are ordered by confidence descending.

        Works on a degraded store, serving whatever parsed. That is the
        lenient half of the posture and the reason retrieval survives a
        corrupt file.
        """
        result = list(self._advisories.values())
        if not include_suppressed:
            result = [a for a in result if a.status == AdvisoryStatus.ACTIVE]
        if scope is not None:
            result = [a for a in result if a.scope == scope]
        if min_confidence > 0.0:
            result = [a for a in result if a.confidence >= min_confidence]
        result.sort(key=lambda a: a.confidence, reverse=True)
        return result

    def get(self, advisory_id: str) -> Advisory | None:
        """Get an advisory by ID.

        Returns the advisory regardless of status — suppressed advisories
        remain retrievable by ID so the fitness loop can evaluate them
        and the UI can surface suppression history.

        **A ``None`` from a degraded store does not mean "no such
        advisory"** — it may mean "the row was unreadable". Callers that
        treat absence as *new* (``AdvisoryGenerator._carry_forward_status``
        is the one that matters) must check :attr:`is_degraded` first; the
        write refusal is what stops that mistake from reaching disk.
        """
        return self._advisories.get(advisory_id)

    def put(self, advisory: Advisory) -> Advisory:
        """Add or replace an advisory.  Persists immediately."""
        self._advisories[advisory.advisory_id] = advisory
        self._save()
        logger.info("advisory_stored", advisory_id=advisory.advisory_id)
        return advisory

    def put_many(self, advisories: Sequence[Advisory]) -> int:
        """Add or replace multiple advisories.  Single write."""
        for advisory in advisories:
            self._advisories[advisory.advisory_id] = advisory
        self._save()
        logger.info("advisories_stored", count=len(advisories))
        return len(advisories)

    def suppress(
        self,
        advisory_id: str,
        *,
        reason: str | None = None,
    ) -> Advisory | None:
        """Soft-suppress an advisory: flip status, stamp metadata, persist.

        Returns the updated advisory, or ``None`` if the id is unknown.
        Idempotent — suppressing an already-suppressed advisory is a
        no-op and returns the existing record without updating
        ``suppressed_at``.

        Unlike :meth:`remove`, the advisory is preserved so it can be
        restored via :meth:`restore` if later evidence warrants.
        """
        advisory = self._advisories.get(advisory_id)
        if advisory is None:
            return None
        if advisory.status == AdvisoryStatus.SUPPRESSED:
            return advisory
        updated = advisory.model_copy(
            update={
                "status": AdvisoryStatus.SUPPRESSED,
                "suppressed_at": datetime.now(UTC),
                "suppression_reason": reason,
                "updated_at": datetime.now(UTC),
            }
        )
        self._advisories[advisory_id] = updated
        self._save()
        logger.info(
            "advisory_suppressed",
            advisory_id=advisory_id,
            reason=reason,
        )
        return updated

    def restore(self, advisory_id: str) -> Advisory | None:
        """Restore a suppressed advisory to active status.

        Returns the updated advisory, or ``None`` if the id is unknown.
        Idempotent — restoring an already-active advisory is a no-op.
        Clears ``suppressed_at`` and ``suppression_reason``.
        """
        advisory = self._advisories.get(advisory_id)
        if advisory is None:
            return None
        if advisory.status == AdvisoryStatus.ACTIVE:
            return advisory
        updated = advisory.model_copy(
            update={
                "status": AdvisoryStatus.ACTIVE,
                "suppressed_at": None,
                "suppression_reason": None,
                "updated_at": datetime.now(UTC),
            }
        )
        self._advisories[advisory_id] = updated
        self._save()
        logger.info("advisory_restored", advisory_id=advisory_id)
        return updated

    def remove(self, advisory_id: str) -> bool:
        """Hard-delete an advisory by ID.  Returns ``True`` if found.

        This is the irreversible path and is intended for manual
        cleanup (admin commands, broken-state recovery). The fitness
        loop should use :meth:`suppress` instead so the record remains
        available for later restoration.
        """
        if advisory_id not in self._advisories:
            return False
        del self._advisories[advisory_id]
        self._save()
        logger.info("advisory_removed", advisory_id=advisory_id)
        return True

    def clear(self) -> int:
        """Remove all advisories.  Returns count removed.

        Refuses on a degraded store like every other write. Resetting a
        file this store could not read is an operator decision taken at
        the shell (see :attr:`AdvisoryLoadDegradation.recovery`), not one
        an admin surface should be able to take by accident.
        """
        count = len(self._advisories)
        self._advisories.clear()
        self._save()
        logger.info("advisories_cleared", count=count)
        return count

    # -- Persistence --

    def _load(self) -> None:
        """Load advisories, recording anything that could not be read.

        The outer catch is broad on purpose, and that breadth was never
        the defect #393 describes. The defect was that the handler left a
        state indistinguishable from an empty deployment and then let the
        next write act on it. Breadth is what keeps
        :mod:`trellis.stores.advisory_source`'s promise unconditional —
        constructing a store never raises, so retrieval never falls over
        on this file, whatever shape the corruption takes. What changed is
        that every failure path now *records* itself, and the record is
        what refuses the write.
        """
        try:
            self._load_rows()
        # Degrades, never swallows: the record below is what refuses the write.
        except Exception as exc:
            self._degrade("load_failed", f"{type(exc).__name__}: {exc}")

    def _load_rows(self) -> None:
        """Parse the file into ``self._advisories``, degrading per failure."""
        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # UnicodeDecodeError is a ValueError, not an OSError, and is the
            # shape a truncated or partially-binary write actually takes.
            self._degrade("unreadable_file", f"{type(exc).__name__}: {exc}")
            return

        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            self._degrade("malformed_json", str(exc))
            return

        if not isinstance(raw, dict) or not isinstance(raw.get("advisories", []), list):
            self._degrade(
                "malformed_envelope",
                'expected a JSON object with an "advisories" list, got '
                f"{type(raw).__name__}",
            )
            return

        skipped: list[str] = []
        for index, entry in enumerate(raw.get("advisories", [])):
            try:
                advisory = Advisory.model_validate(entry)
            # Broad on purpose: one bad row costs one row, not the file.
            except Exception as exc:
                skipped.append(f"row {index}: {type(exc).__name__}")
                continue
            self._advisories[advisory.advisory_id] = advisory

        if skipped:
            detail = "; ".join(skipped[:_MAX_REPORTED_ROWS])
            if len(skipped) > _MAX_REPORTED_ROWS:
                detail += f"; (+{len(skipped) - _MAX_REPORTED_ROWS} more)"
            self._degrade("invalid_rows", detail, rows_skipped=len(skipped))
            return

        logger.info(
            "advisories_loaded",
            count=len(self._advisories),
            path=str(self._path),
        )

    def _degrade(self, reason: str, detail: str, *, rows_skipped: int = 0) -> None:
        """Mark the store degraded and say so at a level operators see.

        ``error``, not ``info``. The CLI's root callback pins
        ``TRELLIS_LOG_LEVEL=WARNING`` unless ``--verbose`` is passed, so an
        ``info`` line here would be filtered out of the one surface that
        runs this nightly — the same class of no-op as a ``logger.debug``
        under an INFO filter.
        """
        self._degradation = AdvisoryLoadDegradation(
            path=str(self._path),
            reason=reason,
            detail=detail,
            rows_loaded=len(self._advisories),
            rows_skipped=rows_skipped,
        )
        logger.error(
            "advisory_load_degraded",
            **self._degradation.to_dict(),
            impact=(
                "Advisories that parsed are still served; every write is "
                "refused so the unreadable file cannot be overwritten."
            ),
        )

    def _refuse_if_degraded(self) -> None:
        """Raise rather than rewrite a file this store could not read."""
        degradation = self._degradation
        if degradation is None:
            return
        msg = (
            f"Refusing to write the Trellis advisory file at {degradation.path}: "
            f"it loaded degraded ({degradation.reason}: {degradation.detail}). "
            f"{degradation.rows_loaded} row(s) parsed and are being served; "
            f"{degradation.rows_skipped} could not be read. Writing would "
            "replace the file with only what parsed, discarding the rest and "
            "reviving any advisory the fitness loop had suppressed. To reset:"
        )
        raise DegradedStoreWriteError(
            msg,
            store="advisory",
            path=degradation.path,
            recovery=degradation.recovery,
        )

    def _save(self) -> None:
        """Persist current advisories to the JSON file.

        Atomic: the payload is written to a temp file in the same
        directory and moved into place with :func:`os.replace`. A direct
        ``write_text`` truncates the destination and *then* writes, so a
        crash, a full disk or a killed cron between the two produces
        exactly the half-written file the rest of this module now has to
        survive. This store is the file's only writer, so closing that
        window closes the main way the state gets created.
        """
        self._refuse_if_degraded()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "advisories": [a.model_dump(mode="json") for a in self._advisories.values()]
        }
        _atomic_write_text(self._path, json.dumps(data, indent=2, default=str))


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path``'s contents in one step, preserving its mode.

    ``mkstemp`` creates ``0600``. Inheriting that would silently narrow a
    live file another uid reads — the reference deployment bind-mounts the
    data directory into containers — so an existing destination's mode is
    copied onto the temp file and a fresh one gets the ``0644`` that
    ``write_text`` produced under the usual umask.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else _NEW_FILE_MODE
        tmp_path.chmod(mode)
        tmp_path.replace(path)
    finally:
        # A successful ``replace`` consumed the temp name, so this is a
        # no-op on the happy path and cleanup on every other.
        tmp_path.unlink(missing_ok=True)
