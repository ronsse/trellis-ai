"""Backend-agnostic data migration helpers.

Currently exposes :class:`GraphMigrator` for moving graph data
(nodes + edges + aliases) between any two ``GraphStore`` backends.
The migrator only relies on the public ``GraphStore`` API plus the
canonical Phase-2 DSL, so SQLite ↔ Postgres ↔ Neo4j all work
without backend-specific branches.

Scope (POC v1):

* Current versions only — historical SCD-2 versions are not preserved.
  Re-running the migration against a destination that already has the
  current row is a no-op (the upsert finds a matching ``node_id`` and
  skips).
* In-memory snapshot — the source is read in one pass capped by
  ``max_nodes``. Suitable up to tens of thousands of nodes; larger
  exports should wait for a paginated iterator.
* Idempotent on retry by node_id / edge_id / alias_id.

CLI wrapper: ``trellis admin migrate-graph``
(:mod:`trellis_cli.migrate`).
"""

from trellis.migrate.graph_migrator import (
    GraphMigrator,
    MigrationCapacityExceededError,
    MigrationReport,
)

__all__ = [
    "GraphMigrator",
    "MigrationCapacityExceededError",
    "MigrationReport",
]
