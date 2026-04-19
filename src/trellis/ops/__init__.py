"""Ops helpers — raw signal recording for the self-learning loop.

The ``ops`` layer is the thin wiring between tuneable components and
the ops-tier stores (:class:`OutcomeStore`, :class:`ParameterStore`,
:class:`TunerStateStore`).  Components call :func:`record_outcome` on
every governed call; tuners consume outcomes and propose parameter
updates through the governed mutation pipeline.
"""

from trellis.ops.recording import record_outcome

__all__ = ["record_outcome"]
