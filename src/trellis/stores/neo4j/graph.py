"""Neo4jGraphStore — Neo4j-backed graph store with SCD Type 2 versioning.

Every version is stored as its own ``(:Node)`` row carrying
``node_id``, ``version_id``, ``valid_from``, ``valid_to``, and the
property payload. Edges use native Neo4j relationships of type
``:EDGE`` with their own versioning properties. This is a direct
port of the Postgres schema — timestamps and JSON-shaped fields are
serialized to strings to sidestep Neo4j's rule forbidding nested
maps on properties.

Note: Neo4j Community Edition does not support partial uniqueness
constraints (``UNIQUE ... WHERE valid_to IS NULL``). The "at most
one current version per node_id" invariant is enforced by the
close-then-insert transaction rather than by the database. Under
concurrent writers on Community, a second writer can observe a
stale "no current" state and create a duplicate current row.
Enterprise/Aura users can layer a node key constraint on top.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.base import utc_now
from trellis.core.ids import generate_ulid
from trellis.schemas.graph import CompactionReport
from trellis.stores.base._bulk_validation import validate_bulk_required_keys
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.base.graph import (
    GraphStore,
    check_node_role_immutable,
    validate_document_ids,
    validate_node_role_args,
)
from trellis.stores.neo4j.base import (
    Neo4jSessionRunner,
    build_driver,
    check_driver_installed,
)

if TYPE_CHECKING:
    from neo4j import Driver, ManagedTransaction

logger = structlog.get_logger(__name__)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _temporal_where(as_of: datetime | None, var: str) -> tuple[str, dict[str, Any]]:
    """Return a WHERE fragment and its params for SCD-2 temporal filtering."""
    if as_of is None:
        return f"{var}.valid_to IS NULL", {}
    return (
        f"{var}.valid_from <= $as_of "
        f"AND ({var}.valid_to IS NULL OR {var}.valid_to > $as_of)",
        {"as_of": as_of.isoformat()},
    )


def _node_props_to_dict(props: dict[str, Any]) -> dict[str, Any]:
    """Convert a Neo4j :Node's raw properties into the GraphStore dict shape.

    ``properties_json``, ``generation_spec_json``, and ``document_ids_json``
    are stored as JSON strings because Neo4j forbids nested-map values on
    properties. Each read pays a ``json.loads`` per nested field; each
    write pays a ``json.dumps``. Cheap individually but worth being aware
    of on hot paths — changing the schema is not worth it.
    """
    return {
        "node_id": props["node_id"],
        "node_type": props["node_type"],
        "node_role": props.get("node_role", "semantic"),
        "generation_spec": json.loads(props["generation_spec_json"])
        if props.get("generation_spec_json")
        else None,
        "document_ids": json.loads(props["document_ids_json"])
        if props.get("document_ids_json")
        else [],
        "properties": json.loads(props.get("properties_json", "{}")),
        "created_at": props.get("created_at"),
        "updated_at": props.get("updated_at"),
        "valid_from": props.get("valid_from"),
        "valid_to": props.get("valid_to"),
    }


def _edge_props_to_dict(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "edge_id": props["edge_id"],
        "source_id": props["source_id"],
        "target_id": props["target_id"],
        "edge_type": props["edge_type"],
        "properties": json.loads(props.get("properties_json", "{}")),
        "created_at": props.get("created_at"),
        "valid_from": props.get("valid_from"),
        "valid_to": props.get("valid_to"),
    }


def _alias_props_to_dict(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "alias_id": props["alias_id"],
        "entity_id": props["entity_id"],
        "source_system": props["source_system"],
        "raw_id": props["raw_id"],
        "raw_name": props.get("raw_name"),
        "match_confidence": props.get("match_confidence", 1.0),
        "is_primary": bool(props.get("is_primary", False)),
        "created_at": props.get("created_at"),
        "updated_at": props.get("updated_at"),
        "valid_from": props.get("valid_from"),
        "valid_to": props.get("valid_to"),
    }


_SCHEMA_STATEMENTS = (
    # Uniqueness on version_id (available on Community + Enterprise)
    "CREATE CONSTRAINT node_version_unique IF NOT EXISTS "
    "FOR (n:Node) REQUIRE n.version_id IS UNIQUE",
    "CREATE CONSTRAINT alias_version_unique IF NOT EXISTS "
    "FOR (a:Alias) REQUIRE a.version_id IS UNIQUE",
    # Lookup indexes
    "CREATE INDEX node_id_idx IF NOT EXISTS FOR (n:Node) ON (n.node_id)",
    "CREATE INDEX node_type_idx IF NOT EXISTS FOR (n:Node) ON (n.node_type)",
    "CREATE INDEX node_role_idx IF NOT EXISTS FOR (n:Node) ON (n.node_role)",
    "CREATE INDEX node_valid_idx IF NOT EXISTS "
    "FOR (n:Node) ON (n.valid_from, n.valid_to)",
    "CREATE INDEX alias_entity_idx IF NOT EXISTS FOR (a:Alias) ON (a.entity_id)",
    "CREATE INDEX alias_lookup_idx IF NOT EXISTS "
    "FOR (a:Alias) ON (a.source_system, a.raw_id)",
    "CREATE INDEX edge_id_idx IF NOT EXISTS FOR ()-[r:EDGE]-() ON (r.edge_id)",
    "CREATE INDEX edge_type_idx IF NOT EXISTS FOR ()-[r:EDGE]-() ON (r.edge_type)",
    "CREATE INDEX edge_valid_idx IF NOT EXISTS "
    "FOR ()-[r:EDGE]-() ON (r.valid_from, r.valid_to)",
)


class Neo4jGraphStore(Neo4jSessionRunner, GraphStore):
    """Neo4j-backed graph store. See module docstring for the data model."""

    def __init__(
        self,
        uri: str,
        *,
        user: str = "neo4j",
        password: str,
        database: str = "neo4j",
    ) -> None:
        check_driver_installed()
        self._driver: Driver = build_driver(uri, user, password)
        self._database = database
        self._init_schema()
        logger.info("neo4j_graph_store_initialized", uri=uri, database=database)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._driver.session(database=self._database) as session:
            for stmt in _SCHEMA_STATEMENTS:
                session.run(stmt)

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def upsert_node(
        self,
        node_id: str | None,
        node_type: str,
        properties: dict[str, Any],
        *,
        node_role: str = "semantic",
        generation_spec: dict[str, Any] | None = None,
        document_ids: list[str] | None = None,
        commit: bool = True,  # noqa: ARG002
    ) -> str:
        validate_node_role_args(node_role, generation_spec)
        validate_document_ids(document_ids)

        if node_id is None:
            node_id = generate_ulid()

        # One Cypher round trip: ``OPTIONAL MATCH`` finds an existing
        # current row (if any), the WHERE filters out role-immutability
        # conflicts so the CREATE only runs when the write is legal,
        # ``coalesce`` carries ``created_at`` forward across versions.
        # Previously this path made a separate ``get_node`` call to
        # fetch the prior version — measured at ~50% of total upsert
        # latency on AuraDB Free.
        now = _iso(utc_now())
        new_props = {
            "node_id": node_id,
            "version_id": generate_ulid(),
            "node_type": node_type,
            "node_role": node_role,
            "generation_spec_json": json.dumps(generation_spec)
            if generation_spec is not None
            else None,
            "document_ids_json": json.dumps(document_ids)
            if document_ids is not None
            else None,
            "properties_json": json.dumps(properties or {}),
            # ``created_at`` is set in Cypher via coalesce so we don't
            # need to know the prior value here.
            "updated_at": now,
            "valid_from": now,
            "valid_to": None,
        }

        cypher = """
        OPTIONAL MATCH (old:Node {node_id: $node_id}) WHERE old.valid_to IS NULL
        WITH old, old.node_role AS prior_role
        WHERE prior_role IS NULL OR prior_role = $node_role
        SET old.valid_to = $now
        WITH old, coalesce(old.created_at, $now) AS created_at_carry
        CREATE (n:Node)
        SET n = $new_props
        SET n.created_at = created_at_carry
        RETURN n.node_id AS node_id
        """
        record = self._run_write_single(
            cypher,
            node_id=node_id,
            now=now,
            node_role=node_role,
            new_props=new_props,
        )
        if record is None:
            # WHERE filtered the write — almost certainly a role-
            # immutability conflict. Re-fetch the prior version and
            # raise the same error the per-row pre-check used to. One
            # extra round trip on the rare error path; happy path
            # stays at one.
            existing = self.get_node(node_id)
            if existing is not None:
                check_node_role_immutable(node_id, existing, node_role)
            msg = (
                f"Cannot upsert node {node_id!r}: write was rejected "
                "but no prior version was found. The node may have "
                "been deleted concurrently."
            )
            raise ValueError(msg)
        return node_id

    def upsert_nodes_bulk(
        self,
        nodes: list[dict[str, Any]],
    ) -> list[str]:
        if not nodes:
            return []

        # Pass 1 — Python-side validation + ID assignment. Done before
        # any I/O so a bad row in the middle of a 5K-node load doesn't
        # leave half the batch written.
        node_ids: list[str] = []
        for i, spec in enumerate(nodes):
            try:
                node_role = spec.get("node_role", "semantic")
                generation_spec = spec.get("generation_spec")
                document_ids = spec.get("document_ids")
                validate_node_role_args(node_role, generation_spec)
                validate_document_ids(document_ids)
            except (ValueError, TypeError) as exc:
                msg = f"upsert_nodes_bulk[{i}]: {exc}"
                raise type(exc)(msg) from exc
            node_ids.append(spec.get("node_id") or generate_ulid())

        # Round trip 1 — fetch existing current rows so we can carry
        # ``created_at`` forward and enforce role immutability per row.
        existing = self._fetch_current_nodes_bulk(node_ids)

        # Validate role immutability now that we know which inputs hit
        # an existing row. Same error contract as ``upsert_node``.
        for i, (spec, nid) in enumerate(zip(nodes, node_ids, strict=True)):
            prior = existing.get(nid)
            if prior is None:
                continue
            try:
                check_node_role_immutable(nid, prior, spec.get("node_role", "semantic"))
            except ValueError as exc:
                msg = f"upsert_nodes_bulk[{i}]: {exc}"
                raise ValueError(msg) from exc

        # Build the row payloads (mirrors ``upsert_node``'s ``new_props``).
        now = _iso(utc_now())
        rows: list[dict[str, Any]] = []
        for spec, nid in zip(nodes, node_ids, strict=True):
            prior = existing.get(nid)
            created_at = prior["created_at"] if prior is not None else now
            generation_spec = spec.get("generation_spec")
            document_ids = spec.get("document_ids")
            rows.append(
                {
                    "node_id": nid,
                    "props": {
                        "node_id": nid,
                        "version_id": generate_ulid(),
                        "node_type": spec["node_type"],
                        "node_role": spec.get("node_role", "semantic"),
                        "generation_spec_json": (
                            json.dumps(generation_spec)
                            if generation_spec is not None
                            else None
                        ),
                        "document_ids_json": (
                            json.dumps(document_ids)
                            if document_ids is not None
                            else None
                        ),
                        "properties_json": json.dumps(spec.get("properties") or {}),
                        "created_at": created_at,
                        "updated_at": now,
                        "valid_from": now,
                        "valid_to": None,
                    },
                }
            )

        # Round trip 2 — close existing current rows and create new
        # versions in a single UNWIND. The ``OPTIONAL MATCH old`` runs
        # per row; the ``WITH row`` carries the row through after the
        # close so the ``CREATE`` happens regardless of whether a prior
        # version existed.
        cypher = """
        UNWIND $rows AS row
        OPTIONAL MATCH (old:Node {node_id: row.node_id})
          WHERE old.valid_to IS NULL
        SET old.valid_to = row.props.valid_from
        WITH row
        CREATE (n:Node)
        SET n = row.props
        """
        with self._driver.session(database=self._database) as session:
            session.execute_write(lambda tx: tx.run(cypher, rows=rows).consume())

        logger.debug("nodes_upserted_bulk", count=len(node_ids))
        return node_ids

    def _fetch_current_nodes_bulk(
        self, node_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Single round trip: load all current rows for ``node_ids``.

        Returns a ``{node_id: row_dict}`` map for the subset that
        currently exists. Missing IDs are absent from the map.
        """
        if not node_ids:
            return {}
        cypher = (
            "MATCH (n:Node) WHERE n.node_id IN $ids AND n.valid_to IS NULL RETURN n"
        )
        with self._driver.session(database=self._database) as session:
            records = session.execute_read(
                lambda tx: list(tx.run(cypher, ids=node_ids))
            )
        out: dict[str, dict[str, Any]] = {}
        for r in records:
            row = _node_props_to_dict(dict(r["n"]))
            out[row["node_id"]] = row
        return out

    def get_node(
        self,
        node_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        where, params = _temporal_where(as_of, "n")
        cypher = f"MATCH (n:Node {{node_id: $node_id}}) WHERE {where} RETURN n"
        record = self._run_read_single(cypher, node_id=node_id, **params)
        if record is None:
            return None
        return _node_props_to_dict(dict(record["n"]))

    def get_nodes_bulk(
        self,
        node_ids: list[str],
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        where, params = _temporal_where(as_of, "n")
        cypher = f"MATCH (n:Node) WHERE n.node_id IN $ids AND {where} RETURN n"
        records = self._run_read_list(cypher, ids=node_ids, **params)
        return [_node_props_to_dict(dict(r["n"])) for r in records]

    def get_node_history(self, node_id: str) -> list[dict[str, Any]]:
        cypher = (
            "MATCH (n:Node {node_id: $node_id}) RETURN n ORDER BY n.valid_from DESC"
        )
        records = self._run_read_list(cypher, node_id=node_id)
        return [_node_props_to_dict(dict(r["n"])) for r in records]

    # ------------------------------------------------------------------
    # Aliases
    # ------------------------------------------------------------------

    def upsert_alias(
        self,
        entity_id: str,
        source_system: str,
        raw_id: str,
        *,
        raw_name: str | None = None,
        match_confidence: float = 1.0,
        is_primary: bool = False,
    ) -> str:
        existing = self.resolve_alias(source_system, raw_id)
        now = _iso(utc_now())
        if existing:
            alias_id = str(existing["alias_id"])
            created_at = existing["created_at"]
        else:
            alias_id = generate_ulid()
            created_at = now

        version_id = generate_ulid()
        new_props = {
            "alias_id": alias_id,
            "version_id": version_id,
            "entity_id": entity_id,
            "source_system": source_system,
            "raw_id": raw_id,
            "raw_name": raw_name,
            "match_confidence": match_confidence,
            "is_primary": is_primary,
            "created_at": created_at,
            "updated_at": now,
            "valid_from": now,
            "valid_to": None,
        }

        cypher = """
        OPTIONAL MATCH (old:Alias {alias_id: $alias_id})
          WHERE old.valid_to IS NULL
        SET old.valid_to = $now
        WITH count(old) AS _closed
        CREATE (a:Alias)
        SET a = $new_props
        RETURN a.alias_id AS alias_id
        """
        self._run_write(cypher, alias_id=alias_id, now=now, new_props=new_props)
        logger.debug(
            "alias_upserted",
            alias_id=alias_id,
            entity_id=entity_id,
            source_system=source_system,
            raw_id=raw_id,
        )
        return alias_id

    def resolve_alias(
        self,
        source_system: str,
        raw_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        where, params = _temporal_where(as_of, "a")
        cypher = (
            "MATCH (a:Alias {source_system: $src, raw_id: $rid}) "
            f"WHERE {where} RETURN a"
        )
        record = self._run_read_single(cypher, src=source_system, rid=raw_id, **params)
        if record is None:
            return None
        return _alias_props_to_dict(dict(record["a"]))

    def get_aliases(
        self,
        entity_id: str,
        source_system: str | None = None,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        where, params = _temporal_where(as_of, "a")
        params["entity_id"] = entity_id
        src_clause = ""
        if source_system is not None:
            src_clause = "AND a.source_system = $source_system"
            params["source_system"] = source_system
        cypher = (
            f"MATCH (a:Alias {{entity_id: $entity_id}}) "
            f"WHERE {where} {src_clause} "
            "RETURN a ORDER BY a.source_system, a.raw_id"
        )
        records = self._run_read_list(cypher, **params)
        return [_alias_props_to_dict(dict(r["a"])) for r in records]

    # ------------------------------------------------------------------
    # Edges
    # ------------------------------------------------------------------

    def upsert_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        properties: dict[str, Any] | None = None,
        *,
        commit: bool = True,  # noqa: ARG002
    ) -> str:
        # One Cypher round trip: endpoint MATCH + existing-edge
        # OPTIONAL MATCH + close-old + create-new in a single query.
        # ``coalesce`` carries ``edge_id`` and ``created_at`` forward
        # from any current version. Previously this path made a
        # separate ``_find_current_edge`` call (~50% of total upsert
        # latency on AuraDB Free).
        now = _iso(utc_now())
        candidate_edge_id = generate_ulid()
        base_props = {
            "version_id": generate_ulid(),
            "source_id": source_id,
            "target_id": target_id,
            "edge_type": edge_type,
            "properties_json": json.dumps(properties or {}),
            "valid_from": now,
            "valid_to": None,
        }

        cypher = """
        MATCH (s:Node {node_id: $source_id}) WHERE s.valid_to IS NULL
        MATCH (t:Node {node_id: $target_id}) WHERE t.valid_to IS NULL
        OPTIONAL MATCH (s)-[old:EDGE {edge_type: $edge_type}]->(t)
          WHERE old.valid_to IS NULL
        WITH s, t, old,
             coalesce(old.edge_id, $candidate_edge_id) AS edge_id_carry,
             coalesce(old.created_at, $now) AS created_at_carry
        SET old.valid_to = $now
        WITH s, t, edge_id_carry, created_at_carry
        CREATE (s)-[new:EDGE]->(t)
        SET new = $base_props
        SET new.edge_id = edge_id_carry
        SET new.created_at = created_at_carry
        RETURN new.edge_id AS edge_id
        """
        record = self._run_write_single(
            cypher,
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            now=now,
            candidate_edge_id=candidate_edge_id,
            base_props=base_props,
        )
        if record is None:
            msg = (
                f"Cannot upsert edge: source {source_id!r} or target "
                f"{target_id!r} has no current version"
            )
            raise ValueError(msg)
        return str(record["edge_id"])

    def upsert_edges_bulk(
        self,
        edges: list[dict[str, Any]],
    ) -> list[str]:
        if not edges:
            return []

        # Pass 1 — Python-side validation. The single-row method has no
        # explicit validators (other than the implicit MATCH inside the
        # Cypher), but we want clear errors before any I/O. Within-batch
        # duplicate triplets are rejected because the bulk pre-fetch
        # only sees the prior edge once — duplicates would all auto-
        # assign their own edge_id and create distinct current versions
        # for one logical edge.
        validate_bulk_required_keys(
            edges, ("source_id", "target_id", "edge_type"), "upsert_edges_bulk"
        )
        self._pre_validate_edges_bulk(edges)

        # Round trip 1 — validate that every source + target has a
        # current version. Done up front so a bad row gets a precise
        # error pointing at its index, not a silent drop in the UNWIND.
        endpoint_ids = sorted(
            {spec["source_id"] for spec in edges}
            | {spec["target_id"] for spec in edges}
        )
        valid_endpoints = self._fetch_current_node_id_set(endpoint_ids)
        for i, spec in enumerate(edges):
            if spec["source_id"] not in valid_endpoints:
                msg = (
                    f"upsert_edges_bulk[{i}]: source "
                    f"{spec['source_id']!r} has no current version"
                )
                raise ValueError(msg)
            if spec["target_id"] not in valid_endpoints:
                msg = (
                    f"upsert_edges_bulk[{i}]: target "
                    f"{spec['target_id']!r} has no current version"
                )
                raise ValueError(msg)

        # Round trip 2 — fetch existing current edges keyed by
        # (source, target, edge_type) so we can carry edge_id +
        # created_at forward, matching the per-row contract.
        existing = self._fetch_current_edges_bulk(
            [
                (spec["source_id"], spec["target_id"], spec["edge_type"])
                for spec in edges
            ]
        )

        now = _iso(utc_now())
        edge_ids: list[str] = []
        rows: list[dict[str, Any]] = []
        for spec in edges:
            triplet = (spec["source_id"], spec["target_id"], spec["edge_type"])
            prior = existing.get(triplet)
            if prior is not None:
                edge_id = str(prior["edge_id"])
                created_at = prior["created_at"]
            else:
                edge_id = generate_ulid()
                created_at = now
            edge_ids.append(edge_id)
            rows.append(
                {
                    "source_id": spec["source_id"],
                    "target_id": spec["target_id"],
                    "edge_type": spec["edge_type"],
                    "props": {
                        "edge_id": edge_id,
                        "version_id": generate_ulid(),
                        "source_id": spec["source_id"],
                        "target_id": spec["target_id"],
                        "edge_type": spec["edge_type"],
                        "properties_json": json.dumps(spec.get("properties") or {}),
                        "created_at": created_at,
                        "valid_from": now,
                        "valid_to": None,
                    },
                }
            )

        # Round trip 3 — close existing current edges and create new
        # versions in one UNWIND. Endpoints validated above.
        cypher = """
        UNWIND $rows AS row
        MATCH (s:Node {node_id: row.source_id}) WHERE s.valid_to IS NULL
        MATCH (t:Node {node_id: row.target_id}) WHERE t.valid_to IS NULL
        WITH s, t, row
        OPTIONAL MATCH (s)-[old:EDGE {edge_type: row.edge_type}]->(t)
          WHERE old.valid_to IS NULL
        SET old.valid_to = row.props.valid_from
        WITH s, t, row
        CREATE (s)-[new:EDGE]->(t)
        SET new = row.props
        """
        with self._driver.session(database=self._database) as session:
            session.execute_write(lambda tx: tx.run(cypher, rows=rows).consume())

        logger.debug("edges_upserted_bulk", count=len(edge_ids))
        return edge_ids

    def _fetch_current_node_id_set(self, node_ids: list[str]) -> set[str]:
        """Round-trip helper: which of these IDs have a current version?"""
        if not node_ids:
            return set()
        cypher = (
            "MATCH (n:Node) WHERE n.node_id IN $ids AND n.valid_to IS NULL "
            "RETURN n.node_id AS node_id"
        )
        with self._driver.session(database=self._database) as session:
            records = session.execute_read(
                lambda tx: list(tx.run(cypher, ids=node_ids))
            )
        return {r["node_id"] for r in records}

    def _fetch_current_edges_bulk(
        self, triplets: list[tuple[str, str, str]]
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        """Round-trip helper: load current edges for ``(s, t, type)`` triplets.

        Returns a map keyed by triplet for entries that currently exist.
        """
        if not triplets:
            return {}
        # Encode triplets as dicts so the driver can ship them as
        # parameters; tuples aren't natively serialisable.
        params = [
            {"source_id": s, "target_id": t, "edge_type": k} for s, t, k in triplets
        ]
        cypher = """
        UNWIND $triplets AS t
        MATCH (s:Node {node_id: t.source_id}) WHERE s.valid_to IS NULL
        MATCH (s)-[r:EDGE {edge_type: t.edge_type}]->
              (target:Node {node_id: t.target_id})
        WHERE target.valid_to IS NULL AND r.valid_to IS NULL
        RETURN t.source_id AS source_id, t.target_id AS target_id,
               t.edge_type AS edge_type, r.edge_id AS edge_id,
               r.created_at AS created_at
        """
        with self._driver.session(database=self._database) as session:
            records = session.execute_read(
                lambda tx: list(tx.run(cypher, triplets=params))
            )
        out: dict[tuple[str, str, str], dict[str, Any]] = {}
        for r in records:
            key = (r["source_id"], r["target_id"], r["edge_type"])
            out[key] = {"edge_id": r["edge_id"], "created_at": r["created_at"]}
        return out

    def _find_current_edge(
        self, source_id: str, target_id: str, edge_type: str
    ) -> dict[str, Any] | None:
        cypher = """
        MATCH (s:Node {node_id: $source_id})-[r:EDGE {edge_type: $edge_type}]->
              (t:Node {node_id: $target_id})
        WHERE r.valid_to IS NULL
        RETURN r
        LIMIT 1
        """
        record = self._run_read_single(
            cypher,
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
        )
        if record is None:
            return None
        return _edge_props_to_dict(dict(record["r"]))

    def get_edges(
        self,
        node_id: str,
        direction: str = "both",
        edge_type: str | None = None,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if direction not in ("outgoing", "incoming", "both"):
            msg = f"direction must be outgoing|incoming|both, got {direction!r}"
            raise ValueError(msg)

        where, params = _temporal_where(as_of, "r")
        params["node_id"] = node_id
        type_clause = ""
        if edge_type is not None:
            type_clause = "AND r.edge_type = $edge_type"
            params["edge_type"] = edge_type

        if direction == "outgoing":
            pattern = "(n:Node {node_id: $node_id})-[r:EDGE]->()"
        elif direction == "incoming":
            pattern = "()-[r:EDGE]->(n:Node {node_id: $node_id})"
        else:
            pattern = "(n:Node {node_id: $node_id})-[r:EDGE]-()"

        cypher = f"MATCH {pattern} WHERE {where} {type_clause} RETURN r"
        records = self._run_read_list(cypher, **params)
        return [_edge_props_to_dict(dict(r["r"])) for r in records]

    # ------------------------------------------------------------------
    # Subgraph
    # ------------------------------------------------------------------

    def get_subgraph(
        self,
        seed_ids: list[str],
        depth: int = 2,
        edge_types: list[str] | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        if not seed_ids:
            return {"nodes": [], "edges": []}
        if depth < 0:
            msg = f"depth must be >= 0, got {depth}"
            raise ValueError(msg)

        node_where, temporal_params = _temporal_where(as_of, "n")
        edge_where, _ = _temporal_where(as_of, "r")

        edge_type_clause = "AND ($edge_types IS NULL OR r.edge_type IN $edge_types)"

        # Step 1 — reachable node ids via variable-length match. The
        # bounds of `*M..N` must be literal Cypher, so we inline `depth`
        # after the validation above.
        if depth == 0:
            reachable = list(seed_ids)
        else:
            seed_where = node_where.replace("n.", "seed.")
            other_where = node_where.replace("n.", "other.")
            reach_cypher = f"""
            MATCH (seed:Node) WHERE seed.node_id IN $seed_ids AND {seed_where}
            OPTIONAL MATCH (seed)-[rels:EDGE*1..{depth}]-(other:Node)
            WHERE all(e IN rels WHERE {edge_where.replace("r.", "e.")}
                      {edge_type_clause.replace("r.", "e.")})
              AND (other IS NULL OR {other_where})
            WITH collect(DISTINCT seed.node_id) AS seed_ids,
                 collect(DISTINCT other.node_id) AS other_ids
            RETURN [id IN seed_ids + other_ids WHERE id IS NOT NULL] AS ids
            """
            params: dict[str, Any] = {
                "seed_ids": seed_ids,
                "edge_types": edge_types,
                **temporal_params,
            }
            record = self._run_read_single(reach_cypher, **params)
            raw_ids = record["ids"] if record else []
            reachable = list({i for i in raw_ids if i is not None})

        if not reachable:
            return {"nodes": [], "edges": []}

        # Step 2 — fetch full node payloads
        nodes = self.get_nodes_bulk(reachable, as_of=as_of)

        # Step 3 — fetch edges between reachable nodes (directed match
        # so each edge appears once).
        edge_cypher = (
            "MATCH (s:Node)-[r:EDGE]->(t:Node) "
            "WHERE s.node_id IN $ids AND t.node_id IN $ids "
            f"AND {edge_where} {edge_type_clause} "
            "RETURN r"
        )
        records = self._run_read_list(
            edge_cypher,
            ids=reachable,
            edge_types=edge_types,
            **temporal_params,
        )
        edges = [_edge_props_to_dict(dict(r["r"])) for r in records]

        logger.debug(
            "subgraph_fetched",
            seed_count=len(seed_ids),
            depth=depth,
            nodes_found=len(nodes),
            edges_found=len(edges),
        )
        return {"nodes": nodes, "edges": edges}

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        node_type: str | None = None,
        properties: dict[str, Any] | None = None,
        limit: int = 50,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        where, params = _temporal_where(as_of, "n")
        conditions = [where]
        if node_type is not None:
            conditions.append("n.node_type = $node_type")
            params["node_type"] = node_type

        # Scalar property filters are handled by decoding properties_json
        # in Cypher would require APOC; keep the backend community-safe
        # by filtering client-side, matching how query handles nested
        # filters in the Postgres backend.
        cypher = (
            "MATCH (n:Node) WHERE "
            + " AND ".join(conditions)
            + " RETURN n ORDER BY n.created_at DESC LIMIT $limit"
        )
        params["limit"] = limit * 4 if properties else limit

        records = self._run_read_list(cypher, **params)

        results: list[dict[str, Any]] = []
        for r in records:
            node = _node_props_to_dict(dict(r["n"]))
            if properties:
                props = node["properties"]
                if not all(props.get(k) == v for k, v in properties.items()):
                    continue
            results.append(node)
            if len(results) >= limit:
                break
        return results

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_node(self, node_id: str) -> bool:
        def _tx(tx: ManagedTransaction) -> bool:
            record = tx.run(
                "MATCH (n:Node {node_id: $nid}) RETURN count(n) AS cnt",
                nid=node_id,
            ).single()
            existed = bool(record and record["cnt"] > 0)
            # DETACH DELETE cleans up all :EDGE relationships automatically.
            tx.run(
                "MATCH (n:Node {node_id: $nid}) DETACH DELETE n", nid=node_id
            ).consume()
            tx.run(
                "MATCH (a:Alias {entity_id: $nid}) DETACH DELETE a", nid=node_id
            ).consume()
            return existed

        with self._driver.session(database=self._database) as session:
            deleted = bool(session.execute_write(_tx))
        if deleted:
            logger.debug("node_deleted", node_id=node_id)
        return deleted

    def delete_edge(self, edge_id: str) -> bool:
        def _tx(tx: ManagedTransaction) -> bool:
            record = tx.run(
                "MATCH ()-[r:EDGE {edge_id: $eid}]->() RETURN count(r) AS cnt",
                eid=edge_id,
            ).single()
            existed = bool(record and record["cnt"] > 0)
            tx.run(
                "MATCH ()-[r:EDGE {edge_id: $eid}]->() DELETE r", eid=edge_id
            ).consume()
            return existed

        with self._driver.session(database=self._database) as session:
            deleted = bool(session.execute_write(_tx))
        if deleted:
            logger.debug("edge_deleted", edge_id=edge_id)
        return deleted

    # ------------------------------------------------------------------
    # Counts
    # ------------------------------------------------------------------

    def count_nodes(self) -> int:
        cypher = (
            "MATCH (n:Node) WHERE n.valid_to IS NULL "
            "RETURN count(DISTINCT n.node_id) AS cnt"
        )
        record = self._run_read_single(cypher)
        return int(record["cnt"]) if record else 0

    def count_edges(self) -> int:
        cypher = "MATCH ()-[r:EDGE]->() WHERE r.valid_to IS NULL RETURN count(r) AS cnt"
        record = self._run_read_single(cypher)
        return int(record["cnt"]) if record else 0

    # ------------------------------------------------------------------
    # Canonical DSL — Phase 2 compiler
    # ------------------------------------------------------------------

    def execute_node_query(self, query: Any) -> list[dict[str, Any]]:
        """Compile :class:`NodeQuery` to Cypher.

        Top-level field filters (``node_type`` / ``node_role`` /
        ``node_id``) compile to native Cypher predicates with ``=`` /
        ``IN`` / ``IS NOT NULL``. Property filters (``properties.<key>``)
        cannot use native Cypher comparisons because the graph store
        encodes ``properties`` as a JSON string (``properties_json``),
        so they over-fetch with the structural filters applied in
        Cypher and apply the property predicates client-side after
        decoding the JSON. Same approach the legacy ``query()`` method
        uses; semantics match.
        """
        cypher_parts, cypher_params, py_predicates = self._compile_node_query(query)
        cypher = (
            "MATCH (n:Node) WHERE "
            + " AND ".join(cypher_parts)
            + " RETURN n ORDER BY n.created_at DESC"
        )
        # Over-fetch when py_predicates are present to compensate for
        # client-side trimming.
        fetch_limit = query.limit * 10 if py_predicates else query.limit
        cypher += f" LIMIT {int(fetch_limit)}"

        records = self._run_read_list(cypher, **cypher_params)
        results: list[dict[str, Any]] = []
        for r in records:
            row = _node_props_to_dict(dict(r["n"]))
            if all(pred(row) for pred in py_predicates):
                results.append(row)
                if len(results) >= query.limit:
                    break
        return results

    def _compile_node_query(
        self, query: Any
    ) -> tuple[list[str], dict[str, Any], list[Any]]:
        """Pure compile — returns (cypher_where_parts, params, python_predicates)."""
        cypher_parts: list[str] = [self._temporal_filter_cypher(query.as_of)]
        cypher_params: dict[str, Any] = {}
        if query.as_of is not None:
            cypher_params["as_of"] = query.as_of.isoformat()
        py_predicates: list[Any] = []
        for i, clause in enumerate(query.filters):
            if clause.field.startswith("properties."):
                py_predicates.append(self._compile_property_predicate(clause))
                continue
            frag, params = self._compile_top_level_clause(clause, i)
            cypher_parts.append(frag)
            cypher_params.update(params)
        return cypher_parts, cypher_params, py_predicates

    @staticmethod
    def _compile_top_level_clause(clause: Any, idx: int) -> tuple[str, dict[str, Any]]:
        column = clause.field
        if column not in {"node_type", "node_role", "node_id"}:
            msg = f"Unsupported DSL field path: {clause.field!r}"
            raise ValueError(msg)
        if clause.op == "eq":
            pname = f"f{idx}"
            return f"n.{column} = ${pname}", {pname: clause.value}
        if clause.op == "in":
            pname = f"f{idx}"
            return f"n.{column} IN ${pname}", {pname: list(clause.value)}
        if clause.op == "exists":
            return f"n.{column} IS NOT NULL", {}
        msg = f"Unknown filter op {clause.op!r}"
        raise ValueError(msg)

    @staticmethod
    def _compile_property_predicate(clause: Any) -> Any:
        """Return a callable that takes a node-dict and returns bool."""
        key = clause.field.split(".", 1)[1]
        if clause.op == "eq":
            target = clause.value
            return lambda node: node["properties"].get(key) == target
        if clause.op == "in":
            allowed = set(clause.value)
            return lambda node: node["properties"].get(key) in allowed
        if clause.op == "exists":
            return lambda node: node["properties"].get(key) is not None
        msg = f"Unknown filter op {clause.op!r}"
        raise ValueError(msg)

    @staticmethod
    def _temporal_filter_cypher(as_of: datetime | None) -> str:
        if as_of is None:
            return "n.valid_to IS NULL"
        return "n.valid_from <= $as_of AND (n.valid_to IS NULL OR n.valid_to > $as_of)"

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------

    def compact_versions(
        self,
        before: datetime,
        *,
        dry_run: bool = False,
        event_log: EventLog | None = None,
    ) -> CompactionReport:
        before_iso = before.isoformat()
        start_ns = time.monotonic_ns()

        def _count_and_range(
            tx: ManagedTransaction, cypher: str
        ) -> tuple[int, str | None, str | None]:
            rec = tx.run(cypher, before=before_iso).single()
            if rec is None:
                return 0, None, None
            return int(rec["cnt"] or 0), rec["lo"], rec["hi"]

        queries = {
            "nodes": (
                "MATCH (n:Node) "
                "WHERE n.valid_to IS NOT NULL AND n.valid_to < $before "
                "RETURN count(n) AS cnt, min(n.valid_to) AS lo, "
                "       max(n.valid_to) AS hi",
                "MATCH (n:Node) "
                "WHERE n.valid_to IS NOT NULL AND n.valid_to < $before "
                "DETACH DELETE n",
            ),
            "edges": (
                "MATCH ()-[r:EDGE]->() "
                "WHERE r.valid_to IS NOT NULL AND r.valid_to < $before "
                "RETURN count(r) AS cnt, min(r.valid_to) AS lo, "
                "       max(r.valid_to) AS hi",
                "MATCH ()-[r:EDGE]->() "
                "WHERE r.valid_to IS NOT NULL AND r.valid_to < $before DELETE r",
            ),
            "aliases": (
                "MATCH (a:Alias) "
                "WHERE a.valid_to IS NOT NULL AND a.valid_to < $before "
                "RETURN count(a) AS cnt, min(a.valid_to) AS lo, "
                "       max(a.valid_to) AS hi",
                "MATCH (a:Alias) "
                "WHERE a.valid_to IS NOT NULL AND a.valid_to < $before "
                "DETACH DELETE a",
            ),
        }

        counts: dict[str, int] = {}
        range_valid_to: list[str] = []

        with self._driver.session(database=self._database) as session:
            for table, (count_q, delete_q) in queries.items():
                # Closures with default args avoid late-binding bugs
                # in the loop AND give mypy a typed signature to
                # check (lambdas in this position fail to infer).
                def _count(
                    tx: ManagedTransaction, q: str = count_q
                ) -> tuple[int, str | None, str | None]:
                    return _count_and_range(tx, q)

                def _delete(tx: ManagedTransaction, q: str = delete_q) -> None:
                    tx.run(q, before=before_iso).consume()

                cnt, lo, hi = session.execute_read(_count)
                counts[table] = cnt
                if cnt > 0:
                    if lo is not None:
                        range_valid_to.append(lo)
                    if hi is not None:
                        range_valid_to.append(hi)
                    if not dry_run:
                        session.execute_write(_delete)

        def _parse(s: str | None) -> datetime | None:
            if s is None:
                return None
            return datetime.fromisoformat(s)

        report = CompactionReport(
            before=before,
            nodes_compacted=counts.get("nodes", 0),
            edges_compacted=counts.get("edges", 0),
            aliases_compacted=counts.get("aliases", 0),
            oldest_compacted_valid_to=_parse(min(range_valid_to))
            if range_valid_to
            else None,
            newest_compacted_valid_to=_parse(max(range_valid_to))
            if range_valid_to
            else None,
            dry_run=dry_run,
            duration_ms=max((time.monotonic_ns() - start_ns) // 1_000_000, 0),
        )
        logger.info(
            "graph_versions_compacted",
            before=before_iso,
            dry_run=dry_run,
            nodes=report.nodes_compacted,
            edges=report.edges_compacted,
            aliases=report.aliases_compacted,
            duration_ms=report.duration_ms,
        )
        if event_log is not None:
            event_log.emit(
                EventType.GRAPH_VERSIONS_COMPACTED,
                source="graph_store",
                payload=report.model_dump(mode="json"),
            )
        return report

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._driver.close()
        logger.info("neo4j_graph_store_closed")
