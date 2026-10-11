"""Default schedule-registry entries, seeded by ``trellis admin init-schedule``.

One operator-tunable row per :data:`trellis.schedule.catalog.JOB_CATALOG`
entry, carrying only the catalog's own default cadence — no command, no
description, no host-only flag, and no absolute path: those live in the
catalog, keyed by the same ``name``, not in this module. See
:mod:`trellis.schedule.catalog` for the full job list and the
``migrate-graph`` exclusion.
"""

from __future__ import annotations

from trellis.schedule.catalog import JOB_CATALOG
from trellis.schemas.schedule import ScheduledJob


def default_scheduled_jobs() -> list[ScheduledJob]:
    """Build the default registry rows, one per catalog entry.

    Each row's ``cadence`` is the catalog's ``default_cadence`` (``None``
    for a manual-only job); ``enabled``, ``timeout_seconds`` and
    ``run_requested_at`` all keep their schema defaults, leaving every
    operator-tunable field for an operator to set later.
    """
    return [
        ScheduledJob(name=spec.name, cadence=spec.default_cadence)
        for spec in JOB_CATALOG.values()
    ]
