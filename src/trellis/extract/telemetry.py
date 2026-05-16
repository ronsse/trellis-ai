"""Extractor fallback + failure telemetry.

Two distinct surfaces share this module by design:

* :func:`analyze_extractor_fallbacks` /
  :func:`analyze_extraction_validation` consume
  ``EXTRACTOR_FALLBACK`` and ``EXTRACTION_REJECTED`` events emitted by
  :class:`~trellis.extract.dispatcher.ExtractionDispatcher` and turn them
  into operator-readable reports (Gap 4.3 / Logic Gap 1.3).
* :func:`emit_extraction_failure` is the writer-side helper that replaces
  the silent ``except json.JSONDecodeError: return []`` defect in
  :class:`~trellis.extract.llm.LLMExtractor` and
  ``trellis_workers.learning.miner.PrecedentMiner._parse_candidates``.
  Callers emit-then-raise; the dispatcher is the one legitimate degrader.
  See ``docs/design/adr-extraction-failure-telemetry.md``.

Two fallback signals are tracked by the analyzers today:

* ``prefer_tier_override`` — caller forced a lower-priority tier.
* ``empty_result`` — chosen extractor ran but produced no drafts. Strongest
  single graduation signal ("deterministic silently fails for this source
  → promote to hybrid / LLM").

Per-source aggregates let consumers ask "where do rules keep losing?" and
"is this source stable enough to graduate down a tier?" without re-running
the dispatcher. The reports are read-only — proposing tier changes or
retiring extractors is left to operators (and, later, a dedicated tuning
rule that watches these aggregates).
"""

from __future__ import annotations

import os
import re
import threading
from collections import Counter, OrderedDict, defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# emit_extraction_failure — writer-side helper (ADR-extraction-failure-telemetry)
# ---------------------------------------------------------------------------

#: Canonical failure kinds for :func:`emit_extraction_failure`. The
#: ``Literal`` mirrors the ADR §2.1 event schema verbatim. Keep this in
#: sync with the analyzer that aggregates by ``failure_kind`` (Phase 2).
#:
#: ``batch_collector_error`` is the one slug that does *not* originate
#: inside the extraction pipeline. It is emitted by
#: :meth:`trellis_workers.enrichment.service.EnrichmentService.batch_enrich`
#: when ``asyncio.gather(..., return_exceptions=True)`` returns a raw
#: ``Exception`` from a task that bubbled past the per-item ``enrich``
#: failure-handling. We reuse :attr:`EventType.EXTRACTION_FAILED` rather
#: than minting a new event type so existing failure analyzers see these
#: rare collector-level escapes for free; analyzers that care about
#: pipeline-only failures can filter on
#: ``payload.failure_kind != "batch_collector_error"``.
ExtractionFailureKind = Literal[
    "parse_error",
    "validation_error",
    "policy_violation",
    "low_confidence",
    "tier_fallback",
    "model_error",
    "budget_exhausted",
    "batch_collector_error",
]

ExtractionTier = Literal["deterministic", "hybrid", "llm"]


class ExtractionFailureError(RuntimeError):
    """Raised by extractors after emitting an EXTRACTION_FAILED event.

    Carries the same ``failure_kind`` that landed on the event so that the
    dispatcher can record the original failure in its ``tier_fallback``
    event without re-deriving it.
    """

    def __init__(
        self,
        message: str,
        *,
        failure_kind: ExtractionFailureKind,
        extractor_id: str,
    ) -> None:
        super().__init__(message)
        self.failure_kind: ExtractionFailureKind = failure_kind
        self.extractor_id = extractor_id


# Sampling state — process-local LRU keyed by
# ``(extractor_id, prompt_hash, failure_kind)``. POC scope: in-process only;
# multi-process aggregation deferred (the analyzer reads from the EventLog,
# which is the shared substrate).
_SAMPLE_CAP_ENV = "EXTRACTION_FAILURE_SAMPLE_CAP"
_SAMPLE_BYPASS_ENV = "EXTRACTION_FAILURE_NO_SAMPLE"
_SAMPLE_LRU_MAX = 4096  # cap the in-process cache to keep RSS bounded


