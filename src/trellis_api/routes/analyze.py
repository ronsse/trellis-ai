"""Read-only HTTP surface for ``trellis analyze`` reports.

Gap G15 (and the backend half of G9): the CLI can answer "are our
learning loops performing or doing anything?" through ``trellis analyze
<report>`` (``src/trellis_cli/analyze.py``); the UI could not ask the same
question at all. Every report below calls the *same* library function the
CLI command calls — never a subprocess, never a re-derivation of its
logic — so a route and its CLI counterpart can only disagree about
transport, never about arithmetic.

**Allowlist, not a generic dispatcher.** ``_REPORTS`` is the single
source of truth for which reports exist over HTTP; an unknown name is a
404 naming every allowed one (never a 500 from an unhandled import).
Deliberately excluded, both named by the brief:

* ``replay`` — heavy, takes a change spec rather than bounded query params.
* ``generate-advisories`` / ``apply-noise-tags`` — write paths; this
  router is read-scope only end to end.
* ``pack-quality`` — its only entry point (``evaluate_pack`` /
  ``trellis analyze pack-quality``) requires a ``--scenarios`` YAML file
  that must already exist on local disk, with no built-in default
  scenario set shipped anywhere in product code
  (``docs/agent-guide/pack-quality-evaluation.md``). There is no bounded
  query-param shape for "a path on the server's filesystem", so this is a
  premise refutation rather than a report to wire up: see the PR body.

**Finite JSON, always.** The repo already refuses non-finite floats on
every store write; this module is the read-side counterpart. Every
response passes through :func:`_finite` before it reaches
``JSONResponse``, which independently also sets ``allow_nan=False`` — a
latent ``inf``/``nan`` becomes ``null`` rather than crashing the request
or leaking a non-JSON token onto the wire.

**Caching.** A per-process, 60s TTL cache (``_CACHE``) covers every
report: several of them aggregate the whole requested window per
request, and the cost is the same shape report to report, so one cache
keyed by ``(report name, registry identity, params)`` is simpler than
deciding per report whether it is "slow enough" to need one. Every
response names ``computed_at`` (when the underlying report was actually
computed) and ``cached`` (whether this response reused that computation)
so a caller can tell a stale read from a fresh one.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError, field_validator

from trellis.analyze.domains import analyze_domains
from trellis.analyze.holdout import OUTCOMES, HoldoutAnalysisError, analyze_holdout
from trellis.core.base import TrellisModel
from trellis.extract.telemetry import analyze_extractor_fallbacks
from trellis.learning import (
    analyze_learning_observations,
    build_learning_observations_from_event_log,
)
from trellis.learning.judged_outcomes import summarize_judged_outcomes
from trellis.retrieve.effectiveness import analyze_advisory_effectiveness
from trellis.retrieve.evaluate import analyze_dimension_predictiveness
from trellis.retrieve.pack_value import summarize_pack_value
from trellis.retrieve.telemetry import analyze_pack_telemetry
from trellis.retrieve.token_usage import analyze_token_usage
from trellis.retrieve.trellis_cost import summarize_trellis_cost
from trellis.stores.advisory_source import load_advisory_store
from trellis.stores.registry import StoreRegistry
from trellis_api.app import get_registry

router = APIRouter()

_CACHE_TTL_SECONDS = 60.0
#: (report, id(registry), params_json) -> (monotonic_time, computed_at_iso, data)
_CACHE: dict[tuple[str, int, str], tuple[float, str, dict[str, Any]]] = {}


def _finite(obj: Any) -> Any:
    """Recursively replace a non-finite float with ``None``.

    Every library report already types an unmeasured quantity as
    ``float | None`` and returns ``None`` for it — this is defense in
    depth against a latent bug upstream, not the primary mechanism. A
    value that had to be replaced here is itself a signal something
    upstream returned ``inf``/``nan`` instead of ``None``, which is why
    callers should treat a non-empty return from this function's sibling
    check (see tests) as worth investigating, not just worth hiding.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_finite(v) for v in obj]
    return obj


