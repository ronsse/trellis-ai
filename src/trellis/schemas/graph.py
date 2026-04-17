"""Graph edge schema for Trellis."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from trellis.core.base import TimestampedModel, VersionedModel
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