def _load_sample_cap() -> int:
    """Read and validate ``EXTRACTION_FAILURE_SAMPLE_CAP``.

    Read on every ``emit_extraction_failure`` call so operators can tune
    the cap without restarting the process. Misconfiguration (non-integer
    or negative value) raises — POC directive: loud on misuse.
    """
    raw = os.environ.get(_SAMPLE_CAP_ENV)
    if raw is None or raw == "":
        return 10
    try:
        value = int(raw)
    except ValueError as exc:
        msg = f"{_SAMPLE_CAP_ENV} must be a non-negative integer; got {raw!r}"
        raise ValueError(msg) from exc
    if value < 0:
        msg = f"{_SAMPLE_CAP_ENV} must be a non-negative integer; got {value}"
        raise ValueError(msg)
    return value


class _ClusterEntry:
    """Single-cluster counter for the sampling LRU.

    Kept as a lightweight mutable class (not a dataclass) so the LRU
    can update fields in place without re-inserting.
    """

    __slots__ = ("capped_count", "count", "last_event_id")

    def __init__(self) -> None:
        self.count: int = 0
        self.capped_count: int = 0
        self.last_event_id: str | None = None


class _SamplerState:
    """Process-local sampling state for :func:`emit_extraction_failure`.

    Counts how many full events we've emitted per
    ``(extractor_id, prompt_hash, failure_kind)`` triple. Once the cap is
    hit the helper still records the failure (so the analyzer can see it
    happened) by emitting an aggregate-only update on the most recent
    event_id rather than dropping the call. POC scope deliberately keeps
    this in-memory: the EventLog is the shared, persistent substrate.
    """

    def __init__(self, max_size: int = _SAMPLE_LRU_MAX) -> None:
        self._lock = threading.Lock()
        self._state: OrderedDict[tuple[str, str | None, str], _ClusterEntry] = (
            OrderedDict()
        )
        self._max_size = max_size

    def observe(
        self,
        cluster_key: tuple[str, str | None, str],
        cap: int,
    ) -> tuple[bool, int, int]:
        """Record an observation; return ``(emit_full, count, capped_count)``.

        ``emit_full`` is ``True`` when the current observation falls within
        the per-cluster cap; aggregate-only beyond that. ``count`` is the
        total observations (including this one); ``capped_count`` is the
        number suppressed so far (excluding this one — the caller can
        increment if it decides to suppress).
        """
        with self._lock:
            entry = self._state.get(cluster_key)
            if entry is None:
                entry = _ClusterEntry()
                self._state[cluster_key] = entry
            else:
                # Touch to refresh LRU recency.
                self._state.move_to_end(cluster_key)
            entry.count += 1
            count_val = entry.count
            emit_full = count_val <= cap
            capped_count = entry.capped_count
            if not emit_full:
                entry.capped_count = capped_count + 1
            # Evict oldest if over budget.
            while len(self._state) > self._max_size:
                self._state.popitem(last=False)
            return emit_full, count_val, capped_count

    def set_last_event_id(
        self,
        cluster_key: tuple[str, str | None, str],
        event_id: str,
    ) -> None:
        with self._lock:
            entry = self._state.get(cluster_key)
            if entry is not None:
                entry.last_event_id = event_id

    def get_last_event_id(
        self,
        cluster_key: tuple[str, str | None, str],
    ) -> str | None:
        with self._lock:
            entry = self._state.get(cluster_key)
            if entry is None:
                return None
            return entry.last_event_id

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


_SAMPLER = _SamplerState()


def reset_extraction_failure_state() -> None:
    """Test helper — clear the process-local sampling state.

    Use in ``pytest`` fixtures (typically ``autouse=True``) so per-test
    state doesn't leak. Not exported in ``__all__`` — internal contract.
    """
    _SAMPLER.reset()


# Redaction patterns. Conservative — false positives (over-redaction) are
# acceptable; false negatives (PII leak) are not. POC seed: email, UUID-
# shaped IDs, SSN-shaped 3-2-4 digits.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

