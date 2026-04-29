"""Deterministic synthetic graph generator.

Used by the multi-backend equivalence scenario (5.1) and reused by the
populated-graph performance scenario (5.3). Output is a flat
``GeneratedGraph`` dataclass holding nodes, edges, and (optionally)
embeddings — scenarios consume it and write to whichever backend they
want to compare.

Determinism: every randomness source is a seeded ``random.Random``
instance. Same seed → same output, byte-for-byte. Never use ``random``
top-level helpers here.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# Long-tail node-type distribution: a handful of common types with a
# lower-frequency tail. The exact mix is arbitrary — what matters is that
# it's stable across runs and that no single type dominates.
NODE_TYPES = [
    ("entity", 0.35),
    ("artifact", 0.25),
    ("concept", 0.15),
    ("event", 0.10),
    ("agent", 0.07),
    ("dataset", 0.05),
    ("policy", 0.03),
]

EDGE_TYPES = [
    "references",
    "depends_on",
    "produced_by",
    "derived_from",
    "related_to",
]


@dataclass
class GeneratedNode:
    node_id: str
    node_type: str
    properties: dict[str, str | int | float]
    embedding: list[float] | None = None


@dataclass
class GeneratedEdge:
    source_id: str
    target_id: str
    edge_type: str
    properties: dict[str, str | int | float] = field(default_factory=dict)


@dataclass
class GeneratedGraph:
    """Container the scenarios consume. Lists are deterministic in order."""

    nodes: list[GeneratedNode]
    edges: list[GeneratedEdge]
    seed_ids: list[str]
    """A small set of node ids reserved for use as `get_subgraph` seeds."""


def _weighted_choice(rng: random.Random, choices: list[tuple[str, float]]) -> str:
    r = rng.random()
    cumulative = 0.0
    for name, weight in choices:
        cumulative += weight
        if r < cumulative:
            return name
    return choices[-1][0]


def _unit_vector(rng: random.Random, dim: int) -> list[float]:
    """Random unit vector — keeps cosine similarity well-defined."""
    raw = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def generate_graph(
    *,
    seed: int = 0,
    node_count: int = 1000,
    edge_count: int = 5000,
    embedding_count: int = 200,
    embedding_dim: int = 3,
    seed_node_count: int = 5,
) -> GeneratedGraph:
    """Build a deterministic synthetic graph.

    Defaults match plan §5.1's "mid-size graph" target (~1K nodes / ~5K
    edges / ~200 embeddings). Tests pass smaller values to keep the
    suite fast.

    Args:
        seed: PRNG seed; the generator is fully deterministic given it.
        node_count: number of nodes to emit.
        edge_count: number of edges to emit. Edges are sampled with
            replacement so duplicates are possible — that's intentional;
            backends should de-duplicate via ``upsert_edge`` semantics
            and we want to verify they do.
        embedding_count: how many of the nodes get an embedding. Drawn
            from the front of the node list deterministically.
        embedding_dim: dimensionality of the random embeddings.
        seed_node_count: how many node ids to reserve as subgraph seeds.

    Returns:
        A ``GeneratedGraph``.
    """
    if node_count <= 0:
        msg = "node_count must be positive"
        raise ValueError(msg)
    if seed_node_count > node_count:
        msg = "seed_node_count cannot exceed node_count"
        raise ValueError(msg)
    if embedding_count > node_count:
        msg = "embedding_count cannot exceed node_count"
        raise ValueError(msg)

    rng = random.Random(seed)  # noqa: S311 — synthetic test data, not crypto

    nodes: list[GeneratedNode] = []
    for i in range(node_count):
        node_type = _weighted_choice(rng, NODE_TYPES)
        nodes.append(
            GeneratedNode(
                node_id=f"n{i:06d}",
                node_type=node_type,
                properties={
                    "name": f"{node_type}-{i}",
                    "rank": rng.randint(0, 100),
                    "weight": round(rng.random(), 4),
                },
            )
        )

    # Embeddings on the first `embedding_count` nodes — keeps the
    # vector-search recall test deterministic in which ids it expects.
    for i in range(embedding_count):
        nodes[i].embedding = _unit_vector(rng, embedding_dim)

    edges: list[GeneratedEdge] = []
    for _ in range(edge_count):
        src = nodes[rng.randrange(node_count)].node_id
        tgt = nodes[rng.randrange(node_count)].node_id
        if src == tgt:
            continue  # skip self-loops; the contract test suite covers them separately
        edges.append(
            GeneratedEdge(
                source_id=src,
                target_id=tgt,
                edge_type=rng.choice(EDGE_TYPES),
            )
        )

    seed_ids = [nodes[i].node_id for i in range(seed_node_count)]

    return GeneratedGraph(nodes=nodes, edges=edges, seed_ids=seed_ids)