def _cache_get(
    report: str, registry: StoreRegistry, params_key: str
) -> tuple[str, dict[str, Any]] | None:
    entry = _CACHE.get((report, id(registry), params_key))
    if entry is None:
        return None
    computed_monotonic, computed_at_iso, data = entry
    if time.monotonic() - computed_monotonic > _CACHE_TTL_SECONDS:
        return None
    return computed_at_iso, data


def _cache_set(
    report: str, registry: StoreRegistry, params_key: str, data: dict[str, Any]
) -> str:
    computed_at_iso = datetime.now(UTC).isoformat()
    # Drop expired entries on every write so the cache holds at most one
    # TTL window of distinct (report, params) keys rather than every key
    # ever requested over the process's lifetime.
    now = time.monotonic()
    for key in [k for k, v in _CACHE.items() if now - v[0] > _CACHE_TTL_SECONDS]:
        del _CACHE[key]
    _CACHE[(report, id(registry), params_key)] = (
        time.monotonic(),
        computed_at_iso,
        data,
    )
    return computed_at_iso


class _OutcomeValidatorMixin:
    """Shared ``outcome`` validator for :class:`HoldoutParams`.

    Checked against :data:`trellis.analyze.holdout.OUTCOMES` rather than a
    hand-copied literal tuple, so the allowed set can never drift from the
    one the library function itself accepts.
    """

    @field_validator("outcome")
    @classmethod
    def _outcome_known(cls, value: str) -> str:
        if value not in OUTCOMES:
            msg = f"outcome must be one of: {', '.join(sorted(OUTCOMES))}"
            raise ValueError(msg)
        return value


# ---------------------------------------------------------------------------
# Per-report bounded param DTOs. extra="forbid" (via TrellisModel) rejects an
# unrecognized query param with a 422 rather than silently ignoring it.
# ---------------------------------------------------------------------------


class DaysLimitParams(TrellisModel):
    days: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=5000, ge=1, le=50_000)


class DaysOnlyParams(TrellisModel):
    days: int = Field(default=30, ge=1, le=365)


class ValueParams(TrellisModel):
    days: int = Field(default=30, ge=1, le=365)
    model: str | None = Field(default=None, max_length=200)
    price_per_mtok: float | None = Field(default=None, ge=0, le=100_000)


class CostParams(ValueParams):
    days: int = Field(default=7, ge=1, le=365)
    limit: int = Field(default=5000, ge=1, le=50_000)


class JudgedOutcomesParams(DaysLimitParams):
    days: int = Field(default=30, ge=1, le=365)


class AdvisoryEffectivenessParams(TrellisModel):
    # suppress_below / blend_weight are CLI params too, but they only
    # affect run_advisory_fitness_loop (the WRITE path: it adjusts and
    # suppresses stored confidence). This route always calls the read-only
    # analyze_advisory_effectiveness, which does not accept them, so they
    # are deliberately not fields here — accepting-but-ignoring a knob
    # would be its own kind of untruthful response.
    days: int = Field(default=30, ge=1, le=365)
    min_presentations: int = Field(default=3, ge=1, le=10_000)


class DimensionPredictivenessParams(TrellisModel):
    days: int = Field(default=30, ge=1, le=365)
    success_threshold: float = Field(default=0.5, ge=0, le=1)
    limit: int = Field(default=5000, ge=1, le=50_000)


class DomainsParams(TrellisModel):
    days: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=1000, ge=1, le=50_000)


class LearningCandidatesParams(TrellisModel):
    days: int = Field(default=30, ge=1, le=365)
    min_support: int = Field(default=2, ge=1, le=10_000)


# `dict[str, Any]`, not the narrower inferred `dict[str, list[str]]`: pydantic's
# `JsonDict` value union is invariant on `list[...]`, so a bare `list[str]`
# does not satisfy `list[JsonValue]` on its own for mypy.
_OUTCOME_SCHEMA_EXTRA: dict[str, Any] = {"enum": sorted(OUTCOMES)}