_REDACTORS: list[tuple[re.Pattern[str], str]] = [
    (_EMAIL_RE, "[REDACTED_EMAIL]"),
    (_UUID_RE, "[REDACTED_UUID]"),
    (_SSN_RE, "[REDACTED_SSN]"),
]

_ERROR_EXCERPT_MAX = 200


def _redact(text: str) -> str:
    out = text
    for pattern, replacement in _REDACTORS:
        out = pattern.sub(replacement, out)
    return out


def emit_extraction_failure(
    *,
    event_log: EventLog | None,
    extractor_id: str,
    extractor_tier: ExtractionTier,
    failure_kind: ExtractionFailureKind,
    source_hint: str | None = None,
    prompt_hash: str | None = None,
    source_excerpt_hash: str | None = None,
    model: str | None = None,
    error_class: str,
    error_excerpt: str,
    correlation_id: str | None = None,
) -> None:
    """Emit an ``EXTRACTION_FAILED`` event with redaction + sampling.

    The helper is total: when ``event_log`` is ``None`` it is a no-op so
    extractors don't have to special-case "wired vs. unwired". Sampling is
    per-process and keyed by ``(extractor_id, prompt_hash, failure_kind)``;
    the first ``EXTRACTION_FAILURE_SAMPLE_CAP`` (default 10) observations
    in a cluster emit in full, subsequent observations record only an
    aggregate count delta. Set ``EXTRACTION_FAILURE_NO_SAMPLE=1`` in tests
    that need deterministic per-call assertions.

    See ``docs/design/adr-extraction-failure-telemetry.md`` §2 for the
    payload schema; this helper is the only place that constructs it.
    """
    from trellis.stores.base.event_log import EventType  # noqa: PLC0415

    if event_log is None:
        return

    # Cap then redact — the hard 200-char bound holds both before and
    # after redaction (replacements are bounded-length placeholders, but
    # they can grow text length slightly; we re-cap to be safe).
    excerpt = (error_excerpt or "")[:_ERROR_EXCERPT_MAX]
    excerpt = _redact(excerpt)[:_ERROR_EXCERPT_MAX]

    bypass = os.environ.get(_SAMPLE_BYPASS_ENV)
    if bypass and bypass not in {"0", "", "false", "False"}:
        emit_full = True
        count = 1
        capped_count = 0
        cluster_key = (extractor_id, prompt_hash, failure_kind)
    else:
        cap = _load_sample_cap()
        cluster_key = (extractor_id, prompt_hash, failure_kind)
        emit_full, count, capped_count = _SAMPLER.observe(cluster_key, cap)

    payload: dict[str, object] = {
        "extractor_id": extractor_id,
        "extractor_tier": extractor_tier,
        "failure_kind": failure_kind,
        "source_hint": source_hint,
        "prompt_hash": prompt_hash,
        "source_excerpt_hash": source_excerpt_hash,
        "model": model,
        "error_class": error_class,
        "error_excerpt": excerpt,
        "correlation_id": correlation_id,
        "cluster_count": count,
        "sampled": not emit_full,
    }

    if not emit_full:
        # Aggregate-only update — annotate the prior cluster event_id so
        # analyzers can join. POC scope: still appends a small event so
        # the count is queryable; this keeps the EventLog as the single
        # source of truth without needing a mutable counter.
        last_id = _SAMPLER.get_last_event_id(cluster_key)
        if last_id is not None:
            payload["aggregate_for_event_id"] = last_id
        payload["capped_count"] = capped_count + 1

    try:
        event = event_log.emit(
            EventType.EXTRACTION_FAILED,
            source="extraction_failure_helper",
            payload=payload,
        )
    # GRACEFUL-DEGRADATION: a broken event log must not break the
    # extractor's emit-then-raise contract. The caller will still raise;
    # the log already captured the failure at WARN.
    except Exception:
        logger.exception(
            "extraction_failure_emit_failed",
            extractor_id=extractor_id,
            failure_kind=failure_kind,
        )
        return

    if emit_full:
        _SAMPLER.set_last_event_id(cluster_key, event.event_id)


