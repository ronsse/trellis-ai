"""Synthetic ``Agent`` nodes for Trellis-internal meta-analyses.

Every meta-Activity recorded by :func:`trellis.meta.record_meta_analysis`
must reference a graph ``Agent`` node (the PROV-O entity that "performed"
the activity). Real human / external agents already have nodes from
trace ingestion, but Trellis-internal analyses ("the noise demotion
loop", "the schema-evolution analyzer") have no natural Agent
counterpart in the graph — they are subsystems, not users.

This module owns the synthetic-agent namespace. The default agent ID
is ``"trellis_meta_analyzer"`` per
``docs/design/plan-dogfooding-meta-traces.md`` §0; operators can use
any ID starting with the ``trellis_meta_`` prefix that the rest of the
system already reserves (see
:data:`trellis.learning.schema_evolution.META_EXTRACTOR_PREFIX`).

``ensure_meta_agent`` is the single entry point — idempotent, returns
the node ID. Multiple calls with the same ``agent_id`` resolve to the
existing node; calls with different IDs create distinct agents. The
PackBuilder default filter (Phase 2 work, not this PR) excludes
``trellis_meta_*`` agents from agent-facing context packs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from trellis.schemas import well_known as wk

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

#: Default synthetic-agent ID for meta-traces — used when callers do
#: not supply an explicit ``agent_id``. The ``trellis_meta_`` prefix
#: is reserved by the schema-evolution analyzer
#: (:data:`trellis.learning.schema_evolution.META_EXTRACTOR_PREFIX`)
#: and by the PackBuilder default filter (Phase 2).
DEFAULT_META_AGENT_ID: str = "trellis_meta_analyzer"

#: Reserved namespace prefix for synthetic meta-agents. Operators must
#: not emit user-data nodes with ``agent_id`` matching this prefix
#: (per the ADR; documented in ``docs/agent-guide/schemas.md``).
META_AGENT_PREFIX: str = "trellis_meta_"


def ensure_meta_agent(
    registry: StoreRegistry,
    agent_id: str = DEFAULT_META_AGENT_ID,
) -> str:
    """Return the node ID of the synthetic Agent for ``agent_id``.

    Idempotent: if a node with ``node_id == agent_id`` and
    ``node_type == "Agent"`` already exists, the existing ID is
    returned without re-upserting (avoids creating a new SCD-2 version
    for no reason). Otherwise, the helper creates the node and returns
    its ID.

    The created node carries minimal properties — just ``name`` and a
    ``synthetic=True`` flag — so filters keyed on the ``properties``
    bag can distinguish synthetic meta-agents from real agent nodes
    that happen to share the prefix.

    Args:
        registry: Store registry.
        agent_id: ID for the synthetic agent. Used as both the
            ``node_id`` and the ``name`` property. Must start with
            :data:`META_AGENT_PREFIX` per the ADR's namespace
            reservation — raises :class:`ValueError` otherwise.

    Raises:
        ValueError: If ``agent_id`` does not start with
            :data:`META_AGENT_PREFIX`.

    Returns:
        The node ID (equal to ``agent_id`` on success).
    """
    if not agent_id.startswith(META_AGENT_PREFIX):
        msg = (
            f"ensure_meta_agent: agent_id must start with "
            f"{META_AGENT_PREFIX!r} (the reserved synthetic-agent "
            f"namespace per adr-dogfooding-meta-traces.md §4.4); "
            f"got {agent_id!r}"
        )
        raise ValueError(msg)

    graph_store = registry.knowledge.graph_store
    existing = graph_store.get_node(agent_id)
    if existing is not None:
        if existing["node_type"] != wk.AGENT:
            # POC discipline: refuse to overwrite an existing non-Agent
            # node sitting at this ID — that would silently corrupt the
            # caller's graph. Loud failure.
            msg = (
                f"ensure_meta_agent: node {agent_id!r} exists but has "
                f"node_type={existing['node_type']!r}, expected "
                f"{wk.AGENT!r}. Refusing to overwrite — pick a "
                "different agent_id."
            )
            raise ValueError(msg)
        return agent_id

    logger.debug("meta_agent_created", agent_id=agent_id)
    return graph_store.upsert_node(
        node_id=agent_id,
        node_type=wk.AGENT,
        properties={"name": agent_id, "synthetic": True},
    )


__all__ = [
    "DEFAULT_META_AGENT_ID",
    "META_AGENT_PREFIX",
    "ensure_meta_agent",
]