class HoldoutParams(_OutcomeValidatorMixin, TrellisModel):
    days: int = Field(default=60, ge=1, le=365)
    until: str | None = Field(default=None, max_length=64)
    rate: float | None = Field(default=None, ge=0.0, le=1.0)
    # `json_schema_extra` reads from the same `OUTCOMES` tuple
    # `_OutcomeValidatorMixin` validates against above, rather than a
    # hand-copied literal list, so the UI's select options can never drift
    # from what the field validator actually accepts.
    outcome: str = Field(
        default="log1p_turns",
        max_length=32,
        json_schema_extra=_OUTCOME_SCHEMA_EXTRA,
    )
    itt: bool = Field(default=False)
    permutations: int = Field(default=10_000, ge=1, le=50_000)
    bootstraps: int = Field(default=4_000, ge=1, le=20_000)
    power_sims: int = Field(default=600, ge=1, le=5_000)
    planned_mde: float = Field(default=0.33, gt=0, le=100)
    planned_n: int = Field(default=114, ge=1, le=100_000)
    settle_hours: float = Field(default=3.0, ge=0, le=168)
    seed: int = Field(default=20261004, ge=0)
    limit: int = Field(default=5000, ge=1, le=50_000)


# ---------------------------------------------------------------------------
# Handlers. Each calls the exact library function `trellis analyze <report>`
# calls, with the request's own registry/event log — no subprocess, no CLI
# re-implementation.
# ---------------------------------------------------------------------------


def _parse_window_end(value: str | None) -> datetime | None:
    if value is None:
        return None
    from trellis.analyze.holdout import HoldoutAnalysisError as _Err  # noqa: PLC0415

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"--until {value!r} is not a valid ISO-8601 timestamp."
        raise _Err(msg) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _run_holdout(params: HoldoutParams, registry: StoreRegistry) -> dict[str, Any]:
    event_log = registry.operational.event_log
    try:
        report = analyze_holdout(
            event_log,
            days=params.days,
            until=_parse_window_end(params.until),
            rate=params.rate,
            outcome=params.outcome,
            itt=params.itt,
            permutations=params.permutations,
            bootstraps=params.bootstraps,
            power_sims=params.power_sims,
            planned_mde=params.planned_mde,
            planned_n=params.planned_n,
            settle_hours=params.settle_hours,
            seed=params.seed,
            limit=params.limit,
        )
    except HoldoutAnalysisError as exc:
        return {"status": "error", "message": str(exc)}
    return report.model_dump()


def _run_judged_outcomes(
    params: JudgedOutcomesParams, registry: StoreRegistry
) -> dict[str, Any]:
    report = summarize_judged_outcomes(
        registry.operational.event_log, days=params.days, limit=params.limit
    )
    return report.model_dump()


def _run_advisory_effectiveness(
    params: AdvisoryEffectivenessParams, registry: StoreRegistry
) -> dict[str, Any]:
    store = load_advisory_store(registry.stores_dir, surface="api.analyze")
    if store is None:
        return {
            "status": "insufficient_data",
            "reason": "no stores_dir configured for this deployment",
        }
    # Same gate the CLI's advisory-effectiveness and generate-advisories
    # commands use (`_exit_if_advisory_store_degraded`): a degraded file
    # scores a set that is missing whatever failed to parse, so report the
    # degradation itself rather than a silently-incomplete measurement.
    degradation = store.degradation
    if degradation is not None:
        return {"status": "degraded", "store_degradation": degradation.to_dict()}
    # ``advisory_store`` is accepted but unused inside
    # analyze_advisory_effectiveness (kept for API symmetry with the write
    # path, run_advisory_fitness_loop) — this call is read-only by
    # construction: it never reaches run_advisory_fitness_loop, which is
    # the only one of the two that writes confidence back to the store.
    report = analyze_advisory_effectiveness(
        registry.operational.event_log,
        store,
        days=params.days,
        min_presentations=params.min_presentations,
    )
    return report.model_dump()


