"""Write-behaviour configuration — one home for the ingest-time knobs.

The knobs below each grew up next to the code path they gate, so the
semantics a given write received depended on which wrapper on which host
performed it — and nothing recorded that.  Two documents written a minute
apart could get entirely different enrichment with no way to tell them
apart afterwards.

This module is the single place that reads them.  Each feature module
keeps its public reader (:func:`trellis.classify.ingest.classify_on_ingest_enabled`
and friends) as the call-site-facing name, but the reader now delegates
here, so there is exactly one parsing rule per knob and one place to add
the next one.

**Consolidation, not a behaviour change** — with one visible exception.
Every environment variable name, every default, and every parsing quirk
(unset-and-blank both mean "off"; an out-of-range confidence floor
degrades to "no gate" with a warning rather than to ``0.0``) is what
shipped before, because deployments already depend on them.  What did
change is where the malformed-value warnings surface: they now carry this
module's logger name rather than ``trellis.extract.trace_ingest_hook``,
and they are emitted once per distinct value per process rather than once
per read (see :func:`_parse_min_confidence`).

Reads stay live against :data:`os.environ` — no caching — so a test that
monkeypatches a variable still sees the change, exactly as before.  The
*stamp* written onto events is snapshotted once per process; see
:mod:`trellis.core.write_provenance`.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Spellings that read as "on".  Shared by every boolean knob below —
#: the five feature modules each had their own identical copy.
TRUTHY = frozenset({"1", "true", "yes", "on"})

# ---------------------------------------------------------------------------
# Environment variable names.  These are the live contract — they are set in
# deployed wrappers and compose files, so the strings never change; the
# feature modules import them from here so there is one spelling of each.
# ---------------------------------------------------------------------------

#: Classify documents at write time (``trellis.classify.ingest``).
CLASSIFY_ON_INGEST_FLAG = "TRELLIS_ENABLE_CLASSIFY_ON_INGEST"

#: Embed documents at write time (``trellis.retrieve.embed_ingest_hook``).
EMBED_ON_INGEST_FLAG = "TRELLIS_ENABLE_EMBED_ON_INGEST"

#: Mine entities out of saved memories (``trellis.extract.memory_ingest_hook``).
MEMORY_EXTRACTION_FLAG = "TRELLIS_ENABLE_MEMORY_EXTRACTION"

#: Model-judged ADD/UPDATE/SUPERSEDE/NOOP verdict at capture
#: (``trellis.mcp.reconcile``).
RECONCILE_FLAG_ENV = "TRELLIS_ENABLE_RECONCILE_ON_WRITE"

#: Extract entities from ingested traces (``trellis.extract.trace_ingest_hook``).
TRACE_EXTRACTION_FLAG = "TRELLIS_ENABLE_TRACE_EXTRACTION"

#: Optional confidence floor applied to trace-extraction drafts.
TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG = "TRELLIS_TRACE_EXTRACTION_MIN_CONFIDENCE"

#: Override for the verdict model identifier labelled on reconcile events.
RECONCILE_MODEL_ENV = "TRELLIS_RECONCILE_MODEL"

#: Per-verdict timeout, in seconds, for the reconcile tier.
RECONCILE_TIMEOUT_ENV = "TRELLIS_RECONCILE_TIMEOUT_S"

# ---------------------------------------------------------------------------
# Defaults.  Unchanged from the per-module values they were lifted from.
# ---------------------------------------------------------------------------

#: Default verdict model — a small local model over an OpenAI-compatible
#: endpoint (Ollama), per the guide's north-star ladder.
DEFAULT_RECONCILE_MODEL = "hermes3:8b"

#: Default verdict timeout in seconds.
DEFAULT_RECONCILE_TIMEOUT_S = 20.0


def _truthy(env: Mapping[str, str], name: str) -> bool:
    """``True`` iff ``name`` is set to a truthy spelling."""
    return env.get(name, "").strip().lower() in TRUTHY


@functools.lru_cache(maxsize=8)
def _parse_min_confidence(raw: str) -> float | None:
    """Parse one confidence-floor spelling, warning at most once for it.

    Cached on the raw string, not on "have I run yet": a test that
    monkeypatches the variable still gets a fresh parse, but a deployment
    with one typo'd value logs one warning per process instead of one per
    ingested document.  Every write-behaviour reader now goes through
    :meth:`WriteBehaviourConfig.from_env`, so an uncached warning here
    would fire on flag reads that have nothing to do with this knob.
    """
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "trace_extraction_min_confidence_unparseable",
            flag=TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG,
            value=raw,
        )
        return None
    if not 0.0 <= value <= 1.0:
        logger.warning(
            "trace_extraction_min_confidence_out_of_range",
            flag=TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG,
            value=value,
        )
        return None
    return value


def _min_confidence(env: Mapping[str, str]) -> float | None:
    """Confidence floor from the environment, or ``None`` for no gate.

    Unset / blank means **off**: every draft the extractor produced is
    submitted, which is what an existing deployment already gets.  A gate
    that silently drops extraction output has to be asked for.

    An unparseable or out-of-range value is treated as unset (with a
    warning) rather than as ``0.0`` — misreading "0.85" as "drop nothing"
    is recoverable, misreading it as "drop everything" is not.
    """
    raw = env.get(TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG, "").strip()
    return _parse_min_confidence(raw) if raw else None


def _reconcile_timeout(env: Mapping[str, str]) -> float:
    """Per-verdict timeout, defaulting on absent / invalid / non-positive."""
    raw = env.get(RECONCILE_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_RECONCILE_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_RECONCILE_TIMEOUT_S
    return value if value > 0 else DEFAULT_RECONCILE_TIMEOUT_S


@dataclass(frozen=True, slots=True)
class WriteBehaviourConfig:
    """The write semantics a process is applying, as one value.

    Field defaults are the *no environment set at all* configuration, so
    ``WriteBehaviourConfig()`` is by construction the shipped default —
    a test can assert against it without restating eight literals.
    """

    classify_on_ingest: bool = False
    embed_on_ingest: bool = False
    memory_extraction: bool = False
    reconcile_on_write: bool = False
    trace_extraction: bool = False
    trace_extraction_min_confidence: float | None = None
    reconcile_model: str = DEFAULT_RECONCILE_MODEL
    reconcile_timeout_s: float = DEFAULT_RECONCILE_TIMEOUT_S

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WriteBehaviourConfig:
        """Read the effective configuration from ``env`` (default ``os.environ``)."""
        src: Mapping[str, str] = os.environ if env is None else env
        return cls(
            classify_on_ingest=_truthy(src, CLASSIFY_ON_INGEST_FLAG),
            embed_on_ingest=_truthy(src, EMBED_ON_INGEST_FLAG),
            memory_extraction=_truthy(src, MEMORY_EXTRACTION_FLAG),
            reconcile_on_write=_truthy(src, RECONCILE_FLAG_ENV),
            trace_extraction=_truthy(src, TRACE_EXTRACTION_FLAG),
            trace_extraction_min_confidence=_min_confidence(src),
            reconcile_model=src.get(RECONCILE_MODEL_ENV, "").strip()
            or DEFAULT_RECONCILE_MODEL,
            reconcile_timeout_s=_reconcile_timeout(src),
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe mapping, field name → effective value."""
        return asdict(self)

    def describe(self) -> list[dict[str, Any]]:
        """Per-knob report rows for the operator surface.

        Each row carries the field name, the environment variable that
        drives it, the effective value, the shipped default, and whether
        the effective value differs from that default — which is the one
        column an operator comparing two hosts actually reads.
        """
        defaults = WriteBehaviourConfig()
        effective = self.as_dict()
        default_values = defaults.as_dict()
        return [
            {
                "name": name,
                "env_var": ENV_VAR_BY_FIELD[name],
                "value": effective[name],
                "default": default_values[name],
                "overridden": effective[name] != default_values[name],
            }
            for name in effective
        ]


