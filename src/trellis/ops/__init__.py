"""Ops helpers — raw signal recording for the feedback loop.

The ``ops`` layer is the thin wiring between tuneable components and
the ops-tier stores (:class:`OutcomeStore`, :class:`ParameterStore`,
:class:`TunerStateStore`).  Components call :func:`record_outcome` on
every governed call; tuners consume outcomes and propose parameter
updates through the governed mutation pipeline.
"""

from trellis.ops.recording import record_outcome
from trellis.ops.registry import ParameterRegistry
from trellis.ops.write_health import (
    BackendHealthReport,
    ServeAttributionReport,
    WriteHealthReport,
    classify_rejection,
    hints_for_trace_rejections,
    record_write_rejection,
    summarize_backend_health,
    summarize_serve_attribution,
    summarize_write_health,
)

__all__ = [
    "BackendHealthReport",
    "ParameterRegistry",
    "ServeAttributionReport",
    "WriteHealthReport",
    "classify_rejection",
    "hints_for_trace_rejections",
    "record_outcome",
    "record_write_rejection",
    "summarize_backend_health",
    "summarize_serve_attribution",
    "summarize_write_health",
]
