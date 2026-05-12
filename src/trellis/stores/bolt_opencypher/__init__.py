"""openCypher-over-Bolt graph store base class.

Shared SCD-2 + Cypher payload + driver/session plumbing for any backend
that speaks the Bolt protocol with an openCypher dialect. Today this is
Neo4j; ArcadeDB (Apache 2.0, Bolt + openCypher 25 at 97.8% TCK) is the
next adopter and the reason this base class exists. Per-backend
subclasses override only the connection-time seams: how to build the
driver (auth/URI), which DDL statements to run in ``_init_schema``, and
any small Cypher dialect quirks via the ``DIALECT`` class attribute.

This is *not* a public extension point yet — the seam contract may
evolve as additional backends land. Treat the class as internal until an
ADR blesses the contract.
"""

from trellis.stores.bolt_opencypher.base import (
    BoltDriverConfig,
    BoltSessionRunner,
    check_driver_installed,
    verify_connectivity,
)
from trellis.stores.bolt_opencypher.graph import BoltOpenCypherGraphStore

__all__ = [
    "BoltDriverConfig",
    "BoltOpenCypherGraphStore",
    "BoltSessionRunner",
    "check_driver_installed",
    "verify_connectivity",
]
