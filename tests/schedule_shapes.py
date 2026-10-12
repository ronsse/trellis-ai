"""The degenerate shapes a damaged ``schedule.json`` takes.

A thin specialisation of :mod:`tests.degradable_shapes`, which owns the
table and explains why there is only one (see its own module docstring).
Named like :mod:`tests.policy_shapes` for the same reason: the shape table
is shared, but the CRUD store that must degrade-and-refuse on every shape
is asserted per store, in that store's own test module.
"""

from __future__ import annotations

from tests.degradable_shapes import degenerate_files, degenerate_ids

#: ``(id, file contents, the ``reason`` the CRUD store must record)``.
DEGENERATE_SCHEDULE_FILES: list[tuple[str, str, str]] = degenerate_files("jobs")

#: Parametrisation ids, so a failure names the shape rather than an index.
DEGENERATE_SCHEDULE_IDS: list[str] = degenerate_ids("jobs")