def _run_value(params: ValueParams, registry: StoreRegistry) -> dict[str, Any]:
    report = summarize_pack_value(
        registry.operational.event_log,
        days=params.days,
        model=params.model,
        price_per_mtok=params.price_per_mtok,
    )
    return report.model_dump()


def _run_dimension_predictiveness(
    params: DimensionPredictivenessParams, registry: StoreRegistry
) -> dict[str, Any]:
    report = analyze_dimension_predictiveness(
        registry.operational.event_log,
        days=params.days,
        success_threshold=params.success_threshold,
        limit=params.limit,
    )
    return report.model_dump()


def _run_learning_candidates(
    params: LearningCandidatesParams, registry: StoreRegistry
) -> dict[str, Any]:
    # Imported at call depth (like admin.py's code-proposal routes do) to
    # avoid pulling the Typer-bearing CLI module in at API import time.
    from trellis_cli.analyze import _build_learning_registry  # noqa: PLC0415

    try:
        param_registry = _build_learning_registry()
    except Exception as exc:
        return {"status": "insufficient_data", "reason": str(exc)}

    event_log = registry.operational.event_log
    observations = build_learning_observations_from_event_log(
        event_log, days=params.days
    )
    report = analyze_learning_observations(
        observations=observations,
        registry=param_registry,
        min_support=params.min_support,
    )
    # Deliberately NOT trellis.learning.scoring.write_learning_review_artifacts:
    # that function writes two files under the learning-artifacts directory
    # (the same one the Review queue reads) and mutates `report` in place to
    # add a readable-name fallback + a recomputed `promotion_ready` digest.
    # A GET route must not write, and must not race the Review queue's own
    # reader/writer over that directory — so this payload is the pure
    # aggregator's output: no `promotion_ready`, no `candidates_path`, and
    # candidate names fall back to bare item ids rather than the
    # document-store-backed readable name. See the PR body for the full
    # deviation note.
    report["note"] = (
        "read-only view: no candidates/decisions file was written, "
        "'promotion_ready' and readable precedent names are computed only "
        "by 'trellis analyze learning-candidates' or the nightly worker"
    )
    return report


def _run_domains(params: DomainsParams, registry: StoreRegistry) -> dict[str, Any]:
    report = analyze_domains(
        registry.operational.trace_store,
        registry.knowledge.document_store,
        registry.operational.event_log,
        days=params.days,
        scan_limit=params.limit,
    )
    return report.to_payload()


def _run_extractor_fallbacks(
    params: DaysLimitParams, registry: StoreRegistry
) -> dict[str, Any]:
    report = analyze_extractor_fallbacks(
        registry.operational.event_log, days=params.days, limit=params.limit
    )
    return report.model_dump()


def _run_pack_telemetry(
    params: DaysLimitParams, registry: StoreRegistry
) -> dict[str, Any]:
    report = analyze_pack_telemetry(
        registry.operational.event_log, days=params.days, limit=params.limit
    )
    return report.model_dump()


def _run_token_usage(
    params: DaysLimitParams, registry: StoreRegistry
) -> dict[str, Any]:
    report = analyze_token_usage(
        registry.operational.event_log, days=params.days, limit=params.limit
    )
    return report.model_dump()


def _run_cost(params: CostParams, registry: StoreRegistry) -> dict[str, Any]:
    report = summarize_trellis_cost(
        registry.operational.event_log,
        days=params.days,
        model=params.model,
        price_per_mtok=params.price_per_mtok,
        limit=params.limit,
    )
    return report.model_dump()


@dataclass(frozen=True)
class _ReportEntry:
    # Human-readable label for the Reports tab's picker; the dict key
    # (the raw report name the route and CLI both use) stays the
    # programmatic identifier and is rendered as secondary text, never
    # the primary label -- see B15 in the UI audit.
    title: str
    description: str
    params_model: type[TrellisModel]
    run: Callable[[Any, StoreRegistry], dict[str, Any]]


