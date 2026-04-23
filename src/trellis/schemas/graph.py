"""Graph edge schema for Trellis."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from trellis.core.base import TimestampedModel, TrellisModel, VersionedModel
from trellis.core.ids import generate_ulid


class Edge(TimestampedModel, VersionedModel):
    """A directed edge in the experience graph.

    ``edge_kind`` is an **open string**. The ``EdgeKind`` enum in
    ``schemas/enums.py`` lists well-known agent-centric values
    (``trace_used_evidence``, ``entity_depends_on``, ...); domain-specific
    integrations pass their own strings (``uc_column_of``,
    ``dbt_references``, ...) and are accepted verbatim by the storage
    layer. Do not add domain-specific kinds to the core enum — define
    them in the consumer package instead.
    """

    edge_id: str = Field(default_factory=generate_ulid)
    source_id: str
    target_id: str
    edge_kind: str
    properties: dict[str, Any] = Field(default_factory=dict)


class CompactionReport(TrellisModel):
    """Result of a :meth:`GraphStore.compact_versions` call.

    Closes Gap 4.2. SCD Type 2 versioning leaves every closed
    (``valid_to IS NOT NULL``) row in place forever; on hot nodes this
    accumulates and degrades ``as_of`` queries. ``compact_versions``
    drops closed rows whose ``valid_to < before`` — current rows
    (``valid_to IS NULL``) are never touched.

    The report is deliberately wide enough to correlate runs with
    downstream storage/latency metrics: per-table counts, the
    ``valid_to`` range of the compacted rows, and an explicit ``dry_run``
    flag so previews and real runs emit the same event shape.
    """

    before: datetime
    """Cutoff — rows with ``valid_to < before`` are eligible."""

    nodes_compacted: int = 0
    edges_compacted: int = 0
    aliases_compacted: int = 0
    oldest_compacted_valid_to: datetime | None = None
    """Earliest ``valid_to`` among compacted rows (``None`` if nothing compacted)."""
    newest_compacted_valid_to: datetime | None = None
    """Latest ``valid_to`` among compacted rows (``None`` if nothing compacted)."""
    dry_run: bool = False
    """When ``True`` no rows were deleted; counts reflect what *would* be dropped."""
    duration_ms: int = 0

    @property
    def total_compacted(self) -> int:
        return self.nodes_compacted + self.edges_compacted + self.aliases_compacted
