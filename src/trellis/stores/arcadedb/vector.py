"""ArcadeDBVectorStore — HNSW vectors on graph store's :Node rows.

Shape #2: embeddings live as a ``LIST OF FLOAT`` property on the same
``(:Node)`` records that :class:`ArcadeDBGraphStore` manages. The
vector store's ``item_id`` IS the graph store's ``node_id``. Same data
model as :class:`trellis.stores.neo4j.vector.Neo4jVectorStore`.

What differs from the Neo4j vector store: **ArcadeDB exposes vector
operations only via SQL, not openCypher**. The HNSW-style index type is
``LSM_VECTOR`` and is created with SQL DDL; nearest-neighbor queries go
through the ``vectorNeighbors`` SQL function; similarity functions
(``vectorCosineSimilarity`` etc.) are SQL-only. The Bolt session that
the graph store uses can't reach any of these. So this store talks to
ArcadeDB's HTTP REST endpoint (``/api/v1/command/<db>``) for every
operation.

This is **shape #2 with a side-channel**: the graph store writes node
rows via Cypher-over-Bolt, and the vector store reads + writes the
``embedding`` property on those same rows via SQL-over-HTTP. The
underlying records are the same. Both stores see each other's mutations
once the transaction commits, because ArcadeDB serializes both
protocols against the same engine.

**SQL parameter-binding quirk:** when the ``embedding`` property is
declared as ``LIST OF FLOAT``, ArcadeDB rejects parameter-bound vector
values with ``ARRAY_OF_FLOATS`` type-mismatch. So the SET side
(``upsert``, ``upsert_bulk``) inlines the vector as a SQL list literal
(``[0.1, 0.2, 0.3]``) — safe because every element is a float. The
read side (``query``) uses parameter binding for the query vector
because that path doesn't trigger the LIST-vs-ARRAY check.

The class is intentionally **standalone** rather than a subclass of
:class:`BoltOpenCypherGraphStore`: vector ops are SQL-via-HTTP, not
Cypher-via-Bolt, so there's no shared payload to reuse from the Bolt
base class.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from trellis.stores.arcadedb.base import execute_sql
from trellis.stores.base.vector import VectorStore, format_vector_literal

logger = structlog.get_logger(__name__)


_VALID_SIMILARITY = frozenset({"cosine", "euclidean"})
_OVER_FETCH_MULT = 10


class ArcadeDBVectorStore(VectorStore):
    """Vectors as ``LIST OF FLOAT`` properties on the graph store's ``:Node`` rows.

    Schema init declares the ``Node.embedding`` and
    ``Node.vector_metadata_json`` properties (idempotent — re-running
    ``CREATE PROPERTY ... IF NOT EXISTS`` is a no-op), then creates an
    ``LSM_VECTOR`` index over ``embedding`` with the chosen
    dimensions/similarity/HNSW parameters.

    Construction does not take a Bolt driver — the vector store talks
    SQL, not Cypher. It does take the same ``http_url`` /
    ``user`` / ``password`` / ``database`` tuple that
    :class:`ArcadeDBGraphStore` uses on its HTTP admin path, so the
    registry can wire them together via a shared
    ``arcadedb-{http_url}-{user}-{database}`` cache.
    """

    def __init__(
        self,
        *,
        http_url: str,
        user: str = "root",
        password: str,
        database: str = "trellis",
        dimensions: int = 1536,
        similarity: str = "cosine",
        index_name: str = "trellis_node_embeddings",
        max_connections: int = 16,
        beam_width: int = 100,
    ) -> None:
        if similarity not in _VALID_SIMILARITY:
            msg = (
                f"similarity must be one of {sorted(_VALID_SIMILARITY)}, "
                f"got {similarity!r}"
            )
            raise ValueError(msg)
        if dimensions <= 0:
            msg = f"dimensions must be > 0, got {dimensions}"
            raise ValueError(msg)
        if max_connections <= 0:
            msg = f"max_connections must be > 0, got {max_connections}"
            raise ValueError(msg)
        if beam_width <= 0:
            msg = f"beam_width must be > 0, got {beam_width}"
            raise ValueError(msg)

        self._http_url = http_url.rstrip("/")
        self._user = user
        self._password = password
        self._database = database
        self._dimensions = dimensions
        self._similarity = similarity
        self._index = index_name
        self._max_connections = max_connections
        self._beam_width = beam_width

        self._init_schema()
        logger.info(
            "arcadedb_vector_store_initialized",
            dimensions=dimensions,
            similarity=similarity,
            index_name=index_name,
            max_connections=max_connections,
            beam_width=beam_width,
        )

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _sql(
        self,
        command: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a SQL command, optionally with parameter binding."""
        return execute_sql(
            self._http_url,
            self._user,
            self._password,
            self._database,
            command,
            params=params,
        )

    def _init_schema(self) -> None:
        # The graph store creates :Node rows via Cypher (which auto-
        # creates the vertex type), but the LSM_VECTOR index requires
        # the property explicitly declared as ``LIST OF FLOAT``.
        self._sql("CREATE VERTEX TYPE Node IF NOT EXISTS")
        self._sql(
            "CREATE PROPERTY Node.embedding IF NOT EXISTS LIST OF FLOAT"
        )
        self._sql(
            "CREATE PROPERTY Node.vector_metadata_json IF NOT EXISTS STRING"
        )
        metadata = json.dumps(
            {
                "dimensions": self._dimensions,
                "similarity": self._similarity,
                "maxConnections": self._max_connections,
                "beamWidth": self._beam_width,
            }
        )
        self._sql(
            f"CREATE INDEX {self._index} IF NOT EXISTS ON Node(embedding) "
            f"LSM_VECTOR METADATA {metadata}"
        )

    # ------------------------------------------------------------------
    # Upsert
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
        # The ``embedding`` SET inlines the vector literal — ArcadeDB
        # rejects parameter-bound lists against a declared LIST OF
        # FLOAT property (see module docstring). Metadata is bound
        # normally because it's a plain string.
        vec_literal = format_vector_literal(vector, separator=", ")
        rows = self._sql(
            f"UPDATE Node SET embedding = {vec_literal}, "
            f"vector_metadata_json = :meta "
            f"WHERE node_id = :item_id AND valid_to IS NULL",
            params={"meta": json.dumps(metadata or {}), "item_id": item_id},
        )
        count = int(rows[0]["count"]) if rows and isinstance(rows[0], dict) else 0
        if count == 0:
            msg = (
                f"Cannot attach vector: node {item_id!r} has no current "
                "version. Create the node via GraphStore.upsert_node first."
            )
            raise ValueError(msg)

    def upsert_bulk(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        # Pass 1 — Python-side validation. ArcadeDB doesn't have a
        # batch UPDATE primitive that takes per-row variable values
        # via parameters (the SET side rejects parameter-bound vector
        # values), so the bulk path issues one UPDATE per row. Still
        # cheaper than per-call HTTP overhead for small batches; for
        # large batches a Bolt-side multi-statement transaction or
        # the ArcadeDB ``bulkInsert`` REST endpoint would be the next
        # optimization.
        self._validate_bulk_required_keys(items, ("item_id", "vector"), "upsert_bulk")
        for i, spec in enumerate(items):
            vector = spec["vector"]
            if len(vector) != self._dimensions:
                msg = (
                    f"upsert_bulk[{i}]: vector has {len(vector)} dimensions "
                    f"but store was configured for {self._dimensions}"
                )
                raise ValueError(msg)
        self._pre_validate_bulk_item_ids(items)

        # Pass 2 — issue per-row UPDATEs. Collect the first missing-node
        # index so we can raise the same error :meth:`upsert` would.
        for i, spec in enumerate(items):
            try:
                self.upsert(
                    spec["item_id"],
                    spec["vector"],
                    metadata=spec.get("metadata"),
                )
            except ValueError as exc:
                msg = f"upsert_bulk[{i}]: {exc}"
                raise ValueError(msg) from exc
        logger.debug("vectors_upserted_bulk", count=len(items))

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

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
        # Over-fetch to compensate for filters applied client-side and
        # for historical versions the index can return (culled below
        # via ``valid_to IS NULL``).
        fetch_k = top_k * _OVER_FETCH_MULT if filters else top_k * 2
        rows = self._sql(
            "SELECT node_id, vector_metadata_json, distance, valid_to "
            "FROM (SELECT expand(vectorNeighbors("
            "'Node[embedding]', :vec, :k))) "
            "WHERE valid_to IS NULL",
            params={"vec": list(vector), "k": fetch_k},
        )

        results: list[dict[str, Any]] = []
        for row in rows:
            meta_raw = row.get("vector_metadata_json")
            meta = json.loads(meta_raw) if meta_raw else {}
            if filters and not all(meta.get(k) == v for k, v in filters.items()):
                continue
            # ArcadeDB returns a distance. Convert to a similarity-style
            # score so callers can sort descending across all backends.
            # cosine distance ∈ [0, 2]; similarity = 1 - distance.
            # euclidean distance ∈ [0, ∞); we negate so closer == higher.
            distance = float(row["distance"])
            score = (
                1.0 - distance if self._similarity == "cosine" else -distance
            )
            results.append(
                {
                    "item_id": row["node_id"],
                    "score": score,
                    "metadata": meta,
                }
            )
            if len(results) >= top_k:
                break
        return results

    # ------------------------------------------------------------------
    # Get / delete / count
    # ------------------------------------------------------------------

    def get(self, item_id: str) -> dict[str, Any] | None:
        rows = self._sql(
            "SELECT embedding, vector_metadata_json FROM Node "
            "WHERE node_id = :item_id "
            "AND valid_to IS NULL AND embedding IS NOT NULL",
            params={"item_id": item_id},
        )
        if not rows:
            return None
        row = rows[0]
        vec_raw = row.get("embedding")
        if vec_raw is None:
            return None
        meta_raw = row.get("vector_metadata_json")
        meta = json.loads(meta_raw) if meta_raw else {}
        vec = list(vec_raw)
        return {
            "item_id": item_id,
            "vector": vec,
            "dimensions": len(vec),
            "metadata": meta,
        }

    def delete(self, item_id: str) -> bool:
        # Like the Neo4j vector store: clear the embedding (and metadata)
        # on the current version; the node itself stays. Historical
        # versions retain their embeddings (queryable via
        # ``GraphStore.get_node_history`` but excluded from vector search
        # by the ``valid_to IS NULL`` filter).
        #
        # ArcadeDB's UPDATE only counts rows whose values actually
        # changed, so requiring ``embedding IS NOT NULL`` in the WHERE
        # makes ``count > 0`` an accurate "existed" signal in a single
        # round trip — no separate SELECT needed.
        rows = self._sql(
            "UPDATE Node SET embedding = null, vector_metadata_json = null "
            "WHERE node_id = :item_id "
            "AND valid_to IS NULL AND embedding IS NOT NULL",
            params={"item_id": item_id},
        )
        count = int(rows[0]["count"]) if rows and isinstance(rows[0], dict) else 0
        return count > 0

    def count(self) -> int:
        rows = self._sql(
            "SELECT count(*) AS cnt FROM Node "
            "WHERE embedding IS NOT NULL AND valid_to IS NULL"
        )
        return int(rows[0]["cnt"]) if rows else 0

    def close(self) -> None:
        # No persistent resources — every SQL request opens its own
        # HTTP connection via urllib. Provided for ABC compatibility.
        logger.debug("arcadedb_vector_store_closed")
