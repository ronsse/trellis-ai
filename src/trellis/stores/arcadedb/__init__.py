"""ArcadeDB substrate — graph + vector via Bolt + HTTP.

Trellis's **blessed substrate** for graph + vector workloads (per the
open ADR). ArcadeDB is Apache 2.0, Bolt + openCypher 25 at 97.8% TCK,
ships native HNSW vector indexes (jVector), and runs comfortably on a
single AWS ECS Fargate task with EFS persistence.

The graph store is a thin adapter over
:class:`~trellis.stores.bolt_opencypher.graph.BoltOpenCypherGraphStore`
— the existing Neo4j Cypher payload works against ArcadeDB's openCypher
parser unchanged once the two portable compatibility fixes are in
place (``datetime()`` casts and ``edge_type`` filtering via WHERE
instead of property-pattern binding in bulk UNWIND).

The vector store is its own implementation. ArcadeDB exposes vector
operations only via SQL (not openCypher), so the vector store talks
to ArcadeDB's HTTP ``/api/v1/command`` endpoint rather than the Bolt
session it shares with the graph store. This is still **shape #2**:
embeddings live as a ``LIST OF FLOAT`` property on the same ``:Node``
records the graph store manages, and the vector store's ``item_id`` is
the graph store's ``node_id``.

See :class:`ArcadeDBGraphStore` and :class:`ArcadeDBVectorStore` for
construction + lifecycle details.
"""

from trellis.stores.arcadedb.graph import ArcadeDBGraphStore
from trellis.stores.arcadedb.vector import ArcadeDBVectorStore

__all__ = ["ArcadeDBGraphStore", "ArcadeDBVectorStore"]
