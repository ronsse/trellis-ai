"""ArcadeDBGraphStore — thin adapter over :class:`BoltOpenCypherGraphStore`.

The shared base class' Cypher payload runs against ArcadeDB unchanged.
This class swaps in ArcadeDB-specific driver construction (basic auth
over Bolt) and idempotent HTTP-based database creation, then defers
everything else to the parent. See
:mod:`trellis.stores.bolt_opencypher.graph` for the SCD-2 + Cypher
contract and :class:`ArcadeDBVectorStore` for the paired vector path
(SQL over HTTP, not Cypher).

Self-hosting note: this store connects over Bolt (default port 7687),
which a stock self-hosted ArcadeDB does **not** expose by default — only
the HTTP endpoint (2480) is on. Start the server (or build the image)
with the Bolt plugin enabled::

    -Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin

A connection-refused on 7687 while 2480 answers means the plugin flag is
missing. See ``docs/design/adr-arcadedb-blessed-substrate.md``
("Self-hosting requirement") for the full deployment note.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from trellis.stores.arcadedb.base import (
    build_arcadedb_driver,
    ensure_database,
    execute_sql,
)
from trellis.stores.bolt_opencypher.base import (
    BoltDriverConfig,
    check_driver_installed,
)
from trellis.stores.bolt_opencypher.graph import BoltOpenCypherGraphStore

if TYPE_CHECKING:
    from neo4j import Driver

logger = structlog.get_logger(__name__)


#: ArcadeDB schema-typed properties for the edge provenance columns
#: (Phase 3 of ``adr-graph-ontology.md`` §6.4 / item 2 of the
#: self-improvement program). ArcadeDB stores relationship properties
#: untyped by default; declaring them with ``CREATE PROPERTY`` opts the
#: column into ArcadeDB's type-coercion + constraint surface so
#: ``confidence`` carries a server-enforced ``MIN/MAX`` range matching
#: the Python-boundary validator. ``extractor_tier`` is left untyped
#: beyond STRING — ArcadeDB has no enum constraint, so the allowlist is
#: enforced by :func:`trellis.stores.base.edge_provenance.validate_edge_provenance`.
#:
#: ``CREATE PROPERTY ... IF NOT EXISTS`` is idempotent — re-runs are
#: cheap no-ops against an already-migrated database. The trailing
#: ``CREATE EDGE TYPE EDGE IF NOT EXISTS`` mirrors the vector store's
#: ``CREATE VERTEX TYPE Node IF NOT EXISTS`` pattern: the Cypher write
#: path auto-creates the edge type on first use, but we declare it
#: explicitly so ``CREATE PROPERTY`` has a target schema to attach to
#: even on a fresh database where no edges have been written yet.
_ARCADEDB_EDGE_PROVENANCE_SCHEMA: tuple[str, ...] = (
    "CREATE EDGE TYPE EDGE IF NOT EXISTS",
    "CREATE PROPERTY EDGE.source_trace_id IF NOT EXISTS STRING",
    "CREATE PROPERTY EDGE.agent_id IF NOT EXISTS STRING",
    # ArcadeDB FLOAT is 32-bit; FLOAT supports MIN/MAX constraints.
    # The Python validator enforces the same [0.0, 1.0] range with a
    # message that points at the offending value.
    "CREATE PROPERTY EDGE.confidence IF NOT EXISTS FLOAT (MIN 0.0, MAX 1.0)",
    "CREATE PROPERTY EDGE.evidence_ref IF NOT EXISTS STRING",
    # ArcadeDB has no enum constraint — the allowlist
    # ({DETERMINISTIC, HYBRID, LLM}) is enforced at the Python
    # boundary in ``validate_edge_provenance``. Declaring the property
    # as STRING still pulls it into ArcadeDB's schema so queries can
    # filter on it without JSON-extracting the properties bag.
    "CREATE PROPERTY EDGE.extractor_tier IF NOT EXISTS STRING",
)


class ArcadeDBGraphStore(BoltOpenCypherGraphStore):
    """ArcadeDB-backed graph store.

    The Cypher payload + SCD-2 logic live in the parent class. This
    subclass builds a Bolt driver with basic auth and (optionally)
    creates the target ArcadeDB database via the HTTP admin endpoint
    before any Bolt session opens.
    """

    def __init__(
        self,
        uri: str,
        *,
        user: str = "root",
        password: str | None = None,
        database: str = "trellis",
        driver: Driver | None = None,
        driver_config: BoltDriverConfig | None = None,
        http_url: str | None = None,
        ensure_database_exists: bool = True,
    ) -> None:
        """Initialize an ArcadeDB graph store.

        Parameters
        ----------
        uri
            Bolt URI for the ArcadeDB server, e.g.
            ``bolt://arcadedb.internal:7687``.
        user, password
            Basic-auth credentials. ``root`` is the conventional admin
            user for ArcadeDB; production deployments should create a
            dedicated user with the minimum required privileges.
        database
            Target ArcadeDB database name. Created on first boot if
            ``ensure_database_exists=True``.
        driver
            Optional pre-built driver, typically injected by
            :class:`StoreRegistry` so the graph + vector pair share one
            connection pool. When set, ``password`` and
            ``driver_config`` must be ``None`` (mutually exclusive
            configuration paths).
        driver_config
            Optional :class:`BoltDriverConfig` overriding the default
            production-safe driver kwargs. Ignored if ``driver`` is set.
        http_url
            Base URL for ArcadeDB's HTTP REST endpoint, e.g.
            ``http://arcadedb.internal:2480``. Required when
            ``ensure_database_exists=True`` (the HTTP endpoint is the
            documented path for database creation). When omitted, the
            store assumes the database is already present and the HTTP
            URL isn't needed.
        ensure_database_exists
            When True (the default), creates the named database via the
            HTTP REST endpoint at startup if it doesn't already exist.
            Set False to skip the check — appropriate for deployments
            where the database is provisioned out-of-band (e.g. by
            Terraform) and the HTTP endpoint isn't exposed to the
            store's network.
        """
        check_driver_installed()

        # Injected-driver path: registry shared a driver across the
        # graph + future Bolt siblings. ``close()`` is a no-op.
        if driver is not None:
            if password is not None or driver_config is not None:
                msg = (
                    "Pass either ``driver`` (caller-owned) or "
                    "``password`` + ``driver_config`` (store-owned), not both."
                )
                raise ValueError(msg)
            super().__init__(driver=driver, database=database, owns_driver=False)
            # Property-schema migration requires HTTP credentials. The
            # registry path forwards ``http_url`` (so we know it
            # resolved one) but strips ``password`` to preserve the
            # mutex with ``driver``. In that case the registry already
            # ran the migration before injecting the driver — log at
            # debug, no missing-constraint risk.
            #
            # If both ``http_url`` and ``password`` are present (a
            # direct caller bypassing the mutex check would not get
            # past the guard above, so this is only the future-proof
            # path where the constructor itself drives migration), run
            # it here. Otherwise — no http_url at all — fall back to
            # the warning: a direct caller missed credentials and the
            # FLOAT MIN/MAX constraint will not be installed.
            if http_url is not None and password is not None:
                self._init_arcadedb_edge_provenance_schema(
                    http_url=http_url,
                    user=user,
                    password=password,
                    database=database,
                )
            elif http_url is not None:
                logger.debug(
                    "arcadedb_provenance_schema_migration_handled_by_registry",
                    reason=(
                        "http_url forwarded alongside injected driver but "
                        "password stripped (registry mutex). Registry runs "
                        "the typed-property migration itself before "
                        "injecting the driver."
                    ),
                )
            else:
                logger.warning(
                    "arcadedb_provenance_schema_migration_skipped_injected_driver",
                    reason=(
                        "http_url not supplied alongside injected driver; "
                        "the FLOAT MIN/MAX constraint on edge.confidence "
                        "will not be installed. Pass http_url (and run "
                        "the migration externally) to enable the schema-"
                        "typed property constraint, or construct via "
                        "StoreRegistry which handles this automatically."
                    ),
                )
            logger.info(
                "arcadedb_graph_store_initialized",
                uri=uri,
                database=database,
            )
            return

        # Store-owned-driver path: build our own driver + (optionally)
        # idempotently create the target database via HTTP first.
        if password is None:
            msg = "password is required when ``driver`` is not provided"
            raise ValueError(msg)
        if ensure_database_exists:
            if http_url is None:
                msg = (
                    "http_url is required when ensure_database_exists=True "
                    "(database creation goes through the HTTP REST endpoint, "
                    "not Bolt). Either provide http_url or set "
                    "ensure_database_exists=False if the database is "
                    "pre-provisioned."
                )
                raise ValueError(msg)
            ensure_database(http_url, user, password, database)
        driver = build_arcadedb_driver(uri, user, password, config=driver_config)
        super().__init__(driver=driver, database=database, owns_driver=True)
        # Idempotently declare schema-typed properties for the
        # provenance columns + the FLOAT MIN/MAX constraint on
        # ``confidence``. Runs over HTTP SQL because openCypher does
        # not expose ArcadeDB's typed-property DDL. Safe to call on
        # every boot — every statement is ``IF NOT EXISTS``.
        if http_url is not None:
            self._init_arcadedb_edge_provenance_schema(
                http_url=http_url,
                user=user,
                password=password,
                database=database,
            )
        else:
            logger.warning(
                "arcadedb_provenance_schema_migration_skipped_no_http_url",
                reason=(
                    "http_url not supplied; provenance properties will be "
                    "created lazily on first write without typed-property "
                    "constraints. Pass http_url to enable the FLOAT "
                    "MIN/MAX constraint on confidence."
                ),
            )
        logger.info(
            "arcadedb_graph_store_initialized",
            uri=uri,
            database=database,
        )

    @staticmethod
    def _init_arcadedb_edge_provenance_schema(
        *,
        http_url: str,
        user: str,
        password: str,
        database: str,
    ) -> None:
        """Run the idempotent ``CREATE PROPERTY`` migration via HTTP SQL.

        Each statement is ``IF NOT EXISTS`` so calling this against an
        already-migrated database is a no-op. Failures bubble up as
        ``RuntimeError`` from :func:`execute_sql` — the registry will
        surface them at boot rather than as opaque errors on first
        write.
        """
        for stmt in _ARCADEDB_EDGE_PROVENANCE_SCHEMA:
            execute_sql(http_url, user, password, database, stmt)
        logger.info(
            "arcadedb_edge_provenance_schema_migrated",
            database=database,
            statements=len(_ARCADEDB_EDGE_PROVENANCE_SCHEMA),
        )

    def close(self) -> None:
        owns = self._owns_driver
        super().close()
        if owns:
            logger.info("arcadedb_graph_store_closed")
        else:
            logger.debug("arcadedb_graph_store_close_noop_injected_driver")