_REPORTS: dict[str, _ReportEntry] = {
    "holdout": _ReportEntry(
        title="Holdout effect",
        description=(
            "Compares task outcomes between agents that got a context pack "
            "and agents held out of one. A good report shows a positive, "
            "statistically significant effect (p below the window's planned "
            "threshold) with a confidence interval that excludes zero and a "
            "clean guardrail section; a bad one shows no measurable "
            "difference, or a window spanning more than one holdout rate, which "
            "needs the rate parameter to pick one. This is the "
            "single strongest answer to whether context packs help at all."
        ),
        params_model=HoldoutParams,
        run=_run_holdout,
    ),
    "judged-outcomes": _ReportEntry(
        title="Judged outcomes",
        description=(
            "Joins each judged memory operation to what happened to it "
            "afterwards: served in a pack, graded by feedback, cited as "
            "helpful. A good value is a high served-and-cited share; a bad "
            "one is memories judged but never served, or served but never "
            "cited, which both mean the judging work is not reaching "
            "outcomes. Informs the memory-extraction and judging loop."
        ),
        params_model=JudgedOutcomesParams,
        run=_run_judged_outcomes,
    ),
    "advisory-effectiveness": _ReportEntry(
        title="Advisory effectiveness",
        description=(
            "Scores each advisory by how its presence correlates with pack "
            "success versus a baseline without it, read-only (never "
            "mutates stored confidence). A good advisory has a positive "
            "lift with enough presentations to trust it; a bad one has a "
            "negative lift or too few presentations to score. Informs the "
            "advisory fitness / suppression loop."
        ),
        params_model=AdvisoryEffectivenessParams,
        run=_run_advisory_effectiveness,
    ),
    "value": _ReportEntry(
        title="Pack value",
        description=(
            "What share of the tokens a pack injected were actually cited "
            "as helpful, by strategy, item type, and intent family, plus "
            "an estimated dollar cost per cited item. A good value is a "
            "high cited-token fraction at a low cost per citation; a bad "
            "one is tokens spent with nothing cited. Informs budget and "
            "strategy-mix tuning."
        ),
        params_model=ValueParams,
        run=_run_value,
    ),
    "dimension-predictiveness": _ReportEntry(
        title="Dimension predictiveness",
        description=(
            "Correlates each pack-quality scoring dimension against actual "
            "task success (point-biserial correlation), when a PackBuilder "
            "evaluator is wired. A good dimension has a correlation clearly "
            "away from zero in the expected direction; a dimension near "
            "zero is not predicting anything and is a candidate to drop or "
            "reweight. Informs pack-quality scoring weights."
        ),
        params_model=DimensionPredictivenessParams,
        run=_run_dimension_predictiveness,
    ),
    "learning-candidates": _ReportEntry(
        title="Learning candidates",
        description=(
            "Aggregates graded packs by (intent_family, item_id) and "
            "proposes promotion or noise-demotion candidates for human "
            "review. A good window has candidates with enough support "
            "(times_served) and a clear success/retry split; zero "
            "candidates over a long window means the review queue has "
            "nothing to look at. This read-only view does not write the "
            "candidates/decisions files the CLI and nightly worker write — "
            "see the response's 'note' field. Informs the Review queue."
        ),
        params_model=LearningCandidatesParams,
        run=_run_learning_candidates,
    ),
    "domains": _ReportEntry(
        title="Domain coverage",
        description=(
            "Tallies the `domain` tag across traces, documents, and served "
            "packs, including an explicit `(none)` bucket for untagged "
            "items. A good domain has broad coverage and a healthy "
            "feedback success rate; `(none)` dominating means domain "
            "tagging coverage is the gap, not retrieval quality. Informs "
            "domain-scoped retrieval and tagging-pipeline coverage."
        ),
        params_model=DomainsParams,
        run=_run_domains,
    ),
    "extractor-fallbacks": _ReportEntry(
        title="Extractor fallbacks",
        description=(
            "Summarizes how often tiered extraction fell back from a "
            "deterministic extractor to a lower-priority one, per "
            "source_hint. A good rate is low and concentrated in sources "
            "that are expected to need it; a bad rate is high across the "
            "board, meaning the deterministic extractors are not matching "
            "real input shapes. Informs the extraction-dispatcher tier mix."
        ),
        params_model=DaysLimitParams,
        run=_run_extractor_fallbacks,
    ),
    "pack-telemetry": _ReportEntry(
        title="Pack telemetry",
        description=(
            "Aggregates PACK_ASSEMBLED telemetry: budget-saturation rate, "
            "rejection-reason distribution, per-strategy yield. A good "
            "window has low saturation and few forced rejections; a bad "
            "one is saturated often (budget too tight) or rejecting a lot "
            "for one dominant reason (a filter worth auditing). Informs "
            "PackBuilder budget and filter tuning."
        ),
        params_model=DaysLimitParams,
        run=_run_pack_telemetry,
    ),
    "token-usage": _ReportEntry(
        title="Token usage",
        description=(
            "Breaks down token usage by layer (CLI/MCP/SDK) and top "
            "operations, plus budget-saturation signals. A good pattern is "
            "usage concentrated in the operations expected to be heavy; a "
            "bad one is an operation burning tokens with no proportionate "
            "value. Informs capacity planning and budget defaults."
        ),
        params_model=DaysLimitParams,
        run=_run_token_usage,
    ),
    "cost": _ReportEntry(
        title="Cost estimate",
        description=(
            "Estimates Trellis's dollar cost overhead: tokens injected "
            "into agent prompts, priced per model. A good value is low "
            "relative to the value report's cited-token fraction; a bad "
            "one is high cost with low citation, the same signal 'value' "
            "gives from the other side. Informs model/pricing choices and "
            "budget defaults."
        ),
        params_model=CostParams,
        run=_run_cost,
    ),
}


