"""Two different traces under one ``trace_id``, for the trace-ingest surfaces.

The handler answers a ``trace_id`` the store already holds as a success
that stores nothing, because traces are immutable. Nothing the second
trace carries may then reach the graph: extracting it would attach its
agent and artifact to the stored trace's ``trace:<id>`` node. The REST,
CLI and MCP suites each submit ``synthetic_trace(X, "first")`` and then
``synthetic_trace(X, "second")``, and compare :func:`graph_state` across
the second call.
"""

from __future__ import annotations

import json
from typing import Any

from trellis.stores.base import GraphStore


def synthetic_trace(trace_id: str, label: str) -> dict[str, Any]:
    """A trace whose intent, agent and artifact are all named by ``label``.

    Two labels give two traces that share no extracted node but the
    trace's own, so a node or edge from the second is unambiguous.
    """
    return {
        "trace_id": trace_id,
        "source": "agent",
        "intent": f"synthetic intent {label}",
        "artifacts_produced": [
            {"artifact_id": f"syn-art-{label}", "artifact_type": "file"}
        ],
        "context": {"agent_id": f"syn-agent-{label}"},
    }


def graph_state(graph: GraphStore, trace_id: str) -> tuple[Any, ...]:
    """Node and edge counts, plus the trace node and every edge touching it.

    Properties are compared too, so a re-extraction that rewrote the trace
    node or one of its edges in place shows up even when no count moves.
    """
    node_id = f"trace:{trace_id}"
    node = graph.get_node(node_id)
    edges = sorted(
        (
            edge["source_id"],
            edge["edge_type"],
            edge["target_id"],
            json.dumps(edge.get("properties", {}), sort_keys=True, default=str),
        )
        for edge in graph.get_edges(node_id, direction="both")
    )
    return (
        graph.count_nodes(),
        graph.count_edges(),
        None if node is None else node.get("properties"),
        edges,
    )
