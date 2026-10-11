"""Schedule registry schema — operator-tunable state, never a command.

A :class:`ScheduledJob` is one row of ``schedule.json``
(:class:`~trellis.stores.schedule_store.ScheduleStore`): ``name``,
``cadence``, ``enabled``, an optional ``timeout_seconds`` override, and
``run_requested_at``. It carries no command, description or host-only
flag — those live in :data:`trellis.schedule.catalog.JOB_CATALOG`, the
only source of what a job runs, keyed by the same ``name``. ``name`` is
validated against that catalog here, on every read and write
(:func:`ScheduledJob.model_validate`, called from both
:meth:`~trellis.stores.schedule_store.ScheduleStore.put` and from loading
the file), so a row naming an unknown job is a validation failure, not a
job that runs. ``extra="forbid"`` (:class:`~trellis.core.base.TrellisModel`)
means a legacy or tampered row still carrying the old ``command`` field
fails validation the same way — see
:mod:`trellis.schedule.catalog` for the full boundary argument and
:class:`~trellis.stores.schedule_store.ScheduleStore`'s module docstring
for how a row that fails this validation is handled (the row degrades;
nothing it names ever runs).

``cadence`` is a 5-field cron expression (:mod:`trellis.core.cron`) or
``None`` for a manual-only job — one an operator or the UI's "Run now"
triggers by setting :attr:`ScheduledJob.run_requested_at`, never on a
timer. ``timeout_seconds``, when set, overrides the catalog's
``default_timeout_seconds`` for this job only, bounded 1..86400 — this
schema does not enforce it at runtime; enforcing it is the dispatcher's
job (the reference script under ``docs/ops/``), which this PR documents
but does not install.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from trellis.core.base import TimestampedModel
from trellis.core.cron import CronSchedule, CronSyntaxError
from trellis.schedule.catalog import JOB_CATALOG


class ScheduledJob(TimestampedModel):
    """One registry entry: operator-tunable state for a catalog job.

    The command, description and host-only flag all come from
    :data:`trellis.schedule.catalog.JOB_CATALOG` under this row's
    ``name`` — never from this model. See the module docstring.
    """

    name: str = Field(
        min_length=1,
        description="Must be a key in trellis.schedule.catalog.JOB_CATALOG.",
    )
    cadence: str | None = Field(
        default=None,
        description="5-field cron expression, or null for manual-only.",
    )
    enabled: bool = Field(default=True)
    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        le=86400,
        description=(
            "Overrides the catalog's default_timeout_seconds for this "
            "job only; null defers to the catalog default."
        ),
    )
    run_requested_at: datetime | None = Field(
        default=None,
        description="Set by a manual 'Run now' trigger; cleared once picked up.",
    )

    @field_validator("name", mode="after")
    @classmethod
    def _known_catalog_name(cls, value: str) -> str:
        if not value.strip():
            msg = "name must not be blank"
            raise ValueError(msg)
        if value not in JOB_CATALOG:
            msg = (
                f"name {value!r} is not a trellis.schedule.catalog.JOB_CATALOG "
                "job — an unknown name is a degraded row, never a run"
            )
            raise ValueError(msg)
        return value

    @field_validator("cadence", mode="after")
    @classmethod
    def _valid_cron_or_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            CronSchedule.parse(value)
        except CronSyntaxError as exc:
            msg = f"cadence is not a valid 5-field cron expression: {exc}"
            raise ValueError(msg) from exc
        return value
