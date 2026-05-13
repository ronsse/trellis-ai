"""Trellis meta-trace machinery.

Public surface:

* :func:`record_meta_analysis` — context manager that records a
  Trellis-internal analysis run as a graph ``Activity`` node, with
  ``wasInformedBy`` edges to consumed events / Observations and
  ``wasGeneratedBy`` edges from produced findings (Item 6 Phase 0).
* :class:`MetaAnalysisRecord` — handle yielded from the recorder for
  adding consumed-input and produced-output edges inside the
  ``with`` block.
* :func:`ensure_meta_agent` — idempotent synthetic-Agent-node factory.
* :func:`reservoir_sample` — first/last/middle sampling helper for
  bounding edge fan-out when an analyzer consumes thousands of inputs.

Env var: :data:`META_TRACES_ENV_VAR` (``TRELLIS_META_TRACES``) accepts
``"on"`` / ``"off"``; anything else raises at recorder entry per the
POC directive (``docs/design/plan-self-improvement-program.md`` §2).

See ``docs/design/plan-dogfooding-meta-traces.md`` and
``docs/design/adr-dogfooding-meta-traces.md`` for the design.
"""

from __future__ import annotations

from trellis.meta.agents import (
    DEFAULT_META_AGENT_ID,
    META_AGENT_PREFIX,
    ensure_meta_agent,
)
from trellis.meta.recorder import (
    DEFAULT_MERGE_WINDOW_SECONDS,
    META_TRACES_ENV_VAR,
    MetaAnalysisRecord,
    record_meta_analysis,
)
from trellis.meta.sampling import (
    DEFAULT_FIRST,
    DEFAULT_LAST,
    DEFAULT_MIDDLE,
    reservoir_sample,
)

__all__ = [
    "DEFAULT_FIRST",
    "DEFAULT_LAST",
    "DEFAULT_MERGE_WINDOW_SECONDS",
    "DEFAULT_META_AGENT_ID",
    "DEFAULT_MIDDLE",
    "META_AGENT_PREFIX",
    "META_TRACES_ENV_VAR",
    "MetaAnalysisRecord",
    "ensure_meta_agent",
    "record_meta_analysis",
    "reservoir_sample",
]
