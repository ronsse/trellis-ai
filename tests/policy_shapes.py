"""The degenerate shapes a damaged ``policies.json`` takes.

A thin specialisation of :mod:`tests.degradable_shapes`, which owns the
table and explains why there is only one. The name is kept because two
suites import it: the CRUD store
(``tests/unit/stores/test_policy_store.py``) must degrade on every shape
and refuse every write, while the enforcement reader
(``tests/unit/mutate/test_policy_wiring.py``) must raise on every one.
"""

from __future__ import annotations

from tests.degradable_shapes import degenerate_files, degenerate_ids

#: ``(id, file contents, the ``reason`` the CRUD store must record)``.
DEGENERATE_POLICY_FILES: list[tuple[str, str, str]] = degenerate_files("policies")

#: Parametrisation ids, so a failure names the shape rather than an index.
DEGENERATE_POLICY_IDS: list[str] = degenerate_ids("policies")
