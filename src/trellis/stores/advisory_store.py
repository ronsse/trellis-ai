"""Advisory store — JSON file-based persistence for advisories.

Failure posture — read leniently, refuse to write
-------------------------------------------------
This store whole-file-rewrites on every write: ``_save`` serialises the
in-memory rows over the path it loaded from. That makes a *lenient read*
and a *lenient write* two very different promises, and until #393 the
module made the first while accidentally making the second.

The read stays lenient, and deliberately so. :mod:`trellis.stores.advisory_source`
argues it and is right: advisories are hint-only guidance attached to a
pack, and a corrupt advisory file must not take retrieval down with it —
unlike :mod:`trellis.mutate.policy_source`, which raises, because degrading
an *access-control* file to "no policies" silently disables a guarantee the
caller believes it has. The asymmetry is real and it survives here.

What does not survive is applying that leniency to the write. Degrading an
unreadable file to an empty set and then rewriting the path deletes it, and
the deletion is the *quiet* half of the failure:

1. ``_load`` fails, logs, leaves the store empty.
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
Since #426 the machinery that implements all of this lives in
:class:`~trellis.stores.degradable_json_store.DegradableJsonStore`, shared
with :class:`~trellis.stores.policy_store.PolicyStore`; what stays here is
what only makes sense about *advisories*.

Per-row, not per-file
---------------------
Validation is per row. One unparseable entry — a hand-edit, a renamed
field — costs that entry rather than the file, so a pack still carries the
advisories that *are* readable. That leniency is only safe because the
write refuses: a partial load followed by a permitted write would rewrite
the file without the skipped rows, which is the same data loss at a
narrower granularity. The two halves are a pair; neither is safe alone.

Unlike ``policies.json``, a **duplicate id here is last-one-wins** rather
than degradation. Nothing else reads this file as a list, so there is no
second reader for a collapsed view to disagree with — the divergence is
deliberate and lives in
:meth:`~trellis.stores.policy_store.PolicyStore._reject_row`, which this
store does not override.

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

Both are closed by one guard: ``refuse_if_stale`` records a fingerprint of
the file as loaded and refuses
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
(#438). Both guards being present in both stores is what made #426's
extraction safe to take: a base pulled out a release earlier would have
frozen a half-guarded one.

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

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

import structlog

from trellis.schemas.advisory import Advisory, AdvisoryStatus
from trellis.stores.degradable_json_store import DegradableJsonStore, LoadDegradation

logger = structlog.get_logger(__name__)


class AdvisoryStore(DegradableJsonStore[Advisory]):
    """Load and save advisories from a JSON file.

    Advisories are small, infrequently updated, and loaded in full — a
    JSON file is the right weight class. The *storage* shape is the one
    :class:`~trellis.stores.policy_store.PolicyStore` uses, and since #413
    so is the failure posture: that store had this store's pre-#393
    behaviour on a higher-stakes file, where a rewrite after a degraded
    load laundered the corruption past the strict enforcement reader in
    :mod:`trellis.mutate.policy_source`. Since #426 they share the
    implementation as well as the posture
    (:class:`~trellis.stores.degradable_json_store.DegradableJsonStore`).

    File format::

        {"advisories": [<Advisory.model_dump()>, ...]}

    A store whose file could not be read in full is **degraded**: reads
    serve what parsed, writes raise
    :class:`~trellis.errors.DegradedStoreWriteError`. See the module
    docstring for why those two are not the same decision.
    """

    _envelope_key: ClassVar[str] = "advisories"
    _envelope_article: ClassVar[str] = "an"
    _store_label: ClassVar[str] = "advisory"
    _loaded_event: ClassVar[str] = "advisories_loaded"
    _degraded_event: ClassVar[str] = "advisory_load_degraded"
    _degraded_impact: ClassVar[str] = (
        "Advisories that parsed are still served; every write is "
        "refused so the unreadable file cannot be overwritten."
    )
    _stale_recovery: ClassVar[str] = "trellis analyze advisory-effectiveness --dry-run"

    # -- Row handling --

    @staticmethod
    def _parse_row(entry: Any) -> Advisory:
        return Advisory.model_validate(entry)

    @staticmethod
    def _row_id(row: Advisory) -> str:
        return row.advisory_id

    # -- Public API --

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
        result = list(self._rows.values())
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
        return self._rows.get(advisory_id)

    def put(self, advisory: Advisory) -> Advisory:
        """Add or replace an advisory.  Persists immediately."""
        self.refuse_if_degraded()
        self.refuse_if_stale()
        restore = self._snapshot()
        self._rows[advisory.advisory_id] = advisory
        self._save_or_roll_back(restore)
        logger.info("advisory_stored", advisory_id=advisory.advisory_id)
        return advisory

    def put_many(self, advisories: Sequence[Advisory]) -> int:
        """Add or replace multiple advisories.  Single write."""
        self.refuse_if_degraded()
        self.refuse_if_stale()
        restore = self._snapshot()
        for advisory in advisories:
            self._rows[advisory.advisory_id] = advisory
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
        advisory = self._rows.get(advisory_id)
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
        self._rows[advisory_id] = updated
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
        advisory = self._rows.get(advisory_id)
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
        self._rows[advisory_id] = updated
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
        if advisory_id not in self._rows:
            return False
        restore = self._snapshot()
        del self._rows[advisory_id]
        self._save_or_roll_back(restore)
        logger.info("advisory_removed", advisory_id=advisory_id)
        return True

    def clear(self) -> int:
        """Remove all advisories.  Returns count removed.

        Refuses on a degraded store like every other write. Resetting a
        file this store could not read is an operator decision taken at
        the shell (see
        :attr:`~trellis.stores.degradable_json_store.LoadDegradation.recovery`),
        not one an admin surface should be able to take by accident.

        Refuses on a stale store too, and the returned count is the second
        reason: it is the size of the in-memory view, so on a store another
        process has written it would under-report what the file lost.
        """
        self.refuse_if_degraded()
        self.refuse_if_stale()
        restore = self._snapshot()
        count = len(self._rows)
        self._rows.clear()
        self._save_or_roll_back(restore)
        logger.info("advisories_cleared", count=count)
        return count

    # -- Refusal messages --

    def _degraded_write_message(self, degradation: LoadDegradation) -> str:
        """What rewriting a partially-read ``advisories.json`` would cost.

        The ``mv`` the refusal carries keeps the bytes for inspection; the
        next generation run rebuilds the *findings*, but the suppression
        decisions the file held are not recoverable from it by machine.
        That is the asymmetry worth stating to an operator at 03:00: they
        are being offered a fix that restores the pipeline and not the
        curation.
        """
        return (
            f"Refusing to write the Trellis advisory file at {degradation.path}: "
            f"it loaded degraded ({degradation.reason}: {degradation.detail}). "
            f"{degradation.rows_loaded} row(s) parsed and are being served; "
            f"{degradation.rows_skipped_display} could not be read. Writing "
            "would replace the file with only what parsed, discarding the rest "
            "and reviving any advisory the fitness loop had suppressed. To reset:"
        )

    def _stale_write_message(self) -> str:
        """What rewriting an ``advisories.json`` that moved under us would cost."""
        return (
            f"Refusing to write the Trellis advisory file at {self._path}: it "
            "changed after this process read it, so writing would replace "
            "whatever landed in between — deleting those advisories and, "
            "because advisory ids are stable (#394), reviving any "
            "suppression they carried. Re-read and retry:"
        )
