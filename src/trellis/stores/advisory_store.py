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

Degradation is not the only stale view
--------------------------------------
The primitive above is *a whole-file rewrite from an in-memory view that is
no longer the file*, and a degraded load is only one way to get one. Two
others reach the identical end state with nothing degraded (#438):

* **Another process wrote the file.** ``advisories.json`` has three writer
  processes on the reference deployment — the nightly ``trellis worker
  curate`` cron, the host's ``trellis analyze generate-advisories`` /
  ``advisory-effectiveness``, and the containerised ``POST
  /api/v1/advisories/generate`` against the same bind-mounted data dir. A
  store that loaded ``[A]`` and then rewrites the file after another made
  it ``[A, B]`` deletes ``B`` — from disk and from every future pack — with
  no error anywhere. Since stable advisory ids (#394) it deletes ``B``'s
  *suppression* too, which is the half no read ever notices.
* **The file appeared after construction.** A store built while the path
  was absent is not degraded, and its first write replaces a file it never
  read.

Both are closed by one guard: :meth:`AdvisoryStore.refuse_if_stale` records
a fingerprint of the file as loaded and refuses
(:class:`~trellis.errors.StaleStoreWriteError`) if it no longer matches.
Unlike a degraded load this one is **transient** — re-read and redo, rather
than go and look at the file. It is a compare-and-swap, not a lock: two
writers can still interleave between the check and the ``os.replace``, so
it closes the wide window and narrows the tiny one. Last-writer-wins
remains the model.

This is the guard #423 landed on
:class:`~trellis.stores.policy_store.PolicyStore`, ported here because that
change generalised its *analysis* one store further than its *fix*:
``policies.json`` does not exist on the reference deployment, while
``advisories.json`` is 51 KB of live rows rewritten by the nightly cron
(#438).

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
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from trellis.core.atomic_write import atomic_write_text
from trellis.errors import DegradedStoreWriteError, StaleStoreWriteError
from trellis.schemas.advisory import Advisory, AdvisoryStatus

logger = structlog.get_logger(__name__)

#: How many per-row failures to name in the degradation detail. Enough to
#: recognise a pattern (one renamed field shows up identically on every
#: row), short enough that a cron log line stays readable.
_MAX_REPORTED_ROWS = 3


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
    #: Rows that did not — or ``None`` when the count is *unknowable*, which
    #: is every whole-file failure: nothing got as far as counting rows.
    #: Deliberately not ``0``. "0 could not be read" tells an operator at
    #: 03:00 that nothing was lost, when the whole file may be sitting
    #: unread; :attr:`rows_skipped_display` is what the surfaces render so
    #: the two cases cannot present identically.
    rows_skipped: int | None = None

    @property
    def recovery(self) -> str:
        """The shell command that clears the degraded state.

        Deliberately concrete. An operator meets this in a 03:00 cron log,
        where a diagnosis is worth much less than the fix. Moving the file
        aside rather than deleting it keeps the bytes for inspection; the
        next generation run rebuilds the findings, though suppression
        decisions the file held are not recoverable from it by machine.

        Both operands are ``shlex.quote``d (#427). A data dir containing a
        space — ``/tmp/my staging dir/``, ``~/Library/Application
        Support/…`` — otherwise word-splits into an ``mv`` with **four**
        operands, which does not run. That is the same failure as the
        Rich-markup and hard-wrap cases the CLI renderer already guards:
        an unrunnable command printed to the operator *as* the fix. It is
        the one string in this module that must survive every layer
        byte-for-byte, and the shell is the last of them.
        """
        quoted = shlex.quote(self.path)
        return f"mv {quoted} {shlex.quote(self.path + '.corrupt')}"

    @property
    def rows_skipped_display(self) -> str:
        """``rows_skipped`` for humans — ``"unknown"`` rather than ``0``."""
        return "unknown" if self.rows_skipped is None else str(self.rows_skipped)

    def to_dict(self) -> dict[str, Any]:
        """Flat view for ``--format json`` payloads and structured logs."""
        return {
            "path": self.path,
            "reason": self.reason,
            "detail": self.detail,
            "rows_loaded": self.rows_loaded,
            "rows_skipped": self.rows_skipped,
            "rows_skipped_display": self.rows_skipped_display,
            "recovery": self.recovery,
        }


class AdvisoryStore:
    """Load and save advisories from a JSON file.

    Advisories are small, infrequently updated, and loaded in full — a
    JSON file is the right weight class. The *storage* shape is the one
    :class:`~trellis.stores.policy_store.PolicyStore` uses, and since #413
    so is the failure posture: that store had this store's pre-#393
    behaviour on a higher-stakes file, where a rewrite after a degraded
    load laundered the corruption past the strict enforcement reader in
    :mod:`trellis.mutate.policy_source`.

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
        # ``stat`` rather than ``exists()``: ``Path.exists`` swallows every
        # ``OSError`` internally, so a file under an unsearchable directory
        # presents as *absent* — which here means "a deployment that has
        # never generated an advisory", the one state that is neither
        # degraded nor stale and so is freely writable. That is the same
        # laundering by a different door.
        try:
            self._path.stat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._degrade("unreadable_file", f"{type(exc).__name__}: {exc}")
        else:
            self._load()
        self._loaded_fingerprint = self._fingerprint()

    # -- Public API --

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
        self.refuse_if_degraded()
        self.refuse_if_stale()
        restore = self._snapshot()
        self._advisories[advisory.advisory_id] = advisory
        self._save_or_roll_back(restore)
        logger.info("advisory_stored", advisory_id=advisory.advisory_id)
        return advisory

    def put_many(self, advisories: Sequence[Advisory]) -> int:
        """Add or replace multiple advisories.  Single write."""
        self.refuse_if_degraded()
        self.refuse_if_stale()
        restore = self._snapshot()
        for advisory in advisories:
            self._advisories[advisory.advisory_id] = advisory
        self._save_or_roll_back(restore)
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

        Both guards run *before* the lookup, not after. This method has two
        early returns that answer from the in-memory view and never reach a
        write, so ``_save``'s guards are too late to prevent either: on a
        degraded store the ``None`` reads as "no such advisory" for a row
        that is in the file and merely failed to parse, and on a **stale**
        store it says the same for a row another process added since this
        one loaded. The idempotent branch is worse — it returns a row read
        from a superseded file and reports the suppression as already
        applied, when the file's current row may be ACTIVE. Wrong answers,
        not merely unhelpful ones.
        """
        self.refuse_if_degraded()
        self.refuse_if_stale()
        advisory = self._advisories.get(advisory_id)
        if advisory is None:
            return None
        if advisory.status == AdvisoryStatus.SUPPRESSED:
            return advisory
        restore = self._snapshot()
        updated = advisory.model_copy(
            update={
                "status": AdvisoryStatus.SUPPRESSED,
                "suppressed_at": datetime.now(UTC),
                "suppression_reason": reason,
                "updated_at": datetime.now(UTC),
            }
        )
        self._advisories[advisory_id] = updated
        self._save_or_roll_back(restore)
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

        Guarded before the lookup for the reasons :meth:`suppress` gives:
        both early returns answer from a view that may no longer be the
        file, and neither reaches a write.
        """
        self.refuse_if_degraded()
        self.refuse_if_stale()
        advisory = self._advisories.get(advisory_id)
        if advisory is None:
            return None
        if advisory.status == AdvisoryStatus.ACTIVE:
            return advisory
        restore = self._snapshot()
        updated = advisory.model_copy(
            update={
                "status": AdvisoryStatus.ACTIVE,
                "suppressed_at": None,
                "suppression_reason": None,
                "updated_at": datetime.now(UTC),
            }
        )
        self._advisories[advisory_id] = updated
        self._save_or_roll_back(restore)
        logger.info("advisory_restored", advisory_id=advisory_id)
        return updated

    def remove(self, advisory_id: str) -> bool:
        """Hard-delete an advisory by ID.  Returns ``True`` if found.

        This is the irreversible path and is intended for manual
        cleanup (admin commands, broken-state recovery). The fitness
        loop should use :meth:`suppress` instead so the record remains
        available for later restoration.

        Refuses *before* the membership check, for both reasons. A degraded
        store would answer ``False`` — "no such advisory" — for a row that
        is in the file and merely failed to parse; a stale one would answer
        ``False`` for a row another process added since. This path returns
        before reaching a write at all, so ``_save``'s guards cannot cover
        it.
        """
        self.refuse_if_degraded()
        self.refuse_if_stale()
        if advisory_id not in self._advisories:
            return False
        restore = self._snapshot()
        del self._advisories[advisory_id]
        self._save_or_roll_back(restore)
        logger.info("advisory_removed", advisory_id=advisory_id)
        return True

    def clear(self) -> int:
        """Remove all advisories.  Returns count removed.

        Refuses on a degraded store like every other write. Resetting a
        file this store could not read is an operator decision taken at
        the shell (see :attr:`AdvisoryLoadDegradation.recovery`), not one
        an admin surface should be able to take by accident.

        Refuses on a stale store too, and the returned count is the second
        reason: it is the size of the in-memory view, so on a store another
        process has written it would under-report what the file lost.
        """
        self.refuse_if_degraded()
        self.refuse_if_stale()
        restore = self._snapshot()
        count = len(self._advisories)
        self._advisories.clear()
        self._save_or_roll_back(restore)
        logger.info("advisories_cleared", count=count)
        return count

    # -- Persistence --

    def _snapshot(self) -> dict[str, Advisory]:
        """Shallow copy of the in-memory rows, for rollback on a failed save.

        :class:`Advisory` is only ever replaced wholesale (``model_copy``),
        never mutated in place, so a shallow copy is a complete undo. The
        corpus is tens of rows; this is not a cost worth avoiding.
        """
        return dict(self._advisories)

    def _save_or_roll_back(self, restore: dict[str, Advisory]) -> None:
        """Persist, and put memory back the way it was if the write fails.

        Without this a refused write still mutated the object: a ``clear()``
        that raised had already emptied ``list()``, and a ``restore()`` that
        raised had already un-suppressed the row in memory — #393's own
        symptom, surviving in-process, landing on exactly the caller the
        store-level refusal exists for (one that catches the error and keeps
        serving packs from the same store). The same applies off the
        degraded path: a full disk must not leave an advisory in memory that
        is not on disk.
        """
        try:
            self._save()
        except Exception:
            self._advisories = restore
            raise

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

        if not isinstance(raw, dict):
            self._degrade(
                "malformed_envelope",
                'expected a JSON object with an "advisories" list, got '
                f"{type(raw).__name__}",
            )
            return

        # A *missing* key is degradation, not an empty store. ``_save`` always
        # emits ``advisories``, so a dict without it is by construction not a
        # file this store produced: a hand-edit, a renamed field, the wrong
        # file at this path, or a future schema. Defaulting it to ``[]`` made
        # ``{}`` and ``{"advisorees": [...]}`` load as a *clean* empty store
        # and left the whole of #393 intact for those shapes — degrade to
        # empty in silence, then let the next nightly write replace the file.
        if "advisories" not in raw:
            self._degrade(
                "malformed_envelope",
                'JSON object has no "advisories" key (keys: '
                f"{sorted(raw)[:_MAX_REPORTED_ROWS]})",
            )
            return

        if not isinstance(raw["advisories"], list):
            self._degrade(
                "malformed_envelope",
                'expected "advisories" to be a list, got '
                f"{type(raw['advisories']).__name__}",
            )
            return

        skipped: list[str] = []
        for index, entry in enumerate(raw["advisories"]):
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

    def _degrade(
        self, reason: str, detail: str, *, rows_skipped: int | None = None
    ) -> None:
        """Mark the store degraded and say so at a level operators see.

        ``error``, not ``info``. The CLI's root callback *defaults*
        ``TRELLIS_LOG_LEVEL`` to ``WARNING`` when the env var is absent
        (``trellis_cli.main._root``; an explicit env var always wins), so on
        a default invocation an ``info`` line here is filtered out of the
        one surface that runs this nightly — the same class of no-op as a
        ``logger.debug`` under an INFO filter.
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

    def _fingerprint(self) -> tuple[int, int, int] | None:
        """Identity of the file as this store last saw it, ``None`` if absent.

        ``st_ino`` is the load-bearing part: every write here lands through
        ``os.replace`` from a fresh temp file, so a completed write by any
        process changes the inode even if size and mtime happen to collide.
        Size and mtime are kept because they catch the other shape — an
        in-place edit that keeps the inode, which is what ``sed -i`` and
        an editor configured to write through produce.

        Inode *reuse* — a replacement landing on the number this store
        recorded, with mtime and size colliding too — is unreachable by
        construction rather than defended against: ``atomic_write_text``
        creates its temp file while the target still exists, so the
        target's inode is never free to be handed back.
        """
        try:
            st = self._path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def refuse_if_stale(self) -> None:
        """Raise rather than rewrite a file that changed after we read it.

        See the module docstring: a degraded load is one way an in-memory
        view stops matching the file, another process writing it is a
        second, and a file appearing after construction is a third. All
        three end the same way, and only the first of them degrades.

        Public for the same reason :meth:`refuse_if_degraded` is — a caller
        about to make a *batch* of writes should fail before it starts
        rather than part-way through — and it is a **compare-and-swap, not
        a lock**: two writers can still interleave between this check and
        the ``os.replace`` inside :meth:`_save`. It closes the wide window
        (a store that loaded minutes ago, which is every nightly run) and
        narrows the tiny one; it does not make the write exclusive.
        """
        if self._fingerprint() == self._loaded_fingerprint:
            return
        msg = (
            f"Refusing to write the Trellis advisory file at {self._path}: it "
            "changed after this process read it, so writing would replace "
            "whatever landed in between — deleting those advisories and, "
            "because advisory ids are stable (#394), reviving any "
            "suppression they carried. Re-read and retry:"
        )
        raise StaleStoreWriteError(
            msg,
            store="advisory",
            path=str(self._path),
            recovery="trellis analyze advisory-effectiveness --dry-run",
        )

    def refuse_if_degraded(self) -> None:
        """Raise rather than rewrite a file this store could not read.

        Public because a caller about to make a *batch* of writes should
        fail before it starts rather than part-way through — see
        :func:`~trellis.retrieve.effectiveness.run_advisory_fitness_loop`.
        Every write path calls it too, so calling it is never a way to get
        a write past the refusal, and not calling it is never a way to
        avoid one.
        """
        degradation = self._degradation
        if degradation is None:
            return
        msg = (
            f"Refusing to write the Trellis advisory file at {degradation.path}: "
            f"it loaded degraded ({degradation.reason}: {degradation.detail}). "
            f"{degradation.rows_loaded} row(s) parsed and are being served; "
            f"{degradation.rows_skipped_display} could not be read. Writing "
            "would "
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

        Atomic, via :func:`~trellis.core.atomic_write.atomic_write_text`.
        A direct ``write_text`` truncates the destination and *then*
        writes, so a crash, a full disk or a killed cron between the two
        produces exactly the half-written file the rest of this module now
        has to survive. No other *code* writes this file, so closing that
        window closes the main way the state gets created.

        Atomicity is not a concurrency guarantee, and this file has three
        writer **processes** — the nightly ``trellis worker curate`` cron,
        the host ``trellis analyze`` advisory commands, and ``POST
        /api/v1/advisories/generate`` in a container, against the same
        bind-mounted data dir. ``os.replace`` makes their writes atomic,
        not ordered; :meth:`refuse_if_stale` is what stops one of them
        rewriting another's work from a stale view (#438).
        """
        self.refuse_if_degraded()
        self.refuse_if_stale()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "advisories": [a.model_dump(mode="json") for a in self._advisories.values()]
        }
        atomic_write_text(self._path, json.dumps(data, indent=2, default=str))
        # What we just wrote is now what we "loaded": a second write from the
        # same store instance must not trip its own guard.
        self._loaded_fingerprint = self._fingerprint()