_FALLBACK_EVENT_LIMIT = 5000
_DISPATCH_EVENT_LIMIT = 5000
_VALIDATION_EVENT_LIMIT = 5000

#: Fraction of dispatches on a single source_hint that must fall back before
#: the source is flagged in findings. Deliberately high — we want a loud
#: signal, not alerts for every occasional override.
_HIGH_FALLBACK_RATE = 0.5
_MIN_SOURCE_SAMPLES = 10

#: Fraction of dispatches on a single source_hint that must hit
#: ``EXTRACTION_REJECTED`` before the source is flagged in findings.
#: Mirrors the fallback threshold — same loudness contract.
_HIGH_VALIDATION_RATE = 0.5


class SourceFallbackStats(TrellisModel):
    """Per-source-hint fallback aggregates."""

    source_hint: str
    total_dispatches: int
    fallback_events: int
    fallback_rate: float
    reasons: dict[str, int] = Field(default_factory=dict)
    chosen_tiers: dict[str, int] = Field(default_factory=dict)


class ExtractorFallbackReport(TrellisModel):
    """Aggregated extractor-fallback telemetry over an analysis window."""

    total_dispatches: int
    total_fallbacks: int
    overall_fallback_rate: float
    reason_counts: dict[str, int] = Field(default_factory=dict)
    per_source: list[SourceFallbackStats] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def analyze_extractor_fallbacks(
    event_log: EventLog,
    *,
    days: int = 30,
    limit: int = _FALLBACK_EVENT_LIMIT,
) -> ExtractorFallbackReport:
    """Summarize extractor-fallback telemetry since ``days`` ago.

    Reads ``EXTRACTOR_FALLBACK`` and ``EXTRACTION_DISPATCHED`` events to
    compute an overall fallback rate and per-source aggregates. When no
    dispatch events exist in the window, returns a zero-valued report with
    a descriptive note rather than raising.
    """
    from trellis.stores.base.event_log import EventType  # noqa: PLC0415

    since = datetime.now(tz=UTC) - timedelta(days=days)
    fallback_events = event_log.get_events(
        event_type=EventType.EXTRACTOR_FALLBACK,
        since=since,
        limit=limit,
    )
    dispatch_events = event_log.get_events(
        event_type=EventType.EXTRACTION_DISPATCHED,
        since=since,
        limit=_DISPATCH_EVENT_LIMIT,
    )

    total_fallbacks = len(fallback_events)
    total_dispatches = len(dispatch_events)

    reason_counts: Counter[str] = Counter()
    per_source_dispatches: Counter[str] = Counter()
    #: source_hint -> {"fallbacks": int, "reasons": Counter, "chosen_tiers": Counter}
    per_source_fallback: dict[str, dict] = defaultdict(
        lambda: {"fallbacks": 0, "reasons": Counter(), "chosen_tiers": Counter()}
    )

    for event in dispatch_events:
        source_hint = (event.payload or {}).get("source_hint") or "<none>"
        per_source_dispatches[source_hint] += 1

    for event in fallback_events:
        payload = event.payload or {}
        reason = payload.get("reason") or "unknown"
        source_hint = payload.get("source_hint") or "<none>"
        chosen_tier = payload.get("chosen_tier") or "unknown"
        reason_counts[reason] += 1
        entry = per_source_fallback[source_hint]
        entry["fallbacks"] += 1
        entry["reasons"][reason] += 1
        entry["chosen_tiers"][chosen_tier] += 1

    per_source: list[SourceFallbackStats] = []
    all_sources = set(per_source_dispatches) | set(per_source_fallback)
    for source_hint in sorted(all_sources):
        dispatches = per_source_dispatches.get(source_hint, 0)
        fallbacks = per_source_fallback.get(source_hint, {}).get("fallbacks", 0)
        rate = fallbacks / dispatches if dispatches else 0.0
        reasons = dict(per_source_fallback.get(source_hint, {}).get("reasons") or {})
        chosen_tiers = dict(
            per_source_fallback.get(source_hint, {}).get("chosen_tiers") or {}
        )
        per_source.append(
            SourceFallbackStats(
                source_hint=source_hint,
                total_dispatches=dispatches,
                fallback_events=fallbacks,
                fallback_rate=rate,
                reasons=reasons,
                chosen_tiers=chosen_tiers,
            )
        )

    overall_rate = total_fallbacks / total_dispatches if total_dispatches else 0.0

    findings = _build_findings(per_source)
    notes: list[str] = []
    if total_dispatches == 0:
        notes.append(
            "No EXTRACTION_DISPATCHED events in this window. Either the "
            "dispatcher is unused or no ``event_log`` is wired — check "
            "``ExtractionDispatcher(event_log=...)`` at construction."
        )

    report = ExtractorFallbackReport(
        total_dispatches=total_dispatches,
        total_fallbacks=total_fallbacks,
        overall_fallback_rate=overall_rate,
        reason_counts=dict(reason_counts),
        per_source=per_source,
        findings=findings,
        notes=notes,
    )
    logger.info(
        "extractor_fallbacks_analyzed",
        days=days,
        total_dispatches=total_dispatches,
        total_fallbacks=total_fallbacks,
        overall_rate=overall_rate,
    )
    return report


