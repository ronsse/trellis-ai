"""ArcadeDBGraphStore — thin adapter over :class:`BoltOpenCypherGraphStore`.

The shared base class' Cypher payload runs against ArcadeDB unchanged.
This class swaps in ArcadeDB-specific driver construction (basic auth
over Bolt) and idempotent HTTP-based database creation, then defers
everything else to the parent. See
:mod:`trellis.stores.bolt_opencypher.graph` for the SCD-2 + Cypher
contract and :class:`ArcadeDBVectorStore` for the paired vector path
(SQL over HTTP, not Cypher).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from trellis.stores.arcadedb.base import (
    build_arcadedb_driver,
    ensure_database,
)
from trellis.stores.bolt_opencypher.base import (
    BoltDriverConfig,
    check_driver_installed,
)
from trellis.stores.bolt_opencypher.graph import BoltOpenCypherGraphStore

if TYPE_CHECKING:
    from neo4j import Driver

logger = structlog.get_logger(__name__)


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
        logger.info(
            "arcadedb_graph_store_initialized",
            uri=uri,
            database=database,
        )

    def close(self) -> None:
        owns = self._owns_driver
        super().close()
        if owns:
            logger.info("arcadedb_graph_store_closed")
        else:
            logger.debug("arcadedb_graph_store_close_noop_injected_driver")