#: Field name → the environment variable that drives it.  Declared after
#: the dataclass so :meth:`WriteBehaviourConfig.describe` can annotate its
#: rows, and so a new field without an env var fails loudly on first use
#: rather than silently reporting nothing.
ENV_VAR_BY_FIELD: dict[str, str] = {
    "classify_on_ingest": CLASSIFY_ON_INGEST_FLAG,
    "embed_on_ingest": EMBED_ON_INGEST_FLAG,
    "memory_extraction": MEMORY_EXTRACTION_FLAG,
    "reconcile_on_write": RECONCILE_FLAG_ENV,
    "trace_extraction": TRACE_EXTRACTION_FLAG,
    "trace_extraction_min_confidence": TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG,
    "reconcile_model": RECONCILE_MODEL_ENV,
    "reconcile_timeout_s": RECONCILE_TIMEOUT_ENV,
}


__all__ = [
    "CLASSIFY_ON_INGEST_FLAG",
    "DEFAULT_RECONCILE_MODEL",
    "DEFAULT_RECONCILE_TIMEOUT_S",
    "EMBED_ON_INGEST_FLAG",
    "ENV_VAR_BY_FIELD",
    "MEMORY_EXTRACTION_FLAG",
    "RECONCILE_FLAG_ENV",
    "RECONCILE_MODEL_ENV",
    "RECONCILE_TIMEOUT_ENV",
    "TRACE_EXTRACTION_FLAG",
    "TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG",
    "TRUTHY",
    "WriteBehaviourConfig",
]