def _build_findings(per_source: list[SourceFallbackStats]) -> list[str]:
    findings: list[str] = []
    for stats in per_source:
        if stats.total_dispatches < _MIN_SOURCE_SAMPLES:
            continue
        if stats.fallback_rate < _HIGH_FALLBACK_RATE:
            continue
        top_reason = max(stats.reasons.items(), key=lambda kv: kv[1])[0]
        if top_reason == "empty_result":
            findings.append(
                f"source `{stats.source_hint}` falls back "
                f"{stats.fallback_rate:.0%} of dispatches ({stats.fallback_events}"
                f" of {stats.total_dispatches}) with top reason `empty_result` — "
                "the chosen extractor keeps producing no drafts; candidate "
                "for graduation (register a hybrid/LLM extractor, or "
                "retire the rule set)."
            )
        elif top_reason == "prefer_tier_override":
            findings.append(
                f"source `{stats.source_hint}` overrides to a lower tier "
                f"{stats.fallback_rate:.0%} of the time — either the default "
                "priority is wrong for this source, or callers are routinely "
                "opting out of deterministic. Worth auditing the callsite."
            )
        else:
            findings.append(
                f"source `{stats.source_hint}` falls back "
                f"{stats.fallback_rate:.0%} (top reason: {top_reason})."
            )
    return findings


class SourceValidationStats(TrellisModel):
    """Per-source-hint extraction-validation aggregates."""

    source_hint: str
    total_dispatches: int
    rejected_events: int
    rejection_rate: float
    codes: dict[str, int] = Field(default_factory=dict)
    extractors: dict[str, int] = Field(default_factory=dict)


