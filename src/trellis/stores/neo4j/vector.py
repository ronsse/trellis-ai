"""Neo4jVectorStore — Neo4j 5.11+ HNSW vectors as optional properties on :Node.

Shape #2 of the design discussion: vectors live as an ``embedding``
property directly on the ``(:Node)`` rows that the
:class:`Neo4jGraphStore` already manages, rather than on a parallel
``(:VectorItem)`` label. This means:

* The vector store's ``item_id`` IS the graph store's ``node_id``.
* Embeddings are genuinely optional — Neo4j's HNSW index only indexes
  nodes that have the property set, so the structural majority of
  nodes pay zero index cost.
* Queries can filter by graph attributes natively (``valid_to IS NULL``
  to exclude historical versions, ``node_type IN [...]`` to scope a
  search) in the same Cypher pass.
* :meth:`upsert` requires the node to already have a current version —
  embeddings attach to existing entities, they don't create them.

Trade-offs vs. shape #1 (separate ``:VectorItem`` label):

* Couples the two stores on Neo4j (other backends keep their split).
* Updating a node via ``GraphStore.upsert_node`` creates a new version
  row that does NOT inherit the prior embedding. Callers that update
  content must re-embed afterwards. The old (closed) version still
  carries its embedding for audit / time-travel reads, but the
  ``query`` path filters them out via ``valid_to IS NULL``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog

from trellis.stores.base.vector import VectorStore
from trellis.stores.neo4j.base import (
    Neo4jSessionRunner,
    build_driver,
    check_driver_installed,
)

if TYPE_CHECKING:
    from neo4j import Driver

logger = structlog.get_logger(__name__)

_VALID_SIMILARITY = frozenset({"cosine", "euclidean"})
_OVER_FETCH_MULT = 10


class Neo4jVectorStore(Neo4jSessionRunner, VectorStore):
    """Vectors as optional properties on the graph store's ``:Node`` rows.

    See module docstring for the data model. The HNSW index is created
    on ``(:Node).embedding`` and indexes only the nodes that have the
    property set.
    """

    def __init__(
        self,
        uri: str,
        *,
        user: str = "neo4j",
        password: str,
        database: str = "neo4j",
        dimensions: int = 1536,
        similarity: str = "cosine",
        index_name: str = "trellis_node_embeddings",
    ) -> None:
        check_driver_installed()
        if similarity not in _VALID_SIMILARITY:
            msg = (
                f"similarity must be one of {sorted(_VALID_SIMILARITY)}, "
                f"got {similarity!r}"
            )
            raise ValueError(msg)
        if dimensions <= 0:
            msg = f"dimensions must be > 0, got {dimensions}"
            raise ValueError(msg)

        self._driver: Driver = build_driver(uri, user, password)
        self._database = database
        self._dimensions = dimensions
        self._similarity = similarity
        self._index = index_name
        self._init_schema()
        logger.info(
            "neo4j_vector_store_initialized",
            dimensions=dimensions,
            similarity=similarity,
            index_name=index_name,
        )

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        # The VECTOR INDEX DDL doesn't accept bound parameters for the
        # index name or options map — they must be Cypher literals.
        # All three inputs are validated in ``__init__`` so inlining
        # is safe.
        index = (
            f"CREATE VECTOR INDEX {self._index} IF NOT EXISTS "
            "FOR (n:Node) ON n.embedding "
            "OPTIONS {indexConfig: {"
            f"  `vector.dimensions`: {self._dimensions}, "
            f"  `vector.similarity_function`: '{self._similarity}'"
            "}}"
        )
        with self._driver.session(database=self._database) as session:
            session.run(index)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert(
        self,
        item_id: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if len(vector) != self._dimensions:
            msg = (
                f"vector has {len(vector)} dimensions but store was "
                f"configured for {self._dimensions}"
            )
            raise ValueError(msg)
        meta_json = json.dumps(metadata or {})
        # Embedding attaches to the CURRENT (valid_to IS NULL) version
        # of the node. No node ⇒ caller mistake; raise.
        cypher = (
            "MATCH (n:Node {node_id: $item_id}) WHERE n.valid_to IS NULL "
            "SET n.embedding = $vector, n.vector_metadata_json = $meta_json "
            "RETURN n.node_id AS node_id"
        )
        record = self._run_write_single(
            cypher,
            item_id=item_id,
            vector=vector,
            meta_json=meta_json,
        )
        if record is None:
            msg = (
                f"Cannot attach vector: node {item_id!r} has no current "
                "version. Create the node via GraphStore.upsert_node first."
            )
            raise ValueError(msg)

    def upsert_bulk(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return

        # Pass 1 — Python-side validation. Catches dimension mismatches
        # and missing keys before any I/O so a bad row in the middle of
        # a 1K-vector batch doesn't leave half written.
        rows: list[dict[str, Any]] = []
        for i, spec in enumerate(items):
            for key in ("item_id", "vector"):
                if key not in spec or spec[key] is None:
                    msg = f"upsert_bulk[{i}]: missing required key {key!r}"
                    raise ValueError(msg)
            vector = spec["vector"]
            if len(vector) != self._dimensions:
                msg = (
                    f"upsert_bulk[{i}]: vector has {len(vector)} dimensions "
                    f"but store was configured for {self._dimensions}"
                )
                raise ValueError(msg)
            rows.append(
                {
                    "item_id": spec["item_id"],
                    "vector": vector,
                    "meta_json": json.dumps(spec.get("metadata") or {}),
                }
            )

        # One round trip — UNWIND attaches every row to its current
        # node. Rows whose node has no current version (or doesn't
        # exist) are silently dropped by the MATCH; we count returned
        # rows after to detect and raise on missing endpoints. Mirrors
        # the single-row :meth:`upsert` error message.
        cypher = """
        UNWIND $rows AS row
        MATCH (n:Node {node_id: row.item_id}) WHERE n.valid_to IS NULL
        SET n.embedding = row.vector,
            n.vector_metadata_json = row.meta_json
        RETURN n.node_id AS node_id
        """
        with self._driver.session(database=self._database) as session:
            records = session.execute_write(
                lambda tx: list(tx.run(cypher, rows=rows))
            )

        if len(records) != len(rows):
            written = {r["node_id"] for r in records}
            for i, spec in enumerate(items):
                if spec["item_id"] not in written:
                    msg = (
                        f"upsert_bulk[{i}]: cannot attach vector — node "
                        f"{spec['item_id']!r} has no current version. "
                        "Create the node via GraphStore.upsert_node first."
                    )
                    raise ValueError(msg)

        logger.debug("vectors_upserted_bulk", count=len(rows))

    def query(
        self,
        vector: list[float],
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if len(vector) != self._dimensions:
            msg = (
                f"query vector has {len(vector)} dimensions but store was "
                f"configured for {self._dimensions}"
            )
            raise ValueError(msg)
        # Over-fetch when filters are present (applied client-side after
        # JSON decode) and to compensate for any closed historical
        # versions the index might still return — query filters those
        # out via valid_to IS NULL.
        fetch_k = top_k * _OVER_FETCH_MULT if filters else top_k * 2
        cypher = (
            "CALL db.index.vector.queryNodes($index, $k, $vector) "
            "YIELD node, score "
            "WHERE node.valid_to IS NULL "
            "RETURN node.node_id AS item_id, score, "
            "       node.vector_metadata_json AS metadata_json"
        )
        records = self._run_read_list(
            cypher, index=self._index, k=fetch_k, vector=vector
        )

        results: list[dict[str, Any]] = []
        for r in records:
            meta = json.loads(r["metadata_json"] or "{}")
            if filters and not all(meta.get(k) == v for k, v in filters.items()):
                continue
            results.append(
                {
                    "item_id": r["item_id"],
                    "score": float(r["score"]),
                    "metadata": meta,
                }
            )
            if len(results) >= top_k:
                break
        return results

    def get(self, item_id: str) -> dict[str, Any] | None:
        cypher = (
            "MATCH (n:Node {node_id: $item_id}) "
            "WHERE n.valid_to IS NULL AND n.embedding IS NOT NULL "
            "RETURN n.embedding AS embedding, "
            "       n.vector_metadata_json AS metadata_json"
        )
        record = self._run_read_single(cypher, item_id=item_id)
        if record is None:
            return None
        vec = list(record["embedding"])
        meta = json.loads(record["metadata_json"] or "{}")
        return {
            "item_id": item_id,
            "vector": vec,
            "dimensions": len(vec),
            "metadata": meta,
        }

    def delete(self, item_id: str) -> bool:
        # Removes the embedding from the current version only — the
        # node itself stays. Historical versions retain their
        # embeddings (queryable via ``GraphStore.get_node_history``
        # but excluded from vector search by the ``valid_to IS NULL``
        # filter).
        def _tx(tx: Any) -> bool:
            record = tx.run(
                "MATCH (n:Node {node_id: $id}) "
                "WHERE n.valid_to IS NULL AND n.embedding IS NOT NULL "
                "RETURN count(n) AS cnt",
                id=item_id,
            ).single()
            existed = bool(record and record["cnt"] > 0)
            tx.run(
                "MATCH (n:Node {node_id: $id}) WHERE n.valid_to IS NULL "
                "REMOVE n.embedding, n.vector_metadata_json",
                id=item_id,
            ).consume()
            return existed

        with self._driver.session(database=self._database) as session:
            return bool(session.execute_write(_tx))

    def count(self) -> int:
        cypher = (
            "MATCH (n:Node) "
            "WHERE n.valid_to IS NULL AND n.embedding IS NOT NULL "
            "RETURN count(n) AS cnt"
        )
        record = self._run_read_single(cypher)
        return int(record["cnt"]) if record else 0

    def close(self) -> None:
        self._driver.close()
        logger.info("neo4j_vector_store_closed")
