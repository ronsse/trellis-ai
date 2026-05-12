"""Neo4jGraphStore — thin adapter over :class:`BoltOpenCypherGraphStore`.

Cypher payload, SCD-2 versioning, JSON property encoding, session
management — all shared with other Bolt-speaking backends in
:mod:`trellis.stores.bolt_opencypher.graph`. This file holds only the
Neo4j-specific seam: driver construction with basic authentication.

Note: Neo4j Community Edition does not support partial uniqueness
constraints (``UNIQUE ... WHERE valid_to IS NULL``). The "at most one
current version per node_id" invariant is enforced by the
close-then-insert transaction rather than by the database. Under
concurrent writers on Community, a second writer can observe a stale
"no current" state and create a duplicate current row.
Enterprise/Aura users can layer a node key constraint on top.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from trellis.stores.bolt_opencypher.graph import BoltOpenCypherGraphStore
from trellis.stores.neo4j.base import (
    DriverConfig,
    build_driver,
    check_driver_installed,
)

if TYPE_CHECKING:
    from neo4j import Driver

logger = structlog.get_logger(__name__)


class Neo4jGraphStore(BoltOpenCypherGraphStore):
    """Neo4j-backed graph store.

    The Cypher payload + SCD-2 logic live in the parent class. This
    subclass just builds the driver with basic auth and emits Neo4j-
    labeled lifecycle log events.
    """

    def __init__(
        self,
        uri: str,
        *,
        user: str = "neo4j",
        password: str | None = None,
        database: str = "neo4j",
        driver: Driver | None = None,
        driver_config: DriverConfig | None = None,
    ) -> None:
        # Driver lifecycle: when ``driver`` is injected, the caller
        # (typically ``StoreRegistry`` sharing one driver across the
        # graph + vector pair) owns it and ``close()`` is a no-op.
        # Otherwise we build our own from ``driver_config`` and own
        # it. Mixing the two is a programming error.
        check_driver_installed()
        if driver is not None:
            if password is not None or driver_config is not None:
                msg = (
                    "Pass either ``driver`` (caller-owned) or "
                    "``password`` + ``driver_config`` (store-owned), not both."
                )
                raise ValueError(msg)
            owns = False
        else:
            if password is None:
                msg = "password is required when ``driver`` is not provided"
                raise ValueError(msg)
            driver = build_driver(uri, user, password, config=driver_config)
            owns = True
        super().__init__(driver=driver, database=database, owns_driver=owns)
        logger.info("neo4j_graph_store_initialized", uri=uri, database=database)

    def close(self) -> None:
        owns = self._owns_driver
        super().close()
        if owns:
            logger.info("neo4j_graph_store_closed")
        else:
            logger.debug("neo4j_graph_store_close_noop_injected_driver")