class ExtractionValidationReport(TrellisModel):
    """Aggregated EXTRACTION_REJECTED telemetry over an analysis window."""

    total_dispatches: int
    total_rejected: int
    overall_rejection_rate: float
    code_counts: dict[str, int] = Field(default_factory=dict)
    per_source: list[SourceValidationStats] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def analyze_extraction_validation(
    event_log: EventLog,
    *,
    days: int = 30,
    limit: int = _VALIDATION_EVENT_LIMIT,
) -> ExtractionValidationReport:
    """Summarize extraction-validation telemetry since ``days`` ago.

    Reads ``EXTRACTION_REJECTED`` and ``EXTRACTION_DISPATCHED`` events to
    compute an overall rejection rate, per-validator-code counts, and
    per-source aggregates. Mirrors the shape of
    :func:`analyze_extractor_fallbacks` so consumers can swap one for the
    other when wiring CLI surfaces. Closes Logic Gap 1.3 telemetry.
    """
    from trellis.stores.base.event_log import EventType  # noqa: PLC0415

    since = datetime.now(tz=UTC) - timedelta(days=days)
    rejected_events = event_log.get_events(
        event_type=EventType.EXTRACTION_REJECTED,
        since=since,
        limit=limit,
    )
    dispatch_events = event_log.get_events(
        event_type=EventType.EXTRACTION_DISPATCHED,
        since=since,
        limit=_DISPATCH_EVENT_LIMIT,
    )

    total_rejected = len(rejected_events)
    total_dispatches = len(dispatch_events)

    code_counts: Counter[str] = Counter()
    per_source_dispatches: Counter[str] = Counter()
    per_source_rejected: dict[str, dict] = defaultdict(
        lambda: {"rejected": 0, "codes": Counter(), "extractors": Counter()}
    )

    for event in dispatch_events:
        source_hint = (event.payload or {}).get("source_hint") or "<none>"
        per_source_dispatches[source_hint] += 1

    for event in rejected_events:
        payload = event.payload or {}
        source_hint = payload.get("source_hint") or "<none>"
        extractor_used = payload.get("extractor_used") or "unknown"
        entry = per_source_rejected[source_hint]
        entry["rejected"] += 1
        entry["extractors"][extractor_used] += 1
        for finding in payload.get("findings") or []:
            code = (finding or {}).get("code") or "unknown"
            code_counts[code] += 1
            entry["codes"][code] += 1

    per_source: list[SourceValidationStats] = []
    all_sources = set(per_source_dispatches) | set(per_source_rejected)
    for source_hint in sorted(all_sources):
        dispatches = per_source_dispatches.get(source_hint, 0)
        rejected = per_source_rejected.get(source_hint, {}).get("rejected", 0)
        rate = rejected / dispatches if dispatches else 0.0
        codes = dict(per_source_rejected.get(source_hint, {}).get("codes") or {})
        extractors = dict(
            per_source_rejected.get(source_hint, {}).get("extractors") or {}
        )
        per_source.append(
            SourceValidationStats(
                source_hint=source_hint,
                total_dispatches=dispatches,
                rejected_events=rejected,
                rejection_rate=rate,
                codes=codes,
                extractors=extractors,
            )
        )

    overall_rate = total_rejected / total_dispatches if total_dispatches else 0.0

    findings = _build_validation_findings(per_source)
    notes: list[str] = []
    if total_dispatches == 0 and total_rejected == 0:
        notes.append(
            "No EXTRACTION_DISPATCHED or EXTRACTION_REJECTED events in this "
            "window. Either the dispatcher is unused, no ``event_log`` is "
            "wired, or no validators are configured — check "
            "``ExtractionDispatcher(event_log=..., validators=[...])``."
        )

    report = ExtractionValidationReport(
        total_dispatches=total_dispatches,
        total_rejected=total_rejected,
        overall_rejection_rate=overall_rate,
        code_counts=dict(code_counts),
        per_source=per_source,
        findings=findings,
        notes=notes,
    )
    logger.info(
        "extraction_validation_analyzed",
        days=days,
        total_dispatches=total_dispatches,
        total_rejected=total_rejected,
        overall_rate=overall_rate,
    )
    return report


def _build_validation_findings(
    per_source: list[SourceValidationStats],
) -> list[str]:
    findings: list[str] = []
    for stats in per_source:
        if stats.total_dispatches < _MIN_SOURCE_SAMPLES:
            continue
        if stats.rejection_rate < _HIGH_VALIDATION_RATE:
            continue
        top_code = (
            max(stats.codes.items(), key=lambda kv: kv[1])[0]
            if stats.codes
            else "unknown"
        )
        findings.append(
            f"source `{stats.source_hint}` has extractions rejected "
            f"{stats.rejection_rate:.0%} of dispatches ({stats.rejected_events}"
            f" of {stats.total_dispatches}) — top finding code "
            f"`{top_code}`. Investigate the extractor or the validator "
            "ruleset before junk silently degrades the corpus."
        )
    return findings


__all__ = [
    "ExtractionFailureError",
    "ExtractionFailureKind",
    "ExtractionTier",
    "ExtractionValidationReport",
    "ExtractorFallbackReport",
    "SourceFallbackStats",
    "SourceValidationStats",
    "analyze_extraction_validation",
    "analyze_extractor_fallbacks",
    "emit_extraction_failure",
]
