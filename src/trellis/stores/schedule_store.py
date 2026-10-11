"""Schedule store — JSON file-based persistence for the job registry.

Same failure posture as :class:`~trellis.stores.policy_store.PolicyStore`
and :class:`~trellis.stores.advisory_store.AdvisoryStore`, built on the
shared :class:`~trellis.stores.degradable_json_store.DegradableJsonStore`:
**the read degrades and the write refuses.** A host dispatcher cron and a
containerised API are both future readers of the same bind-mounted
``schedule.json`` (see ``docs/design/p1-ui-control-plane.md`` §(b)); a
store that rewrote a partially-read file would silently drop whatever
didn't parse, which for a schedule registry means a job quietly stops
being dispatched rather than erroring.

The actual command boundary
----------------------------
This store never holds a command at all. A
:class:`~trellis.schemas.schedule.ScheduledJob` row is operator-tunable
state only (``name``, ``cadence``, ``enabled``,
``timeout_seconds`` override, ``run_requested_at``); what a job runs
comes from :data:`trellis.schedule.catalog.JOB_CATALOG`, keyed by the
row's ``name`` and fixed in source. ``ScheduledJob`` validates ``name``
against that catalog on every read and write, and ``extra="forbid"``
rejects a legacy or tampered row still carrying the old ``command``
field — in both cases :meth:`_parse_row` raises and the row degrades
(``invalid_rows``) rather than being treated as something to run. So
this file — shared, read-write-mounted into every container, and
API-editable once a later PR lands — can change only *whether* and
*when* a catalog job runs, never *what* runs. The one thing that execs a
job is the host dispatcher (``docs/ops/job-dispatcher.sh.example``,
documented but not installed), and it reads the command from
``trellis admin due-jobs``' catalog-joined output, never from this file.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

import structlog

from trellis.schemas.schedule import ScheduledJob
from trellis.stores.degradable_json_store import DegradableJsonStore, LoadDegradation

logger = structlog.get_logger(__name__)


class ScheduleStore(DegradableJsonStore[ScheduledJob]):
    """Load and save the job schedule registry from a JSON file.

    Backs ``trellis admin due-jobs`` and (read-only, for now) the
    reference dispatcher script. There is no REST route and no UI on this
    store yet — both are a later PR (plan p1, PR 6).

    File format::

        {"jobs": [<ScheduledJob.model_dump()>, ...]}
    """

    _envelope_key: ClassVar[str] = "jobs"
    _store_label: ClassVar[str] = "schedule"
    _loaded_event: ClassVar[str] = "schedule_loaded"
    _degraded_event: ClassVar[str] = "schedule_load_degraded"
    _degraded_impact: ClassVar[str] = (
        "Jobs that parsed are still listed; every write is refused so the "
        "unreadable file cannot be replaced by the partial view, which "
        "would silently stop dispatching whatever job didn't parse."
    )
    _stale_recovery: ClassVar[str] = "trellis admin due-jobs --format json"

    # -- Row handling --

    @staticmethod
    def _parse_row(entry: Any) -> ScheduledJob:
        return ScheduledJob.model_validate(entry)

    @staticmethod
    def _row_id(row: ScheduledJob) -> str:
        return row.name

    def _reject_row(self, row: ScheduledJob) -> str | None:
        """A duplicate job name is degradation, not a last-one-wins overwrite.

        This store keys by ``name``; a duplicate in the file means two
        rows silently collapse to one and whichever the loader did not
        keep is a job the dispatcher would stop running — the same
        laundering :class:`~trellis.stores.policy_store.PolicyStore` guards
        against for ``policy_id``.
        """
        if row.name in self._rows:
            return "duplicate job name"
        return None

    # -- Public API --

    def list(self) -> list[ScheduledJob]:
        """Return all jobs, in file order.

        Works on a degraded store, serving whatever parsed — an operator
        whose schedule file just broke still needs to see the jobs that
        are readable. **A degraded store's list is not the whole
        registry** — callers rendering it must say so (:attr:`degradation`).
        """
        return list(self._rows.values())

    def get(self, name: str) -> ScheduledJob | None:
        """Get a job by name.

        **A ``None`` from a degraded store does not mean "no such job"**
        — it may mean "the row was unreadable". Callers reporting absence
        to a human must check :attr:`is_degraded` first.
        """
        return self._rows.get(name)

    def put(self, job: ScheduledJob) -> ScheduledJob:
        """Add or replace a job. Persists immediately."""
        self.refuse_if_degraded()
        self.refuse_if_stale()
        restore = self._snapshot()
        self._rows[job.name] = job
        self._save_or_roll_back(restore)
        logger.info("schedule_job_stored", name=job.name)
        return job

    def put_many(self, jobs: Sequence[ScheduledJob]) -> int:
        """Add or replace multiple jobs in a single write. Returns the count."""
        self.refuse_if_degraded()
        self.refuse_if_stale()
        restore = self._snapshot()
        for job in jobs:
            self._rows[job.name] = job
        self._save_or_roll_back(restore)
        logger.info("schedule_jobs_stored", count=len(jobs))
        return len(jobs)

    def remove(self, name: str) -> bool:
        """Remove a job by name. Returns ``True`` if found."""
        self.refuse_if_degraded()
        self.refuse_if_stale()
        if name not in self._rows:
            return False
        restore = self._snapshot()
        del self._rows[name]
        self._save_or_roll_back(restore)
        logger.info("schedule_job_removed", name=name)
        return True

    # -- Refusal messages --

    def _degraded_write_message(self, degradation: LoadDegradation) -> str:
        return (
            f"Refusing to write the Trellis schedule file at {degradation.path}: "
            f"it loaded degraded ({degradation.reason}: {degradation.detail}). "
            f"{degradation.rows_loaded} job(s) parsed and are being shown; "
            f"{degradation.rows_skipped_display} could not be read. Writing "
            "would replace the file with only what parsed, silently dropping "
            "whatever job didn't — the dispatcher would just stop running it, "
            "with nothing in the schedule file to say why. To reset:"
        )

    def _stale_write_message(self) -> str:
        return (
            f"Refusing to write the Trellis schedule file at {self._path}: it "
            "changed after this process read it, so writing would replace "
            "whatever landed in between — silently dropping any job added or "
            "edited concurrently. Re-read and retry:"
        )

    def _unreadable_write_message(self, detail: str) -> str:
        return (
            f"Refusing to write the Trellis schedule file at {self._path}: its "
            f"identity could not be read ({detail}), so this process cannot "
            "tell whether the file changed after it read it. Writing anyway "
            "would replace a file it never saw. Check the path, then retry:"
        )
