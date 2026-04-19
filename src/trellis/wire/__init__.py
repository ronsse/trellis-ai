"""Translators between core Pydantic models and wire DTOs.

This module sits *inside* ``trellis`` core (so it can import core
types) and imports the standalone ``trellis_wire`` package (so it can
return / accept wire DTOs).  It is the only module that knows about
both sides.

The wire package itself has **zero dependency on ``trellis.*``** — that
invariant is what makes client packages able to depend on
``trellis_wire`` in isolation.  Translators live here instead so the
dependency direction stays: core → wire, never wire → core.

See :mod:`trellis.wire.translate` for the actual translation
functions.
"""

from trellis.wire.translate import (
    batch_strategy_to_core,
    batch_strategy_to_wire,
    edge_draft_to_core,
    entity_draft_to_core,
    extraction_batch_to_core_result,
    node_role_to_core,
    node_role_to_wire,
)

__all__ = [
    "batch_strategy_to_core",
    "batch_strategy_to_wire",
    "edge_draft_to_core",
    "entity_draft_to_core",
    "extraction_batch_to_core_result",
    "node_role_to_core",
    "node_role_to_wire",
]