@router.get("/analyze")
async def list_analyze_reports() -> dict[str, Any]:
    """List the reports available under ``GET /api/v1/analyze/{report}``.

    ``params_schema`` is each report's params DTO as JSON Schema
    (``params_model.model_json_schema()``) — the same bounds
    (``minimum``/``maximum``/``exclusiveMinimum``/``exclusiveMaximum``),
    types (``integer``/``number``/``boolean``/``string``) and, where a
    field restricts to a fixed set (``outcome`` on the holdout report),
    ``enum`` that ``GET /api/v1/analyze/{report}`` itself enforces. A
    client renders a typed control from this instead of a plain text box
    per param.
    """
    return {
        "reports": [
            {
                "name": name,
                "title": entry.title,
                "description": entry.description,
                "default_params": entry.params_model().model_dump(),
                "params_schema": entry.params_model.model_json_schema(),
            }
            for name, entry in sorted(_REPORTS.items())
        ]
    }


# A plain ``def``, not ``async def``: every report is a synchronous scan of
# the stores, and FastAPI runs a sync handler in its threadpool. As a
# coroutine, one slow report would block the event loop and stall every
# other request the process is serving.
@router.get("/analyze/{report}")
def get_analyze_report(report: str, request: Request) -> JSONResponse:
    """Run one allowlisted ``trellis analyze`` report over HTTP.

    Query params are validated against the report's own bounded DTO
    (``extra="forbid"``) rather than FastAPI's automatic pydantic-model
    binding, because that binding does not reject an unrecognized query
    key — only a manual parse does.
    """
    entry = _REPORTS.get(report)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown analyze report",
                "report": report,
                "allowed": sorted(_REPORTS),
            },
        )

    raw_params = dict(request.query_params)
    try:
        params = entry.params_model(**raw_params)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc

    registry = get_registry()
    params_key = params.model_dump_json()
    cached_hit = _cache_get(report, registry, params_key)
    if cached_hit is not None:
        computed_at, data = cached_hit
        cached = True
    else:
        data = entry.run(params, registry)
        computed_at = _cache_set(report, registry, params_key, data)
        cached = False

    envelope = {
        "report": report,
        "params": params.model_dump(),
        "computed_at": computed_at,
        "cached": cached,
        "data": data,
    }
    safe = _finite(jsonable_encoder(envelope))
    return JSONResponse(content=safe)
