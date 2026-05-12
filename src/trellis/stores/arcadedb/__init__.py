"""ArcadeDB substrate — graph (Bolt + openCypher) backend.

Trellis's blessed substrate for the Knowledge plane graph store (per
the open ADR). ArcadeDB is Apache 2.0, Bolt + openCypher 25 at 97.8%
TCK, and runs comfortably on a single AWS ECS Fargate task with EFS
persistence.

The graph store is a thin adapter over
:class:`~trellis.stores.bolt_opencypher.graph.BoltOpenCypherGraphStore`
— the existing Neo4j Cypher payload works against ArcadeDB's openCypher
parser unchanged.

(A paired vector store follows in a subsequent commit; embeddings will
land as a ``LIST OF FLOAT`` property on the same ``:Node`` records the
graph store manages — shape #2, same as Neo4j's vector store.)
"""

from trellis.stores.arcadedb.graph import ArcadeDBGraphStore

__all__ = ["ArcadeDBGraphStore"]
