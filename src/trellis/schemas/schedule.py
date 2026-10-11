"""Schedule registry schema — the host dispatcher's source of truth.

A :class:`ScheduledJob` is one row of ``schedule.json``
(:class:`~trellis.stores.schedule_store.ScheduleStore`): a name, a command
to run, and when it should run. ``command`` is an argv list — never a
shell string — so there is no interpolation step between this file and a
process exec; see the module docstring on
:class:`~trellis.stores.schedule_store.ScheduleStore` for the full
no-arbitrary-execution argument.

``cadence`` is a 5-field cron expression (:mod:`trellis.core.cron`) or
``None`` for a manual-only job — one an operator or the UI's "Run now"
triggers by setting :attr:`ScheduledJob.run_requested_at`, never on a
timer. ``host_only`` flags a job that depends on a resource the API
container will never be given (the docker socket, ``gh`` auth, the
``/mnt/data`` mount) — see ``docs/design/p1-ui-control-plane.md`` §(b):
the dispatcher that reads this registry is host-side already, but the
flag matters once a container-side reader exists (a later PR; out of
scope here) so it can skip what it could never run.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from trellis.core.base import TimestampedModel
from trellis.core.cron import CronSchedule, CronSyntaxError


class ScheduledJob(TimestampedModel):
    """One registry entry: what to run, on what cadence, and its last request.

    ``timeout_seconds`` is advisory — this schema does not enforce it;
    enforcing it is the dispatcher's job (the reference script under
    ``docs/ops/``), which this PR documents but does not install.
    """

    name: str = Field(min_length=1, description="Unique job name; the registry key.")
    command: list[str] = Field(
        min_length=1,
        description=(
            "Argv list, e.g. ['trellis', 'worker', 'tune', '--dry-run']. "
            "Never a shell string — see the store module docstring."
        ),
    )
    cadence: str | None = Field(
        default=None,
        description="5-field cron expression, or null for manual-only.",
    )
    enabled: bool = Field(default=True)
    timeout_seconds: int = Field(gt=0, default=3600)
    description: str = Field(
        min_length=1, description="Operator-facing: what this job does and why."
    )
    host_only: bool = Field(
        default=False,
        description=(
            "True when the job needs a host-only resource "
            "(docker socket, gh auth, /mnt/data)."
        ),
    )
    run_requested_at: datetime | None = Field(
        default=None,
        description="Set by a manual 'Run now' trigger; cleared once picked up.",
    )

    @field_validator("name", mode="after")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        if not value.strip():
            msg = "name must not be blank"
            raise ValueError(msg)
        return value

    @field_validator("command", mode="after")
    @classmethod
    def _non_blank_argv(cls, value: list[str]) -> list[str]:
        if any(not arg.strip() for arg in value):
            msg = "command entries must not be blank"
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
