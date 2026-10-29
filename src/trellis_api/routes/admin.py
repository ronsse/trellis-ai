"""Admin routes.

The Review-view endpoints (WP10) live at the bottom of this module. They
back the UI's human-decision inbox — surfacing tuner proposals, learning
candidates, schema-evolution candidates, and code-authoring proposals —
and route every approve / reject / promotion through the same governed
library paths the CLI uses (never a new direct-write path). The autonomy
tiers that govern which surfaces are human-gated are described in
``docs/design/adr-autonomy-ladder.md``; in short: tuner-proposal
approve/reject and learning promotion are human-gated, schema-evolution
promotion has *no* machine write path (only a draft-ADR action), and code
proposals are read-only here.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from trellis.core.base import utc_now
from trellis.core.cron import next_tick_after
from trellis.core.error_sanitize import (
    describe_json_error,
    describe_os_error,
    sanitize_error_message,
)
from trellis.core.path_presence import path_is_present
from trellis.core.vector_metadata import resolve_vector_store
from trellis.errors import StaleStoreWriteError
from trellis.learning.artifacts import (
    LEARNING_ARTIFACTS_DIR_ENV,
    LEARNING_CANDIDATES_FILENAME,
    resolve_learning_artifacts_dir,
)
from trellis.learning.scoring import (
    PROMOTE_RECOMMENDATIONS,
    prepare_learning_promotions,
    promotion_already_promoted,
    promotion_landed,
    promotion_verdict,
    settle_promotion,
    submit_learning_promotion,
    unnamed_approved_count,
)
from trellis.learning.tuners import (
    PromotionPolicy,
    preview_promotion,
    promote_proposal,
    reject_proposal,
    resolve_auto_promote_overrides,
)
from trellis.llm.routing import LLMConsumer, LLMRoutingError
from trellis.mutate import (
    Command,
    CommandResult,
    CommandStatus,
    Operation,
    build_curate_executor,
    build_schedule_executor,
)
from trellis.mutate.commands import STORE_REFUSAL_REASONS
from trellis.ops.loop_health import JOB_LOOPS, LoopHealthRow, summarize_loop_health
from trellis.ops.parameter_reachability import (
    READ_POINTS,
    reachability_reasons,
)
from trellis.retrieve.advisory_generator import (
    AdvisoryGenerator,
    resolve_advisory_thresholds,
)
from trellis.retrieve.effectiveness import (
    analyze_effectiveness,
    run_effectiveness_feedback,
)
from trellis.retrieve.metrics_timeseries import compute_timeseries
from trellis.retrieve.pack_builder import PackBuilder, _filter_legacy_advisories_enabled
from trellis.schedule.catalog import JOB_CATALOG, JobSpec
from trellis.schemas.schedule import ScheduledJob
from trellis.stores.advisory_source import load_advisory_store
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.base.vector import VectorStore
from trellis.stores.schedule_store import ScheduleStore
from trellis.stores.settings_store import load_settings_values
from trellis_api.app import get_bind_info, get_registry
from trellis_api.auth import (
    AUTH_MODE_REQUIRED,
    AuthContext,
    authenticate,
    resolve_auth_mode,
)
from trellis_api.observability import resolve_metrics_public
from trellis_api.routes._principal import request_clearance
from trellis_api.routes.health import probe_backends, resolve_ops_detail
from trellis_api.routes.settings import resolve_env_pinned_bool_setting
from trellis_wire.dtos import (
    AdminJobLastRun,
    AdminJobListResponse,
    AdminJobPatchRequest,
    AdminJobPatchResponse,
    AdminJobRunResponse,
    AdminJobSummary,
    BackendHealth,
    CodeProposalListResponse,
    CodeProposalSummary,
    DefaultValidationError,
    DraftAdrResponse,
    HealthResponse,
    LearningCandidateListResponse,
    LearningPromotionRequest,
    LearningPromotionResponse,
    LearningPromotionResultRow,
    LoopHealthResponse,
    LoopHealthRowResponse,
    MetricsTimeseriesResponse,
    ProposalDecisionResponse,
    ProposalPreviewResponse,
    ProposalRejectRequest,
    RuntimeResponse,
    SchemaEvolutionCandidate,
    SchemaEvolutionListResponse,
    StatsResponse,
    StringDetailError,
    TimeseriesPointResponse,
    TimeseriesSeriesResponse,
    TunerProposalListResponse,
    TunerProposalSummary,
)

logger = structlog.get_logger(__name__)

router = APIRouter()

#: Read by the HOST job dispatcher (``docs/ops/job-dispatcher.sh.example``),
#: never by this process — ``GET /api/v1/runtime`` echoes it unvalidated,
#: purely so the admin UI can show what a host-only nightly job would
#: resolve against, without claiming this process consults it itself.
_HOST_JOBS_DIR_ENV = "TRELLIS_HOST_JOBS_DIR"


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Check API and store health.

    Reuses the ``/readyz`` probe set (``probe_backends``) so this
    versioned-API view can never read healthy while ``/readyz`` reads
    degraded — before this, ``checks`` was a hard-coded literal that
    never touched a store.
    """
    registry = get_registry()
    backend_results = probe_backends(registry)
    checks: dict[str, bool] = {"api": True}
    checks.update(
        {name: result["status"] == "ok" for name, result in backend_results.items()}
    )
    overall_status = "ok" if all(checks.values()) else "degraded"
    return HealthResponse(
        status=overall_status,
        checks=checks,
        backends={
            name: BackendHealth(**result) for name, result in backend_results.items()
        },
    )


# Two document counts, not one, and not the other one (#412).
#
# ``GET /api/v1/documents`` has counted its ``total`` under the caller's
# ``include_chunks`` since #385/#391 — whole documents by default. This
# endpoint reported ``count()`` with no argument, which defaults to
# ``include_chunks=True``. On the reference deployment those read 579 and
# 1,319: two operator surfaces, both labelled "documents", disagreeing by
# 2.3x, with nothing on either saying which population it described.
#
# Neither is wrong in isolation, which is why the fix reports both rather
# than picking. Making stats exclude chunks would have destroyed the
# storage number an operator sizing a corpus or sanity-checking a prune
# legitimately wants; leaving it alone keeps two fields called
# "documents" meaning different things on two surfaces a reader compares.
# The ABC already binds ``count``'s ``include_chunks`` to the
# ``list_documents`` call it is *reported beside* — neither stats site is
# beside a listing, so the rule could not reach them. Naming both
# populations is how it reaches them.
@router.get("/stats", response_model=StatsResponse)
def stats() -> StatsResponse:
    """Get store statistics.

    ``documents`` counts whole documents and reconciles with
    ``GET /api/v1/documents``' ``total``; ``document_rows`` counts every
    stored row, fragments included, and reconciles with
    ``GET /api/v1/documents?include_chunks=true`` (#412).
    """
    registry = get_registry()
    document_store = registry.knowledge.document_store
    return StatsResponse(
        traces=registry.operational.trace_store.count(),
        documents=document_store.count(include_chunks=False),
        document_rows=document_store.count(include_chunks=True),
        nodes=registry.knowledge.graph_store.count_nodes(),
        edges=registry.knowledge.graph_store.count_edges(),
        events=registry.operational.event_log.count(),
    )


@router.get("/runtime", response_model=RuntimeResponse)
def runtime() -> RuntimeResponse:
    """Report this process's effective configuration for the admin UI.

    Every field is read straight from the resolver the real enforcement
    uses — ``resolve_auth_mode``, ``resolve_ops_detail``,
    ``resolve_metrics_public``, ``get_bind_info``, and for ``embedder``,
    ``registry.embedding_fn`` itself (the resolver every retrieval
    surface actually calls, not the separate ``llm.embedding:`` block
    nothing else reads) — so this view cannot drift from how the server
    actually behaves. Deliberately excludes anything secret-shaped:
    ``stores`` is class names only, ``embedder`` is the resolved
    embedder's dotted path or provider name followed by the configuration
    knob in play (``TRELLIS_EMBEDDING_FN``, ``embeddings.provider``, or
    ``embeddings`` when the whole block is malformed), never a key or
    URL, ``llm_routes`` carries tier names
    only (an ``LLMRoute``'s ``block`` — which can hold a literal API key
    — is never touched, and a malformed route is reported by its
    setting path, never the value that failed to validate), and the two
    directory fields are plain paths. Resolving the embedder is wrapped
    whole, and the knob lookup used on both the raise and the
    resolved-to-nothing paths is itself guarded against raising, so an
    unconfigured or misconfigured embedding knob — including a
    non-mapping ``embeddings:`` block — reads as "none configured" or
    "unknown" here, never 500s a panel that exists to show
    configuration, not enforce it.
    """
    registry = get_registry()
    host, port = get_bind_info()

    stores = {
        "graph_store": type(registry.knowledge.graph_store).__name__,
        "vector_store": type(registry.knowledge.vector_store).__name__,
        "document_store": type(registry.knowledge.document_store).__name__,
        "blob_store": type(registry.knowledge.blob_store).__name__,
        "event_log": type(registry.operational.event_log).__name__,
        "trace_store": type(registry.operational.trace_store).__name__,
    }

    # Derived from registry.embedding_fn -- the resolver every retrieval
    # surface (PackBuilder, the MCP server, the trace-embed worker, ...)
    # actually calls -- never from build_embedder_client(), which builds
    # an unrelated, unused-in-production EmbedderClient from a separate
    # 'llm.embedding:' block nothing else reads. Wrapped whole, like the
    # embedder's own degrade-to-keyword+graph callers, so a misconfigured
    # 'embeddings:' block or TRELLIS_EMBEDDING_FN reads as "unknown" here
    # rather than 500-ing a panel that exists to show configuration, not
    # enforce it. A non-mapping 'embeddings:' block (a bare string, bool,
    # int, or list) no longer raises inside this try -- embedding_fn
    # itself degrades that shape to "unconfigured" -- so it is reported
    # as "unknown" below, in the resolved-to-nothing branch, not here.
    embedder_name: str | None = None
    try:
        resolved_embedding_fn = registry.embedding_fn
    except Exception as exc:  # GRACEFUL-DEGRADATION: a panel, not a gate
        # Never the knob's *value* here (embedding_source_label()) -- on
        # the failure path the configured string is exactly the
        # typo/attacker-controlled text that must not reach the response
        # body or the log. Only the knob name and the exception type.
        # The knob lookup itself is wrapped too: it is guarded against
        # this same failure shape (see StoreRegistry.embedding_source_knob),
        # but a second, independent try/except here means a future bug in
        # that guard degrades this field instead of 500-ing the panel.
        try:
            knob = registry.embedding_source_knob()
        except Exception:  # GRACEFUL-DEGRADATION: a panel, not a gate
            knob = None
        logger.warning(
            "runtime_embedder_probe_failed",
            knob=knob,
            error_type=type(exc).__name__,
        )
        embedder_name = (
            f"unknown ({knob} failed: {type(exc).__name__})"
            if knob
            else f"unknown ({type(exc).__name__})"
        )
    else:
        if resolved_embedding_fn is not None:
            embedder_name = registry.embedding_source_label()
        else:
            # embedding_fn returns None both when nothing is configured
            # and when the 'embeddings:' block is a non-mapping value it
            # degrades rather than raises on (see its docstring) -- the
            # two must not render identically, or a malformed block reads
            # as a quiet, intentional "no embedder" instead of a
            # misconfiguration the operator should fix. embedding_source_knob
            # reports "embeddings" for exactly that second case and None
            # for the first.
            knob = registry.embedding_source_knob()
            embedder_name = f"unknown ({knob})" if knob else None

    llm_routes: dict[str, str | None] = {}
    llm_routes_error: str | None = None
    try:
        for consumer in LLMConsumer:
            llm_routes[consumer.value] = registry.llm_route(consumer).tier
    except LLMRoutingError as exc:
        # Never surface exc.message verbatim. trellis.llm.routing composes
        # that text from the raw llm.routes / llm.tiers value an operator
        # can mistype a credential into (a mapping holding an api_key, a
        # bare key string standing in for a tier name) -- routing.py now
        # avoids repr-ing that value itself, but this is the one place the
        # message leaves the process into a REST body, so it stays a
        # second, independent gate. Report only exc.setting -- a YAML key
        # path, never a config value -- plus fixed text; the full message
        # still reaches the operator via the log.
        logger.warning("runtime_llm_routes_invalid", setting=exc.setting)
        llm_routes_error = (
            f"{exc.setting} is invalid; see the server log."
            if exc.setting
            else "llm.routes/llm.tiers is invalid; see the server log."
        )
    except Exception as exc:  # GRACEFUL-DEGRADATION: a panel, not a gate
        # routing.py raises LLMRoutingError for a non-Mapping 'llm:' block,
        # caught above; this is the second, independent gate the embedder
        # block earlier in this function also uses -- whatever reaches
        # here next (a resolver bug, a future shape validate_llm_routing
        # doesn't anticipate) degrades to a reported field rather than a
        # 500 that blanks the whole panel, auth banner included. Fixed
        # text only, never str(exc), which could echo a pasted credential.
        logger.warning(
            "runtime_llm_routes_invalid",
            setting=None,
            error_type=type(exc).__name__,
        )
        llm_routes_error = "llm.routes/llm.tiers is invalid; see the server log."

    learning_dir = resolve_learning_artifacts_dir(registry.stores_dir)
    mode = resolve_auth_mode()

    return RuntimeResponse(
        auth_mode=mode,
        auth_mode_permissive=mode != AUTH_MODE_REQUIRED,
        ops_detail=resolve_ops_detail(),
        metrics_public=resolve_metrics_public(),
        bind_host=host,
        bind_port=port,
        stores=stores,
        embedder=embedder_name,
        llm_routes=llm_routes,
        llm_routes_error=llm_routes_error,
        learning_artifacts_dir=str(learning_dir) if learning_dir else None,
        host_jobs_dir=os.environ.get(_HOST_JOBS_DIR_ENV) or None,
    )


@router.get("/loops", response_model=LoopHealthResponse)
def loop_health() -> LoopHealthResponse:
    """Is each curation / learning loop actually doing anything?

    Reads existing events only (``trellis.ops.loop_health``), plus one
    documented live store read for the tuner's pending-proposal count.
    A loop with no event yet reports ``last_run_at``/``last_status`` as
    ``null`` — "never run (or ran before this build)" — which is a
    different fact from a real run that counted zero, so the two must
    never render the same. ``schedule_status``/``stale`` are computed
    here (not in ``summarize_loop_health``) from each row's own
    schedule-registry row, so a loop with no catalog job is never
    "stale", a disabled or manual-only job reads "not_scheduled", and a
    scheduled job's threshold follows its own cadence rather than a
    fixed constant.
    """
    registry = get_registry()
    settings_values = load_settings_values(registry.stores_dir)
    auto_promote_enabled = bool(
        resolve_auto_promote_overrides(settings=settings_values)["enabled"]
    )
    report = summarize_loop_health(
        registry.operational.event_log,
        registry.operational.tuner_state_store,
        auto_promote_enabled=auto_promote_enabled,
    )
    store = _get_schedule_store()
    now = utc_now()
    return LoopHealthResponse(
        generated_at=report.generated_at.isoformat(),
        loops=[_loop_health_row_response(row, store, now) for row in report.loops],
    )


def _loop_health_row_response(
    row: LoopHealthRow, store: ScheduleStore, now: datetime
) -> LoopHealthRowResponse:
    """Map one core ``LoopHealthRow`` onto its wire row, adding schedule facts.

    ``trellis_wire`` has no dependency on ``trellis`` core, so this file
    (not ``loop_health.py``) is where a core row's fields become wire
    fields — and where the schedule-registry lookup for
    ``schedule_status``/``stale`` happens, since ``summarize_loop_health``
    is deliberately EventLog-only plus one documented live read, and a
    ``ScheduleStore`` lookup is neither.
    """
    schedule_status, stale = _loop_schedule_status_and_staleness(
        row.job_name, row.last_run_at, store, now
    )
    return LoopHealthRowResponse(
        name=row.name,
        description=row.description,
        actuates=row.actuates,
        what_it_changes=row.what_it_changes,
        job_name=row.job_name,
        last_run_at=row.last_run_at.isoformat() if row.last_run_at else None,
        last_status=row.last_status,
        counters=row.counters,
        schedule_status=schedule_status,
        stale=stale,
    )


@router.get("/effectiveness")
def effectiveness(
    days: int = Query(30, description="Days of history to analyze"),
    min_appearances: int = Query(2, description="Minimum item appearances"),
) -> dict[str, Any]:
    """Analyze context pack effectiveness."""
    registry = get_registry()
    settings_values = load_settings_values(registry.stores_dir)
    report = analyze_effectiveness(
        registry.operational.event_log,
        days=days,
        min_appearances=min_appearances,
        demotion_settings=settings_values,
    )
    return {"status": "ok", **report.model_dump()}


@router.post("/effectiveness/apply-noise-tags")
def apply_noise_tags(
    days: int = Query(30, description="Days of history to analyze"),
    min_appearances: int = Query(2, description="Minimum item appearances"),
) -> dict[str, Any]:
    """Analyze effectiveness AND apply noise tags to low-value items.

    Runs the full feedback loop: analyze → tag noise items with
    signal_quality="noise" so PackBuilder excludes them by default.
    """
    registry = get_registry()
    settings_values = load_settings_values(registry.stores_dir)
    report = run_effectiveness_feedback(
        registry.operational.event_log,
        registry.knowledge.document_store,
        days=days,
        min_appearances=min_appearances,
        # #338: mirror the demotion onto the vector row, or the semantic
        # axis keeps serving the pre-demotion snapshot.
        vector_store=resolve_vector_store(registry),
        demotion_settings=settings_values,
    )
    # What apply_noise_tags actually wrote, not what the evidence gate
    # admitted (#336) and not the usage-rate rule's proposal — the key
    # says "tagged", so it has to count writes. A gate admission that
    # resolves to no document (a trace id or other non-document id)
    # writes nothing; ``noise_candidates_refused_not_document`` below
    # names that remainder (#833).
    # ``demotion_screen`` in the dumped report carries the full accounting.
    tagged = (
        report.noise_tags_written
        if report.noise_tags_written is not None
        else len(report.noise_candidates)
    )
    return {
        "status": "ok",
        "noise_candidates_tagged": tagged,
        "noise_candidates_proposed": len(report.noise_candidates),
        "noise_candidates_refused_not_document": len(report.noise_refused_not_document),
        **report.model_dump(),
    }


# -- Improvement-metrics dashboard (WP11) --


@router.get(
    "/metrics/timeseries",
    response_model=MetricsTimeseriesResponse,
    responses={
        422: {
            "description": (
                "``days`` isn't a valid int (DefaultValidationError -- "
                "``detail`` is a list of field errors), or ``bucket`` / "
                "``metric`` / ``group_by`` fails its own value check "
                "(StringDetailError -- ``detail`` is a plain string naming "
                "the problem)."
            ),
            "model": DefaultValidationError | StringDetailError,
        }
    },
)
def metrics_timeseries(
    metric: str = Query(..., description="One of the five improvement metrics"),
    days: int = Query(30, description="Look-back window in days"),
    bucket: str = Query("day", description="Bucket granularity (only 'day')"),
    group_by: str = Query(
        "none", description="Grouping axis: domain | intent_family | none"
    ),
) -> MetricsTimeseriesResponse:
    """Compute an improvement metric as a daily time series.

    Read-only — the aggregation in
    :func:`trellis.retrieve.metrics_timeseries.compute_timeseries` reads
    the EventLog and never mutates a store. Buckets with no data are
    omitted (not zero-filled). An unknown ``metric`` / ``group_by`` /
    ``bucket`` (or a non-positive ``days``) returns 422 rather than a
    silent empty result, so a typo surfaces loudly.

    ``scan_truncated`` / ``scanned_events`` carry
    :func:`compute_timeseries`'s :class:`~trellis.stores.base.event_log.ScanCoverage`
    (P1-8): when ``scan_truncated`` is true, the EventLog read hit its
    cap and the window's recent-end buckets may be missing data rather
    than genuinely empty.
    """
    if bucket != "day":
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported bucket {bucket!r}; only 'day' is implemented.",
        )
    registry = get_registry()
    try:
        result = compute_timeseries(
            registry.operational.event_log,
            metric=metric,
            days=days,
            group_by=group_by,
        )
    except ValueError as exc:
        # Unknown metric / group_by / non-positive days — a client input
        # error, surfaced as 422 (not a 500).
        raise HTTPException(
            status_code=422, detail=sanitize_error_message(str(exc))
        ) from exc

    return MetricsTimeseriesResponse(
        metric=result.metric,
        bucket=result.bucket,
        group_by=result.group_by,
        days=result.days,
        series=[
            TimeseriesSeriesResponse(
                group_key=s.group_key,
                points=[
                    TimeseriesPointResponse(
                        bucket_start=p.bucket_start,
                        value=p.value,
                        sample_count=p.sample_count,
                    )
                    for p in s.points
                ],
            )
            for s in result.series
        ],
        scan_truncated=result.scan.truncated,
        scanned_events=result.scan.scanned,
    )


# -- Advisories --


#: The refusal status every advisory-generate arm that wrote nothing
#: answers with. 409, matching what ``routes/policies.py`` already answers
#: for a degraded store and what #483 set ``CONFIG_ERROR_STATUS`` to at
#: this boundary — a third answer would give one condition three statuses
#: across three surfaces. Not derived from ``middleware._error_status``:
#: that maps every ``StoreError`` to 500, so leaning on the global typed
#: handler here would produce a *differently* wrong status.
_ADVISORY_REFUSED_STATUS = 409


@router.post(
    "/advisories/generate",
    responses={
        _ADVISORY_REFUSED_STATUS: {
            "description": (
                "Nothing was generated and nothing was written: the advisory "
                "store loaded degraded, the file changed under the request, "
                "or the deployment has no stores_dir. A `code` says which — "
                "`degraded_store` and `stores_dir_unconfigured` at the top "
                "level beside `status`, `stale_store_write` under `detail`, "
                "which is the shape that arm has answered since #438."
            )
        }
    },
)
def generate_advisories(
    response: Response,
    days: int = Query(30, description="Days of history to analyze"),
    min_sample: int | None = Query(
        None, description="Min sample size; defaults to the configured setting"
    ),
    min_effect: float | None = Query(
        None, description="Min effect size; defaults to the configured setting"
    ),
) -> dict[str, Any]:
    """Generate advisories from outcome data.

    Analyzes PACK_ASSEMBLED and FEEDBACK_RECORDED events to find patterns,
    then stores deterministic advisories for delivery alongside packs.

    Every arm that wrote nothing answers 409, never 200, and names itself
    with a `code`. Two of the three carry it at the top level beside
    `status` — `degraded_store` and `stores_dir_unconfigured`. The third,
    `stale_store_write`, carries it under `detail`: that arm has answered
    409 since #438 and its body shape is deliberately unchanged here, so
    a client reading all three wants `.code // .detail.code`.
    """
    # This docstring is the endpoint's public API description (it is what
    # ``scripts/generate_openapi.py`` puts in ``docs/api/v1.yaml``), so the
    # implementation rationale stays in comments. #484: the refusal status
    # is set on the *injected* ``Response`` rather than by raising
    # ``HTTPException``, because an ``HTTPException`` replaces the body
    # with a ``detail`` envelope — and the body is the half that was
    # already honest. Setting the status in place keeps ``status``,
    # ``store_degradation`` and ``advisories_stored`` exactly where #393
    # put them, so the only thing that changes on the wire is the status
    # line that was lying.
    registry = get_registry()
    settings_values = load_settings_values(registry.stores_dir)
    default_min_sample, default_min_effect = resolve_advisory_thresholds(
        settings_values
    )
    effective_min_sample = min_sample if min_sample is not None else default_min_sample
    effective_min_effect = min_effect if min_effect is not None else default_min_effect
    store = load_advisory_store(registry.stores_dir, surface="api.admin.generate")
    if store is None:
        # The third asymmetric arm, fixed with the same reasoning as the
        # degraded one below: a deployment with no ``stores_dir`` generated
        # nothing and stored nothing, and said so at 200. A ``ConfigError``
        # in all but name, which is why it takes the status #483 gave that
        # family. Scoped to this route — the ``GET`` beside it returns the
        # same sentinel for a *read* that served nothing, which is a
        # different claim and is left alone.
        response.status_code = _ADVISORY_REFUSED_STATUS
        return {
            "status": "error",
            "code": "stores_dir_unconfigured",
            "message": "stores_dir not configured",
        }
    generator = AdvisoryGenerator(
        registry.operational.event_log,
        store,
        min_sample_size=effective_min_sample,
        min_effect_size=effective_min_effect,
    )
    try:
        report = generator.generate(days=days)
    except StaleStoreWriteError as exc:
        # 409, not the 500 the unhandled-exception handler would produce:
        # that handler's body says only "internal server error", and this
        # refusal is both retryable and the caller's to retry. The host CLI
        # and this container write the same bind-mounted file (#438).
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_store_write",
                "message": (
                    "The advisory file changed after this request read it. "
                    "Generation was refused rather than replace whatever "
                    "landed in between. Retry."
                ),
                "path": exc.path,
            },
        ) from exc
    # ``ok`` over a refused run is the same lie as an unexplained zero: the
    # payload carries ``store_degradation``, but a caller that reads only the
    # headline would record a clean nightly generation (#393).
    if report.store_degradation is not None:
        # #484. #393 stopped at the body and pinned the 200 by test; the
        # status line is a second headline, and it is the one an HTTP
        # caller reads — ``response.ok`` is the overwhelmingly common
        # branch and it could not see this refusal at all. ``generate``
        # returns early on a degraded store rather than raising, so the
        # ``StaleStoreWriteError`` arm above never fired and the refusal
        # fell through to the success status: the #437 class with the
        # status line as the channel.
        #
        # ``degraded_store`` rather than ``degraded_store_write``, and 409
        # rather than 503, are both ``routes/policies.py``'s existing
        # answers for this exact condition — a degraded file does not
        # repair itself, so "retry later" is the wrong instruction.
        response.status_code = _ADVISORY_REFUSED_STATUS
        return {
            "status": "degraded",
            "code": "degraded_store",
            **report.model_dump(),
        }
    return {"status": "ok", **report.model_dump()}


@router.get("/advisories")
def list_advisories(
    scope: str | None = Query(None, description="Filter by scope"),
    min_confidence: float = Query(0.0, description="Minimum confidence"),
) -> dict[str, Any]:
    """List stored advisories."""
    registry = get_registry()
    store = load_advisory_store(registry.stores_dir, surface="api.admin.list")
    if store is None:
        return {"status": "error", "message": "stores_dir not configured"}
    advisories = store.list(scope=scope, min_confidence=min_confidence)
    degradation = store.degradation
    # Whether a legacy row (pre-#394, no evidence_confidence) is actually
    # being withheld from packs right now -- the same precedence
    # GET /settings reports for this entry, read here with the one shared
    # resolver so the two routes cannot disagree about which value governs.
    settings_values = load_settings_values(registry.stores_dir)
    filter_legacy_advisories_enabled = resolve_env_pinned_bool_setting(
        "filter_legacy_advisories",
        _filter_legacy_advisories_enabled,
        settings_values,
    )
    # ``count`` silently under-reports on a partial load, so a corrupt file
    # would present to an API client (and to the Memory Explorer) exactly as
    # a deployment that has never generated an advisory — the state
    # ``advisory_source`` exists to make distinguishable (#393). The rows
    # that parsed are still returned: the read stays lenient.
    return {
        "count": len(advisories),
        "advisories": [
            {
                **a.model_dump(mode="json"),
                # Same predicate PackBuilder uses to filter legacy rows out
                # of a pack -- imported, not re-derived, so this can never
                # disagree with what retrieval actually withholds.
                "is_legacy_advisory": PackBuilder._is_legacy_advisory(a),
            }
            for a in advisories
        ],
        "filter_legacy_advisories_enabled": filter_legacy_advisories_enabled,
        "store_degradation": degradation.to_dict() if degradation else None,
    }


# -- Vector store management --


#: The status every arm that could not even attempt the reset answers.
#: 409, matching what #484/#505 settled on for the ``advisories/generate``
#: refusals and what #483 set ``CONFIG_ERROR_STATUS`` to at this
#: boundary: "no store is configured" is a ``ConfigError`` in all but
#: name, and the same condition class answering 409 on one route and 200
#: on another is the asymmetry those fixes existed to remove.
#:
#: **Two conditions share it, deliberately** (#511): no vector store
#: configured, and a configured store whose backend this route cannot
#: drive. They are one condition class — nothing was dropped, nothing was
#: recreated, and what has to change is the deployment rather than the
#: request — and they are told apart by ``code``, the discriminator this
#: route's docstring already declares canonical. That is how
#: ``advisories/generate`` next door tells *three* refusals apart under
#: one 409 — though only two of the three reach it through
#: ``_ADVISORY_REFUSED_STATUS`` and the third puts its ``code`` under
#: ``detail``, which is the half of that precedent this route does **not**
#: follow: all three codes here sit at the top level. Giving this arm its
#: own status would reintroduce the per-condition-per-surface spread #484
#: existed to remove. One name for one number, for the same reason.
#:
#: **501 was the other candidate and is refused.** Its dictionary meaning
#: — "the server does not support the functionality required to fulfill
#: the request" — is the closest literal match, and a distinct status is
#: machine-distinguishable without reading the body. Against it: 501 is a
#: **5xx**, and nothing broke. This boundary treats 5xx as server failure
#: (``middleware._error_status`` maps every ``StoreError`` to 500), so a
#: correct, expected refusal would land in the error rate beside real
#: outages — #506's silent-200 defect pointed the other way. 4xx is the
#: class that means "this request cannot be satisfied here, do not resend
#: it as-is", which is the true statement. 501 is also cacheable by
#: default (RFC 9110 15.6.2) while this condition is a property of *process
#: configuration*, so an intermediary could keep serving the refusal after
#: the operator has reconfigured. The machine-distinguishable half rides
#: ``code``, at the top level, where every other refusal here carries it.
_VECTORS_REFUSED_STATUS = 409

#: The status for a reset that was *attempted* and broke. Deliberately
#: **not** 409, and not by symmetry with the arm above — the two arms
#: make different claims and only one of them is the caller's to act on.
#: 409 says a precondition on the caller's side is unmet; a ``DROP
#: TABLE`` that raised mid-flight says the server fell over, possibly
#: with the table already gone, and no request the caller reshapes will
#: fix it. It is also the answer this boundary already gives for that
#: condition: ``middleware._error_status`` maps every ``StoreError`` to
#: 500 and only remaps ``ConfigError``, so a store failure that
#: propagated instead of being caught here would answer 500 too. The
#: catch stays for the *body*, not the status — see the comment on it.
_VECTORS_RESET_FAILED_STATUS = 500


def _vector_reset_refusal(vector_store: object) -> dict[str, Any] | None:
    """The refusal body for a store this route must not reset, else ``None``.

    Two things have to hold before the route touches anything, and both
    are questions the :class:`~trellis.stores.base.vector.VectorStore`
    abstraction answers about itself. The object has to *be* a
    ``VectorStore`` — the registry instantiates whatever class a config
    names, and this route depends on the ABC rather than on a shape — and
    that backend has to implement
    :meth:`~trellis.stores.base.vector.VectorStore.reset_storage`.

    Both are **declarations, not inferences** (#511, #512). Until #512
    this asked for private attributes: ``_pool`` / ``_conn`` for the reset
    and ``_dimensions`` for the width. Nothing declared either, so a
    backend that spelled them differently had an answer published on its
    behalf — silently, plausibly, and wrongly. ``supports_reset()`` is
    derived from the ``reset_storage`` override, so the fact this asks
    about and the code that does the work are one thing and cannot come
    apart; there is no second check and no dispatch left to disagree with
    it.

    Asking costs nothing and touches no instance state: ``isinstance``
    reads the MRO and ``supports_reset`` is a classmethod. That is load
    bearing here, above the ``try``. ``SQLiteStoreBase._conn`` — the
    attribute the pre-#512 probe was hunting for — is a *property that
    opens a connection*, so the obvious ``hasattr(store, "_conn")``
    spelling of that probe would have done I/O to answer a question about
    shape and, on a corrupt database, raised ``sqlite3.DatabaseError`` out
    of a decision about whether anything should happen at all.

    What this deliberately does *not* try to decide is whether
    ``reset_storage`` will *succeed*. The boundary is "can the reset
    *begin*": anything failing after the ``DROP`` has run is a reset that
    was attempted and broke, which is ``_VECTORS_RESET_FAILED_STATUS``'s
    claim and not this one's. Widening it would move a genuinely
    destructive failure into an arm whose whole promise is that nothing
    was touched.

    The two conditions share ``vector_reset_unsupported_backend``: same
    class (nothing touched, fix the deployment), and ``code`` is the
    machine-readable half. They do **not** share a message, and **neither
    message states a reason the object did not give.** A backend that
    declined is told to be exactly that — it does not implement
    ``reset_storage()`` — and *not* that it "keeps no `vectors` table",
    which is ArcadeDB's and Neo4j's reason for declining and would be
    invented about any other backend that simply has not implemented the
    method yet. ``VectorStore.reset_storage``'s own refusal makes that
    distinction for the same reason and in the same words; a route that
    borrowed the reason here while the ABC refused to borrow it one frame
    down would be #512 committed in the sentence that fixes #512. The
    OpenAPI description names ArcadeDB and Neo4j, which is a statement
    about this deployment's known backends and not about the object in
    hand.
    """
    if isinstance(vector_store, VectorStore):
        if vector_store.supports_reset():
            return None
        detail = (
            f"{type(vector_store).__name__} does not implement "
            "`reset_storage()`, so there is no way to drop and recreate "
            "its storage through this interface."
        )
    else:
        detail = (
            f"{type(vector_store).__name__} does not implement the "
            "`VectorStore` interface this route resets through."
        )
    return {
        "status": "error",
        "code": "vector_reset_unsupported_backend",
        "message": (
            f"Vector reset is not supported on this backend: {detail} "
            "Nothing was changed. "
            "Rebuild the backend's vector index with its own tooling, "
            "then repopulate with `trellis admin reindex-vectors "
            "--force`."
        ),
    }


@router.post(
    "/vectors/reset",
    responses={
        _VECTORS_REFUSED_STATUS: {
            "description": (
                "Nothing was dropped and nothing was recreated, and what "
                "has to change is the deployment rather than the request. "
                "A `code` says which — `vector_store_unconfigured` when "
                "the deployment has no vector store, "
                "`vector_reset_unsupported_backend` when it has one this "
                "route cannot reset — a backend that does not implement "
                "`reset_storage()` (ArcadeDB and Neo4j decline because "
                "they hold vectors as graph-node state and keep no "
                "`vectors` table of their own), or an object that does "
                "not implement the vector-store interface at all. "
                "Both sit at the top level beside `status`, and neither is "
                "transient: retrying an unchanged request cannot succeed."
            )
        },
        _VECTORS_RESET_FAILED_STATUS: {
            "description": (
                "The reset was attempted and failed. The vectors table may "
                "have been dropped and not recreated, so this is not a "
                "no-op the way the 409 is. `code` is `vector_reset_failed`, "
                "at the top level beside `status`, and `message` carries "
                "the sanitized backend error."
            )
        },
    },
)
def reset_vectors(response: Response) -> dict[str, Any]:
    """Drop and recreate the vectors table with current configured dimensions.

    No failure arm answers 200, and they do not all answer the same
    status. **409** is the answer whenever nothing was dropped and
    nothing was recreated and the fix is to the deployment rather than to
    the request — either the deployment has **no vector store
    configured** (`vector_store_unconfigured`), or it has one whose
    **backend this route cannot reset** (`vector_reset_unsupported_backend`;
    ArcadeDB and Neo4j keep vectors as graph-node state, so there is no
    `vectors` table here to drop). A reset that was **attempted and
    failed** answers **500** (`vector_reset_failed`): the server fell over
    mid-operation, the vectors table may be gone, and no reshaped request
    fixes that.

    A 409 on this route is a *permanent* answer, not "try again later" —
    same as the `advisories/generate` refusals next door, the condition
    does not resolve on its own. It is 4xx rather than 501 because nothing
    broke: the server declined correctly, and a 5xx at this boundary means
    the server failed.

    All three refusals name themselves in a `code` at the **top level** of
    the body, beside `status`. This route raises no `HTTPException`, so
    there is no `detail` envelope on any arm and `body["code"]` is always
    where the code is.
    """
    # This docstring is the endpoint's public API description (it is what
    # ``scripts/generate_openapi.py`` puts in ``docs/api/v1.yaml``), so the
    # implementation rationale stays in comments. #506, following #484 and
    # #505 next door: both statuses are set on the *injected* ``Response``
    # rather than raised as an ``HTTPException``, because an
    # ``HTTPException`` replaces the body with a ``detail`` envelope and
    # the structured body is the half of this route that was already
    # honest. Each assignment sits immediately before its ``return``: an
    # injected status is discarded outright if anything raises after it is
    # set, so there is no present or latent status/body mismatch.
    registry = get_registry()
    vector_store = getattr(registry.knowledge, "vector_store", None)
    if vector_store is None:
        response.status_code = _VECTORS_REFUSED_STATUS
        return {
            "status": "error",
            "code": "vector_store_unconfigured",
            "message": "Vector store not configured",
        }

    # Before #511 this route reached for ``_conn`` on the **blessed**
    # substrate — ``ArcadeDBVectorStore`` and ``Neo4jVectorStore`` have no
    # SQL handle at all — and died on ``AttributeError``, answered as a
    # 500 whose message was the attribute name: nothing an operator can
    # act on, and an invitation to retry something that can never succeed.
    # #511 asked the shape question before assuming the answer; #512
    # replaced the question. The backend now *declares* whether it can be
    # reset, and the declaration is derived from the implementation, so
    # this refusal and the work below read one fact.
    refusal = _vector_reset_refusal(vector_store)
    if refusal is not None:
        response.status_code = _VECTORS_REFUSED_STATUS
        return refusal

    # Read the width *before* anything is dropped. It is a declaration
    # rather than a measurement (``VectorStoreContractTests`` pins that it
    # is stable across writes), so it reads the same on either side — and
    # reading it after would put a property call between a completed
    # destructive operation and the 200 that reports it, which is #506's
    # defect with a new attribute in the hole. Nothing has been dropped
    # yet here, so a backend whose declaration raises fails a request that
    # changed nothing. There is no ``getattr`` fallback: a default
    # answered on the backend's behalf is exactly what #512 removed, and
    # the ABC makes the property abstract so no backend can be silent.
    declared_dimensions = vector_store.dimensions

    try:
        vector_store.reset_storage()
    # The catch is kept, and it is now earning its keep on the **body
    # alone**. It used to be annotated GRACEFUL-DEGRADATION — "surfaces
    # failure as a structured JSON response rather than a 5xx" — which
    # was the defect, not the rationale: the 2026-05 silent-fallback
    # audit bucketed this very site as DEFECT and it was annotated rather
    # than fixed. Degrading the *status* is over; degrading it was the
    # whole of #506. What letting the exception propagate to
    # ``app.add_exception_handler(Exception, ...)`` would cost is the
    # message: that handler answers a deliberately sparse
    # ``internal_error`` / "internal server error" envelope, because it
    # fires on every route including ones handling untrusted input. This
    # one is admin-scoped and its entire job is telling an operator what
    # broke, so the sanitized backend error is worth catching for — the
    # same argument #505 used for not raising ``HTTPException``. Nothing
    # is lost by catching: ``logger.exception`` records the traceback
    # under the request_id bound by ``request_id_middleware``, which also
    # echoes that id in ``X-Request-ID`` on this response.
    except Exception as exc:
        logger.exception("vectors_reset_failed")
        response.status_code = _VECTORS_RESET_FAILED_STATUS
        return {
            "status": "error",
            "code": "vector_reset_failed",
            "message": sanitize_error_message(str(exc)),
        }
    else:
        # The third arm of the same defect, found while fixing the two the
        # issue named and included for the reason #505 included its own
        # third: a route with one honest arm beside one lying arm is no
        # more trustworthy than one with two. This read used to be
        # ``getattr(vector_store, "_dimensions", None)`` — a private
        # attribute of another module, defined on ``PgVectorStore``,
        # ``ArcadeDBVectorStore`` and ``Neo4jVectorStore`` and **not** on
        # ``SQLiteVectorStore``, the default backend. #506 fixed the crash
        # it caused there (a completed reset answering 500) and #512 the
        # sentence: ``None`` now means the backend *said* it pins no
        # width, which is true of SQLite (a ``dimensions`` column per
        # row), rather than meaning nobody found an attribute by that
        # name. A fabricated number was never the risk; a fabricated
        # *absence*, published as a fact about a backend that never spoke,
        # was.
        message = (
            f"Recreated with {declared_dimensions}D"
            if declared_dimensions is not None
            else "Recreated (backend declares no fixed dimensionality)"
        )
        return {"status": "ok", "message": message}


# ===========================================================================
#  Review queue (WP10) — human-decision inbox
# ===========================================================================
#
# Every endpoint below is mounted on the admin router, so it already
# requires the ``admin`` scope and respects the TRELLIS_UI_ENABLED /
# ops-gating conventions wired in ``trellis_api.app``. The approve /
# reject / promotion routes additionally resolve the caller's identity via
# ``Depends(authenticate)`` and stamp it onto a ``REVIEW_DECISION_RECORDED``
# audit event, so the EventLog attributes each human decision to a
# credential.

#: How an operator produces the artifact the learning routes read, appended
#: to every answer that found none.
_LEARNING_WRITER_REMEDY = (
    "'trellis worker curate' and 'trellis analyze learning-candidates' write "
    f"{LEARNING_CANDIDATES_FILENAME} there when run without --output-dir "
    "against the same data directory; to use another directory, set "
    f"{LEARNING_ARTIFACTS_DIR_ENV} on the writer and on the API alike."
)


def _audit_identity(ctx: AuthContext) -> dict[str, str | None]:
    """Project the auth context to the identity fields stamped on audits."""
    return {"key_id": ctx.key_id, "key_name": ctx.name}


def _emit_review_decision(
    event_log: EventLog,
    *,
    surface: str,
    action: str,
    ctx: AuthContext,
    entity_id: str | None,
    detail: dict[str, Any],
) -> None:
    """Append a ``REVIEW_DECISION_RECORDED`` audit event for a human action.

    Complements (never replaces) the surface-specific event the underlying
    pipeline already emits — this row records *who* acted, attributing the
    decision to the authenticated credential.
    """
    event_log.emit(
        EventType.REVIEW_DECISION_RECORDED,
        source="trellis_api.review",
        entity_id=entity_id,
        entity_type=surface,
        payload={
            "surface": surface,
            "action": action,
            **_audit_identity(ctx),
            **detail,
        },
    )


class _LearningCandidatesUnavailableError(Exception):
    """No learning-candidate artifact the learning routes can serve.

    ``code`` is the machine-readable reason, ``path`` the directory or file
    at fault (``None`` when no directory could be resolved), and
    ``artifacts_dir`` the directory the routes looked in. Both routes catch
    it: the ``GET`` answers 200 with ``status="error"``, the ``POST`` 409.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        path: Path | None,
        artifacts_dir: Path | None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path
        self.artifacts_dir = artifacts_dir


def _load_learning_candidates() -> tuple[Path, dict[str, Any]]:
    """Return the learning-artifacts directory and its parsed candidates file.

    The directory comes from :func:`resolve_learning_artifacts_dir`, the
    resolver the writers default ``--output-dir`` from. Raises
    :class:`_LearningCandidatesUnavailableError` naming what is missing
    rather than letting a missing directory read as "no candidates".
    """
    artifacts_dir = resolve_learning_artifacts_dir(get_registry().stores_dir)
    if artifacts_dir is None:
        msg = (
            "No learning-candidate directory: the deployment has no stores_dir "
            f"and {LEARNING_ARTIFACTS_DIR_ENV} is unset. Set "
            f"{LEARNING_ARTIFACTS_DIR_ENV} on the writer and on the API alike."
        )
        raise _LearningCandidatesUnavailableError(
            msg, code="stores_dir_unconfigured", path=None, artifacts_dir=None
        )
    # ``path_is_present``: a directory that cannot be stat'ed for any reason
    # but absence falls through to the read below, which names the error.
    if not path_is_present(artifacts_dir):
        msg = (
            f"No learning candidates have been written for review: {artifacts_dir} "
            f"does not exist. {_LEARNING_WRITER_REMEDY}"
        )
        raise _LearningCandidatesUnavailableError(
            msg,
            code="learning_artifacts_dir_missing",
            path=artifacts_dir,
            artifacts_dir=artifacts_dir,
        )
    candidates_path = artifacts_dir / LEARNING_CANDIDATES_FILENAME
    if not path_is_present(candidates_path):
        msg = (
            f"No learning candidates have been written for review: "
            f"{candidates_path} does not exist. {_LEARNING_WRITER_REMEDY}"
        )
        raise _LearningCandidatesUnavailableError(
            msg,
            code="learning_candidates_missing",
            path=candidates_path,
            artifacts_dir=artifacts_dir,
        )
    try:
        payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    # ValueError covers JSONDecodeError and UnicodeDecodeError.
    except (OSError, ValueError) as exc:
        logger.warning(
            "learning_candidates_read_failed", path=str(candidates_path), error=str(exc)
        )
        # Describe, don't quote: str(exc) can be the whole message for an
        # OSError/ValueError built from a single text argument (#829
        # follow-up), so this never falls back to it.
        if isinstance(exc, OSError):
            description = describe_os_error(exc)
        elif isinstance(exc, json.JSONDecodeError):
            description = describe_json_error(exc)
        else:
            description = type(exc).__name__
        msg = (
            f"Could not read learning candidates from {candidates_path}: {description}"
        )
        raise _LearningCandidatesUnavailableError(
            msg,
            code="learning_candidates_unreadable",
            path=candidates_path,
            artifacts_dir=artifacts_dir,
        ) from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("candidates", []), list
    ):
        msg = (
            f"Could not read learning candidates from {candidates_path}: it is "
            "not a JSON object with a 'candidates' list."
        )
        raise _LearningCandidatesUnavailableError(
            msg,
            code="learning_candidates_unreadable",
            path=candidates_path,
            artifacts_dir=artifacts_dir,
        )
    return artifacts_dir, payload


# -- Section 1: Tuner proposals --------------------------------------------


@router.get("/proposals", response_model=TunerProposalListResponse)
def list_pending_proposals(
    limit: int = Query(100, ge=1, le=500, description="Max proposals to return"),
) -> TunerProposalListResponse:
    """List pending tuner proposals awaiting a human approve / reject.

    Surfaces effect_size / sample_size / baseline / proposed_values per
    proposal so the operator can judge each one. Mirrors the read in
    ``trellis metrics proposals --status pending`` but enriches each row
    with the resolved baseline values (what the proposal is measured
    against) and with whether the proposal can be read back at all --
    a scope no reader queries is approvable and inert, so the reason
    travels with the row rather than living only in a tuner log line.
    """
    registry = get_registry()
    tuner_state = registry.operational.tuner_state_store
    params = registry.operational.parameter_store
    proposals = tuner_state.list_proposals(status="pending", limit=limit)

    rows: list[TunerProposalSummary] = []
    for p in proposals:
        baseline = params.resolve(p.scope)
        baseline_values = dict(baseline.values) if baseline else {}
        checked = p.scope.component_id in READ_POINTS
        reasons = reachability_reasons(p.scope, tuple(p.proposed_values))
        rows.append(
            TunerProposalSummary(
                reachable=(not reasons) if checked else None,
                unreachable_reasons=[r.detail for r in reasons],
                proposal_id=p.proposal_id,
                tuner=p.tuner,
                status=p.status,
                component_id=p.scope.component_id,
                domain=p.scope.domain,
                intent_family=p.scope.intent_family,
                tool_name=p.scope.tool_name,
                proposed_values=dict(p.proposed_values),
                baseline_values=baseline_values,
                sample_size=p.sample_size,
                effect_size=p.effect_size,
            )
        )
    return TunerProposalListResponse(count=len(rows), proposals=rows)


#: The Review queue exposes no ``force`` or policy override (unlike the
#: CLI's ``--force``/``--min-sample-size``/``--min-effect-size`` flags), so
#: its one implicit policy must be the stricter of the two reasonable
#: defaults. ``allow_no_baseline=False`` now matches ``PromotionPolicy``'s
#: own class default — this object stays explicit anyway, so the route's
#: behaviour is pinned to this surface's own decision rather than silently
#: following wherever the library default moves next; mirrors
#: AutoPromotePolicy's ``require_baseline=True`` default on the other
#: unattended path. The CLI's bootstrap opt-in (``--allow-no-baseline``)
#: has no equivalent here: only a promotion writes a snapshot, so without
#: the CLI a scope can never clear its own first refusal.
_REVIEW_QUEUE_POLICY = PromotionPolicy(allow_no_baseline=False)


@router.get("/proposals/{proposal_id}/preview", response_model=ProposalPreviewResponse)
def preview_proposal(proposal_id: str) -> ProposalPreviewResponse:
    """Dry-run a proposal promotion — predict the decision, mutate nothing.

    Backs the UI confirm step: the operator sees the predicted
    promote / reject outcome (and why) before committing. Wraps the same
    :func:`trellis.learning.tuners.preview_promotion` the CLI dry-run uses,
    with the Review queue's stricter default policy (see
    ``_REVIEW_QUEUE_POLICY``) so a preview never predicts a promotion the
    commit route below would refuse.
    """
    registry = get_registry()
    preview = preview_promotion(
        proposal_id,
        tuner_state=registry.operational.tuner_state_store,
        parameter_store=registry.operational.parameter_store,
        policy=_REVIEW_QUEUE_POLICY,
    )
    return ProposalPreviewResponse(
        proposal_id=preview.proposal_id,
        predicted_status=preview.status,
        reason=preview.reason,
        proposed_values=preview.proposed_values,
        baseline_values=preview.baseline_values,
        effect_size=preview.effect_size,
        sample_size=preview.sample_size,
    )


@router.post(
    "/proposals/{proposal_id}/promote", response_model=ProposalDecisionResponse
)
def promote_proposal_route(
    proposal_id: str,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> ProposalDecisionResponse:
    """Promote a tuner proposal through the governed promotion pipeline.

    Wraps the same :func:`trellis.learning.tuners.promote_proposal` logic
    as ``trellis metrics promote --commit`` — validate, policy gate,
    write the new ``ParameterSet``, and emit ``PARAMS_UPDATED`` (or
    ``TUNER_PROPOSAL_REJECTED`` on a refusal from any of its three
    gates: the immutable core, reachability, or the policy gate itself).
    A second ``REVIEW_DECISION_RECORDED`` event records the reviewer
    identity. Unlike the CLI, this route has no ``--force``/threshold
    flags, so it always applies the stricter ``_REVIEW_QUEUE_POLICY``.
    """
    registry = get_registry()
    result = promote_proposal(
        proposal_id,
        tuner_state=registry.operational.tuner_state_store,
        parameter_store=registry.operational.parameter_store,
        event_log=registry.operational.event_log,
        policy=_REVIEW_QUEUE_POLICY,
        source="trellis_api.review.promote",
    )
    _emit_review_decision(
        registry.operational.event_log,
        surface="tuner_proposal",
        action="promote",
        ctx=ctx,
        entity_id=proposal_id,
        detail={
            "result_status": result.status,
            "reason": result.reason,
            "params_version": result.params_version,
            "effect_size": result.effect_size,
        },
    )
    return ProposalDecisionResponse(
        proposal_id=result.proposal_id,
        status=result.status,
        reason=result.reason,
        params_version=result.params_version,
        effect_size=result.effect_size,
    )


@router.post("/proposals/{proposal_id}/reject", response_model=ProposalDecisionResponse)
def reject_proposal_route(
    proposal_id: str,
    req: ProposalRejectRequest | None = None,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> ProposalDecisionResponse:
    """Reject a tuner proposal (human-gated tier-2 decision).

    Marks the proposal ``rejected`` and emits ``TUNER_PROPOSAL_REJECTED``
    via :func:`trellis.learning.tuners.reject_proposal`, then records a
    ``REVIEW_DECISION_RECORDED`` event with the reviewer identity.
    """
    registry = get_registry()
    reason = (req.reason if req else None) or "rejected_by_reviewer"
    result = reject_proposal(
        proposal_id,
        tuner_state=registry.operational.tuner_state_store,
        event_log=registry.operational.event_log,
        reason=reason,
        source="trellis_api.review.reject",
    )
    _emit_review_decision(
        registry.operational.event_log,
        surface="tuner_proposal",
        action="reject",
        ctx=ctx,
        entity_id=proposal_id,
        detail={"result_status": result.status, "reason": result.reason},
    )
    return ProposalDecisionResponse(
        proposal_id=result.proposal_id,
        status=result.status,
        reason=result.reason,
    )


# -- Section 2: Learning-promotion candidates ------------------------------


@router.get("/learning/candidates", response_model=LearningCandidateListResponse)
def list_learning_candidates() -> LearningCandidateListResponse:
    """Serve the most-recent ``intent_learning_candidates.json`` artifact.

    ``trellis worker curate`` and ``trellis analyze learning-candidates``
    write it, by default to ``TRELLIS_LEARNING_ARTIFACTS_DIR`` when set and
    otherwise to ``<data_dir>/learning``, the directory this route reads;
    ``artifacts_dir`` names it. When there is nothing to serve (the
    directory or the file is missing, the file is unreadable, or no
    directory can be resolved), the answer is still 200 but carries
    ``status="error"``, a ``code`` saying which, and a ``hint`` naming the
    path, so an unwritten artifact never reads as an empty review queue.

    Each candidate dict gets a server-computed ``promotable`` boolean —
    ``recommendation_type in PROMOTE_RECOMMENDATIONS``, the same set
    :func:`prepare_learning_promotions` checks before submitting a
    promotion — so a UI (or any other caller) can gate its own Approve
    control on that field instead of re-deriving the rule.
    """
    try:
        artifacts_dir, payload = _load_learning_candidates()
    except _LearningCandidatesUnavailableError as exc:
        return LearningCandidateListResponse(
            status="error",
            code=exc.code,
            artifacts_dir=None if exc.artifacts_dir is None else str(exc.artifacts_dir),
            hint=sanitize_error_message(exc.message),
        )
    candidates = [
        {
            **candidate,
            "promotable": (
                str(candidate.get("recommendation_type", "")).strip()
                in PROMOTE_RECOMMENDATIONS
            ),
        }
        if isinstance(candidate, dict)
        else candidate
        for candidate in payload.get("candidates", [])
    ]
    return LearningCandidateListResponse(
        artifacts_dir=str(artifacts_dir),
        generated_at_utc=payload.get("generated_at_utc"),
        candidate_count=len(candidates),
        candidates=candidates,
    )


@router.post(
    "/learning/promotions",
    response_model=LearningPromotionResponse,
    responses={
        409: {
            "description": (
                "Nothing was promoted: there is no learning-candidate artifact "
                "to join the decisions against. `detail.code` says why "
                "(`learning_artifacts_dir_missing`, `learning_candidates_missing`, "
                "`learning_candidates_unreadable` or `stores_dir_unconfigured`), "
                "`detail.path` names the directory or file at fault and "
                "`detail.message` how to produce it."
            )
        }
    },
)
def promote_learning_candidates(
    req: LearningPromotionRequest,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> LearningPromotionResponse:
    """Promote approved learning candidates through the governed pipeline.

    Builds the decisions payload from the request body, joins it against
    the most-recent candidate artifact (read from the directory
    ``GET /learning/candidates`` serves) via
    :func:`trellis.learning.prepare_learning_promotions`, then submits
    each approved promotion as ``ENTITY_CREATE`` + per-target
    ``LINK_CREATE`` commands through :class:`MutationExecutor` — exactly
    the path ``trellis curate promote-learning`` uses. A
    ``REVIEW_DECISION_RECORDED`` event records the reviewer identity.

    The response's ``status`` is the verdict ``promote-learning`` states,
    from the same function (:func:`promotion_verdict`), and the status code
    stays 200 for every verdict. An approved row with a blank
    ``candidate_id`` is skipped but makes the verdict ``refused``; rows that
    promoted still landed. A row whose precedent is already current, complete
    and unchanged comes back ``already_promoted`` and is written not at all,
    so re-submitting a decision (or a whole file) adds no version and no
    second ``PRECEDENT_PROMOTED`` event. A precedent a redaction already took
    comes back ``refused_redacted`` and is not written back. So does a row
    whose source document was redacted, when its name is one the redacted text
    gave it (the source's text or its own title): a candidate or a decision's
    ``promotion_name`` kept from before the redaction, or edited from it, since
    a name that contains an earlier name's distinctive text is refused too.
    Regenerate the candidate (and clear that name) to promote it again. Once the
    source id was written again with new content, a name derived from that
    content and a reviewer's own title promote; only a name the precedent
    carried at or before the redaction stays refused.
    """
    try:
        _, candidates_payload = _load_learning_candidates()
    except _LearningCandidatesUnavailableError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": sanitize_error_message(exc.message),
                "path": None if exc.path is None else str(exc.path),
            },
        ) from exc

    decisions_payload = {
        "decisions": [d.model_dump() for d in req.decisions],
    }
    registry = get_registry()
    plan = prepare_learning_promotions(
        candidates_payload=candidates_payload,
        decisions_payload=decisions_payload,
        document_store=registry.knowledge.document_store,
    )
    ready = [r for r in plan["results"] if r["status"] == "ready"]

    executor = build_curate_executor(registry, clearance=request_clearance(ctx))
    rows: list[LearningPromotionResultRow] = []
    # What the verdict reads: the executor's own answer, edges and precedent
    # event included, which the response rows do not carry. The CLI reads the
    # same outcomes, so the two surfaces cannot disagree about one run.
    outcomes: list[dict[str, Any]] = []
    for entry in plan["results"]:
        if entry["status"] != "ready":
            outcomes.append(
                {"candidate_id": entry["candidate_id"], "status": entry["status"]}
            )
            rows.append(
                LearningPromotionResultRow(
                    candidate_id=entry["candidate_id"], status=entry["status"]
                )
            )
            continue
        outcome = settle_promotion(
            submit_learning_promotion(
                executor,
                entry["entity_payload"],
                entry["edge_payloads"],
                requested_by="api:review.promote-learning",
                graph_store=registry.knowledge.graph_store,
                event_log=registry.operational.event_log,
                document_store=registry.knowledge.document_store,
            )
        )
        outcomes.append({"candidate_id": entry["candidate_id"], **outcome})
        rows.append(
            LearningPromotionResultRow(
                candidate_id=entry["candidate_id"],
                status=outcome["status"],
                entity_id=entry["entity_id"],
                node_id=outcome.get("node_id"),
                message=outcome.get("message"),
            )
        )

    promoted_count = sum(1 for o in outcomes if promotion_landed(o))
    already_promoted_count = sum(1 for o in outcomes if promotion_already_promoted(o))
    # An approved row with a blank id has no outcome but refuses the run, as it
    # does for the CLI: both hand the verdict the planner's own count.
    status = promotion_verdict(outcomes, unnamed_count=unnamed_approved_count(plan))
    _emit_review_decision(
        registry.operational.event_log,
        surface="learning_promotion",
        action="promote",
        ctx=ctx,
        entity_id=None,
        detail={
            "approved_count": plan["approved_count"],
            "named_count": plan["named_count"],
            "ready_count": len(ready),
            "promoted_count": promoted_count,
            "already_promoted_count": already_promoted_count,
            "candidate_ids": [r.candidate_id for r in rows],
        },
    )
    # HTTP 200 whatever the verdict: ``status`` is the run's outcome, not the
    # request's. Mapping ``refused`` / ``failed`` onto an error status is a
    # separate decision.
    return LearningPromotionResponse(
        status=status,
        approved_count=plan["approved_count"],
        named_count=plan["named_count"],
        ready_count=len(ready),
        promoted_count=promoted_count,
        already_promoted_count=already_promoted_count,
        results=rows,
    )


# -- Section 3: Schema-evolution candidates --------------------------------


@router.get("/schema-evolution/candidates", response_model=SchemaEvolutionListResponse)
def list_schema_evolution_candidates(
    limit: int = Query(
        200, ge=1, le=500, description="Max WELL_KNOWN_CANDIDATE events to scan"
    ),
) -> SchemaEvolutionListResponse:
    """List the latest ``WELL_KNOWN_CANDIDATE`` event per ``candidate_id``.

    Reuses the EventLog query the CLI's draft-promotion-adr path reads
    from. The only action exposed on these candidates is drafting the ADR
    markdown — promotion is a one-way ADR commitment with no machine
    write path.
    """
    event_log = get_registry().operational.event_log
    events = event_log.get_events(
        event_type=EventType.WELL_KNOWN_CANDIDATE,
        limit=limit,
        order="desc",
    )
    # Latest event wins per candidate_id (events are newest-first).
    seen: set[str] = set()
    rows: list[SchemaEvolutionCandidate] = []
    for event in events:
        payload = event.payload or {}
        candidate_id = str(payload.get("candidate_id") or "")
        if not candidate_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        rows.append(
            SchemaEvolutionCandidate(
                candidate_id=candidate_id,
                candidate_kind=payload.get("candidate_kind"),
                open_string_value=payload.get("open_string_value"),
                suggested_canonical_name=payload.get("suggested_canonical_name"),
                count=int(payload.get("count") or 0),
                distinct_extractors=list(payload.get("distinct_extractors") or []),
                distinct_domains=list(payload.get("distinct_domains") or []),
                first_seen=payload.get("first_seen"),
                last_seen=payload.get("last_seen"),
                recorded_at=event.recorded_at.isoformat(),
            )
        )
    return SchemaEvolutionListResponse(count=len(rows), candidates=rows)


@router.post(
    "/schema-evolution/{candidate_id}/draft-adr", response_model=DraftAdrResponse
)
def draft_schema_evolution_adr(
    candidate_id: str,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> DraftAdrResponse:
    """Render the promotion-ADR markdown for one schema-evolution candidate.

    The ONLY schema-evolution action — there is no approve/promote
    endpoint, because promoting a well-known type is a one-way ADR
    commitment (the ADR author edits ``well_known.py`` by hand after
    review). Reuses the CLI's template-rendering helpers
    (``_lookup_candidate_payload`` + ``_render_promotion_adr``) so the
    UI markdown matches what ``trellis admin draft-promotion-adr`` writes
    — but returns the markdown to the caller instead of writing a file.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    # Imported at call depth (like the auth module does) to avoid pulling
    # the Typer-bearing CLI module at import time.
    from trellis.learning.schema_evolution import (  # noqa: PLC0415
        RECOMMENDED_SEED_VALUES,
    )
    from trellis_cli.admin import (  # noqa: PLC0415
        _lookup_candidate_payload,
        _render_promotion_adr,
    )

    event_log = get_registry().operational.event_log
    try:
        candidate = _lookup_candidate_payload(event_log, candidate_id)
    # ``_lookup_candidate_payload`` raises ``typer.Exit`` when the id is
    # unknown; translate that to a 404 for the HTTP surface.
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No WELL_KNOWN_CANDIDATE event with candidate_id={candidate_id!r}."
            ),
        ) from exc

    drafted_date = datetime.now(tz=UTC).date().isoformat()
    rendered = _render_promotion_adr(
        candidate=candidate,
        canonical_name_override=None,
        drafted_date=drafted_date,
        thresholds=dict(RECOMMENDED_SEED_VALUES),
    )
    _emit_review_decision(
        event_log,
        surface="schema_evolution",
        action="draft_adr",
        ctx=ctx,
        entity_id=candidate_id,
        detail={"open_string_value": candidate.get("open_string_value")},
    )
    return DraftAdrResponse(
        candidate_id=candidate_id,
        markdown=rendered,
        suggested_canonical_name=candidate.get("suggested_canonical_name"),
    )


# -- Section 4: Code-authoring proposals (read-only) -----------------------


@router.get("/code-proposals", response_model=CodeProposalListResponse)
def list_code_proposals(
    limit: int = Query(
        50, ge=1, le=500, description="Max PROPOSAL_DRAFTED events to return"
    ),
) -> CodeProposalListResponse:
    """List recent ``PROPOSAL_DRAFTED`` events with their markdown preview.

    Read-only surface — the Review view shows these for visibility but
    exposes no action. Mirrors the read in ``trellis admin
    list-proposals`` / ``show-proposal``.
    """
    event_log = get_registry().operational.event_log
    events = event_log.get_events(
        event_type=EventType.PROPOSAL_DRAFTED,
        limit=limit,
        order="desc",
    )
    rows: list[CodeProposalSummary] = []
    for event in events:
        payload = event.payload or {}
        rows.append(
            CodeProposalSummary(
                proposal_id=str(payload.get("proposal_id") or event.entity_id or ""),
                cluster_signature=str(payload.get("cluster_signature") or ""),
                source_file=_code_proposal_source_file(
                    str(payload.get("markdown_preview") or "")
                ),
                source_event_count=int(payload.get("source_event_count") or 0),
                markdown_preview=str(payload.get("markdown_preview") or ""),
                generated_at=event.occurred_at.isoformat(),
            )
        )
    return CodeProposalListResponse(count=len(rows), proposals=rows)


def _code_proposal_source_file(preview: str) -> str | None:
    """Extract the source-file token from a proposal markdown preview.

    Reuses ``trellis_cli.admin_proposals._parse_source_file_from_preview``
    so the UI and CLI agree on the parse.
    """
    from trellis_cli.admin_proposals import (  # noqa: PLC0415
        _parse_source_file_from_preview,
    )

    return _parse_source_file_from_preview(preview)


# ===========================================================================
#  Scheduled jobs — ``GET``/``PATCH /admin/jobs``,
#  ``POST /admin/jobs/{name}/run``
# ===========================================================================
#
# The writable surface is exactly ``enabled``, ``cadence``,
# ``timeout_seconds`` (via PATCH) and ``run_requested_at`` (via run) — the
# same fields :class:`~trellis.schemas.schedule.ScheduledJob` exposes, and
# only for a ``name`` already present in :data:`trellis.schedule.catalog.
# JOB_CATALOG`. No route here creates, deletes, or can ever carry a
# command, argv, path or environment variable: ``AdminJobPatchRequest``
# has no such field to forbid selectively, and ``extra="forbid"``
# (:class:`~trellis_wire.base.WireRequestModel`) refuses any other field
# name outright. See ``trellis.schedule.catalog``'s module docstring for
# the full boundary argument.

#: Floor for the dispatcher-staleness window, in seconds, used when no
#: enabled+stored job has a cadence to measure against. A dispatcher that
#: has not started any job within the window reads as "not reporting" to
#: the UI banner.
_DISPATCHER_STALE_SECONDS_FLOOR = 15 * 60

#: Slack added on top of the longest enabled cadence's own tick interval
#: (see ``_dispatcher_stale_seconds``), so a daily job's dispatcher isn't
#: flagged "not reporting" for the ~23 hours between its ticks.
_DISPATCHER_STALE_SLACK_SECONDS = 2 * 60 * 60


def _get_schedule_store() -> ScheduleStore:
    """Build a schedule store from the registry's stores directory.

    Deliberately not cached — same rationale as ``policies.py``'s
    ``_get_policy_store``: a host CLI and this containerised API both
    write the same bind-mounted ``schedule.json``, so a cached store
    could silently roll back a concurrent write (#413's defect).
    :meth:`ScheduleStore.refuse_if_stale` backstops the window a fresh
    read-per-call cannot close on its own.
    """
    registry = get_registry()
    stores_dir = registry.stores_dir
    if stores_dir is None:
        msg = "stores_dir must be set on registry to use ScheduleStore"
        raise ValueError(msg)
    return ScheduleStore(stores_dir / "schedule.json")


def _schedule_result_http_error(result: CommandResult) -> HTTPException:
    """Map a non-SUCCESS schedule-admin ``CommandResult`` to an HTTPException.

    ``ScheduleUpdateHandler`` / ``JobRunRequestHandler`` convert every
    store refusal into a ``ValidationError`` whose ``code`` becomes
    ``metadata["rejection_reason"]`` — ``degraded_store_write`` or
    ``stale_store_write`` — so those two read 409 here, same as the old
    direct-``store.put()`` path did (mirrors ``policies.py``'s
    ``_refusal_http_error``: 409, not 503, because a degraded file does
    not repair itself). ``result.message`` is the handler's own text —
    the store's refusal message, recovery command baked in by
    ``StoreWriteRefusedError.__init__`` — so ``recovery`` here duplicates
    ``message`` rather than carrying a second value: a ``CommandResult``
    has no channel beyond ``message`` and ``metadata["rejection_reason"]``
    for what a handler-raised exception said (``executor.py``'s
    ``except ValidationError`` branch). Anything else REJECTED (bad
    cadence, unknown field, catalog mismatch) is the caller's own input,
    so it reads 422 with the raw message, matching the field-level 422s
    ``AdminJobPatchRequest`` already raises for a malformed body. A
    non-REJECTED failure (a defect, not an expected refusal) reads 500.
    """
    reason = result.metadata.get("rejection_reason")
    if result.status == CommandStatus.REJECTED and reason in STORE_REFUSAL_REASONS:
        return HTTPException(
            status_code=409,
            detail={
                "code": reason,
                "message": result.message,
                "recovery": result.message,
            },
        )
    if result.status == CommandStatus.REJECTED:
        return HTTPException(status_code=422, detail=result.message)
    return HTTPException(status_code=500, detail=sanitize_error_message(result.message))


def _last_job_run(event_log: EventLog, name: str) -> AdminJobLastRun | None:
    """The most recent run record for *name*, derived from the EventLog.

    Reads the latest ``JOB_RUN_COMPLETED`` and the latest
    ``JOB_RUN_STARTED`` and prefers whichever is more recent: normally
    that is a completed pair, but when the latest start has no matching
    completion yet (a run in flight, or a dispatcher that crashed before
    reporting), this reports that started run with every other field
    ``None`` rather than silently falling back to an older, finished run.
    ``None`` only when the EventLog has no record of this job at all —
    never a hardcoded literal (the ``/health`` route's status fields
    must not be repeated here).
    """
    entity_id = f"job:{name}"
    completed = event_log.get_events(
        event_type=EventType.JOB_RUN_COMPLETED,
        entity_id=entity_id,
        order="desc",
        limit=1,
    )
    started = event_log.get_events(
        event_type=EventType.JOB_RUN_STARTED,
        entity_id=entity_id,
        order="desc",
        limit=1,
    )
    if not completed and not started:
        return None
    if started and (not completed or started[0].occurred_at > completed[0].occurred_at):
        payload = started[0].payload or {}
        return AdminJobLastRun(
            run_id=payload.get("run_id"),
            started_at=payload.get("started_at"),
        )
    payload = completed[0].payload or {}
    return AdminJobLastRun(
        run_id=payload.get("run_id"),
        started_at=payload.get("started_at"),
        finished_at=payload.get("finished_at"),
        duration_ms=payload.get("duration_ms"),
        exit_code=payload.get("exit_code"),
        status=payload.get("status"),
    )


def _dispatcher_last_seen_at(event_log: EventLog) -> datetime | None:
    """The most recent ``JOB_RUN_STARTED`` across every catalog job.

    No ``entity_id`` filter — this is a dispatcher-liveness signal, not a
    per-job one: any job starting at all means some dispatcher polled
    ``due-jobs`` recently.
    """
    events = event_log.get_events(
        event_type=EventType.JOB_RUN_STARTED,
        order="desc",
        limit=1,
    )
    return events[0].occurred_at if events else None


def _job_row_or_default(store: ScheduleStore, spec: JobSpec) -> ScheduledJob:
    """The schedule-registry row for *spec*, or a fresh, unsaved default.

    Mirrors ``trellis_cli.schedule_seed.default_scheduled_jobs``'s shape
    for a job with no row yet, so a job always appears in the listing
    even before ``init-schedule`` or a first ``PATCH`` has ever run.
    Nothing is written here — this is a read-time convenience only, and
    writing one of these is not how a job gets created (it is already in
    :data:`trellis.schedule.catalog.JOB_CATALOG`; nothing here ever adds
    to or removes from that catalog).

    On a *degraded* store the default's ``enabled`` is ``False``, not
    :class:`ScheduledJob`'s own default of ``True``: a missing row here
    might be a row that failed to parse, whose real value is unknown and
    may be the operator's own ``enabled=False`` — showing ``True`` would
    read as a stored, operator-chosen decision it is not (gate #860,
    blocker 1, probe P5: disable a job, corrupt its row, and the listing
    must not then show it enabled). A *clean* store with no row at all
    keeps the schema's own default — that job was simply never seeded,
    and "would run enabled once initialised" is accurate, matching what
    ``init-schedule`` would write.
    """
    row = store.get(spec.name)
    if row is not None:
        return row
    return ScheduledJob(
        name=spec.name, cadence=spec.default_cadence, enabled=not store.is_degraded
    )


#: Multiplier applied to a loop's own cadence tick interval before adding
#: slack, when deciding whether its last run is stale
#: (``_loop_schedule_status_and_staleness``). Two missed ticks, not one: a
#: single late tick (dispatcher jitter, a run that started a few minutes
#: after the hour) must not read as the loop having gone silent.
_LOOP_STALE_CADENCE_MULTIPLIER = 2

#: Slack seconds added on top of that doubled interval. Reuses the same
#: value as the dispatcher-staleness window's own slack
#: (``_DISPATCHER_STALE_SLACK_SECONDS``) for the identical reason — tick
#: timing is never exact — but is named separately because the two
#: windows answer different questions (is the dispatcher alive at all,
#: vs has this one loop's own job reported recently).
_LOOP_STALE_SLACK_SECONDS = _DISPATCHER_STALE_SLACK_SECONDS


def _loop_schedule_status_and_staleness(
    job_name: str | None,
    last_run_at: datetime | None,
    store: ScheduleStore,
    now: datetime,
) -> tuple[str | None, bool]:
    """A Loops-tab row's schedule status, and whether its last run is stale.

    ``job_name`` is ``None`` for a loop no catalog job drives at all (a
    human action, or an agent's own call) — that row's ``schedule_status``
    is ``None`` and it is never ``stale``: staleness is a claim about a
    job that was supposed to run again by now, which does not apply to
    something nothing schedules. When ``job_name`` names a catalog job
    with no *stored*, *enabled* row with a cadence — never patched via
    ``PATCH /admin/jobs``, disabled, or manual-only — ``schedule_status``
    is ``"not_scheduled"`` and ``stale`` is ``False``: nothing promises
    this job will run again, so "overdue" does not apply either.
    Otherwise ``schedule_status`` is ``"scheduled"``, and ``stale`` is
    ``True`` only once a *real* last run (``last_run_at`` is not ``None``
    — a loop that has never run says so via that field already, not via
    this one) is older than twice the job's own tick interval plus slack
    (see :data:`_LOOP_STALE_CADENCE_MULTIPLIER` /
    :data:`_LOOP_STALE_SLACK_SECONDS`), reusing the same
    "stored-or-catalog cadence" read :func:`_job_row_or_default` already
    makes for the Schedules tab, rather than a UI-side constant that
    cannot see any of this.
    """
    if job_name is None:
        return None, False
    spec = JOB_CATALOG.get(job_name)
    if spec is None:
        return None, False
    row = _job_row_or_default(store, spec)
    stored = store.get(spec.name) is not None
    if not (stored and row.enabled and row.cadence is not None):
        return "not_scheduled", False
    if last_run_at is None:
        return "scheduled", False
    first_tick = next_tick_after(row.cadence, now)
    if first_tick is None:
        return "scheduled", False
    second_tick = next_tick_after(row.cadence, first_tick)
    if second_tick is None:
        return "scheduled", False
    interval_seconds = (second_tick - first_tick).total_seconds()
    threshold_seconds = (
        _LOOP_STALE_CADENCE_MULTIPLIER * interval_seconds + _LOOP_STALE_SLACK_SECONDS
    )
    age_seconds = (now - last_run_at).total_seconds()
    return "scheduled", age_seconds > threshold_seconds


def _job_summary(
    spec: JobSpec,
    row: ScheduledJob,
    event_log: EventLog,
    *,
    now: datetime,
    stored: bool,
) -> AdminJobSummary:
    """Join one catalog entry with its schedule-registry row and run history.

    ``stored`` is ``False`` when *row* is a materialized catalog default
    rather than something ``schedule.json`` actually holds (no row yet,
    or a row that failed to parse out of a degraded store — see
    ``_job_row_or_default``). ``next_due_at`` is unconditionally ``None``
    in that case: ``trellis admin due-jobs`` only ever evaluates a
    *stored* row, so a catalog default showing a next-due timestamp
    would promise a run that cannot happen (gate #860, blocker 1).
    """
    effective_timeout = (
        row.timeout_seconds
        if row.timeout_seconds is not None
        else spec.default_timeout_seconds
    )
    next_due_at: str | None = None
    if stored and row.enabled and row.cadence is not None:
        tick = next_tick_after(row.cadence, now)
        if tick is not None:
            next_due_at = tick.isoformat()
    return AdminJobSummary(
        name=spec.name,
        description=spec.description,
        host_only=spec.host_only,
        spends_llm=spec.spends_llm,
        heavy=spec.heavy,
        enabled=row.enabled,
        cadence=row.cadence,
        default_cadence=spec.default_cadence,
        timeout_seconds=effective_timeout,
        run_requested_at=(
            row.run_requested_at.isoformat()
            if row.run_requested_at is not None
            else None
        ),
        last_run=_last_job_run(event_log, spec.name),
        next_due_at=next_due_at,
        stored=stored,
        loops=list(JOB_LOOPS.get(spec.name, ())),
    )


def _longest_enabled_cadence_interval_seconds(
    jobs: Sequence[AdminJobSummary], now: datetime
) -> int | None:
    """Seconds between consecutive ticks of the slowest enabled cadence.

    Only *stored* (``job.stored``), *enabled* jobs with a cadence count —
    a catalog default or a disabled job never runs, so its cadence says
    nothing about how often the dispatcher should be heard from. ``None``
    means no such job exists; the caller falls back to the floor.
    """
    longest: int | None = None
    for job in jobs:
        if not (job.stored and job.enabled and job.cadence):
            continue
        first = next_tick_after(job.cadence, now)
        if first is None:
            continue
        second = next_tick_after(job.cadence, first)
        if second is None:
            continue
        interval = int((second - first).total_seconds())
        if longest is None or interval > longest:
            longest = interval
    return longest


def _dispatcher_stale_seconds(jobs: Sequence[AdminJobSummary], now: datetime) -> int:
    """How long since the last job-run start before the dispatcher reads as silent.

    A fixed 15-minute window false-positives against a daily cadence (gate
    #860, blocker 5): the window must be at least as long as the slowest
    enabled cadence's own tick interval, plus slack, never a literal
    compared against every cadence regardless of period.
    """
    longest = _longest_enabled_cadence_interval_seconds(jobs, now)
    if longest is None:
        return _DISPATCHER_STALE_SECONDS_FLOOR
    return max(
        _DISPATCHER_STALE_SECONDS_FLOOR, longest + _DISPATCHER_STALE_SLACK_SECONDS
    )


@router.get("/admin/jobs", response_model=AdminJobListResponse)
def list_admin_jobs() -> AdminJobListResponse:
    """List every catalog job, joined with its schedule row and last-run status.

    A job always appears here even with no schedule-registry row yet
    (materialized with catalog defaults — see ``_job_row_or_default``);
    this route never creates one. ``dispatcher_seen``/
    ``dispatcher_last_seen_at`` answer the UI's "no dispatcher reporting"
    banner from the EventLog's own ``JOB_RUN_STARTED`` events, never a
    hardcoded literal. ``store_degradation`` surfaces a degraded schedule
    store the same way ``routes/policies.py`` does, so the UI can show a
    banner instead of silently rendering catalog defaults as if they were
    stored, operator-chosen values (gate #860, blocker 1).
    """
    registry = get_registry()
    event_log = registry.operational.event_log
    store = _get_schedule_store()
    now = utc_now()
    jobs = [
        _job_summary(
            spec,
            _job_row_or_default(store, spec),
            event_log,
            now=now,
            stored=store.get(spec.name) is not None,
        )
        for spec in JOB_CATALOG.values()
    ]
    dispatcher_last_seen_at = _dispatcher_last_seen_at(event_log)
    stale_seconds = _dispatcher_stale_seconds(jobs, now)
    dispatcher_seen = (
        dispatcher_last_seen_at is not None
        and (now - dispatcher_last_seen_at).total_seconds() <= stale_seconds
    )
    degradation = store.degradation
    return AdminJobListResponse(
        count=len(jobs),
        jobs=jobs,
        dispatcher_last_seen_at=(
            dispatcher_last_seen_at.isoformat()
            if dispatcher_last_seen_at is not None
            else None
        ),
        dispatcher_seen=dispatcher_seen,
        store_degradation=(degradation.to_dict() if degradation is not None else None),
    )


@router.patch(
    "/admin/jobs/{name}",
    response_model=AdminJobPatchResponse,
    responses={
        422: {
            "description": (
                "``body`` fails ``AdminJobPatchRequest`` validation "
                "(DefaultValidationError -- ``detail`` is a list of field "
                "errors), or the schedule-update command is REJECTED for a "
                "reason other than a degraded/stale store "
                "(StringDetailError -- ``detail`` is a plain string; see "
                "``_schedule_result_http_error``)."
            ),
            "model": DefaultValidationError | StringDetailError,
        }
    },
)
def patch_admin_job(
    name: str,
    body: AdminJobPatchRequest,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> AdminJobPatchResponse:
    """Edit a catalog job's mutable schedule fields.

    The writable surface is exactly ``enabled``, ``cadence`` (cron-
    validated) and ``timeout_seconds`` (bounded 1..86400) —
    ``AdminJobPatchRequest``'s only fields, with ``extra="forbid"``
    refusing anything else. ``name`` must already be a
    :data:`trellis.schedule.catalog.JOB_CATALOG` key (404 otherwise);
    this route never creates or deletes a job. Only fields the caller
    actually set (``body.model_fields_set``) are changed — omitting a
    field leaves it unchanged; an empty body is a no-op that writes
    nothing and emits no event. A write while the schedule store is
    degraded or stale is refused with 409 and the store's own error
    text. Every applied change emits ``SCHEDULE_UPDATED`` with ``old``/
    ``new`` dicts (only the fields that changed) and the caller's
    identity.
    """
    spec = JOB_CATALOG.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown job: {name}")
    store = _get_schedule_store()
    current = _job_row_or_default(store, spec)
    registry = get_registry()
    event_log = registry.operational.event_log

    fields_set = body.model_fields_set
    if not fields_set:
        return AdminJobPatchResponse(
            job=_job_summary(
                spec,
                current,
                event_log,
                now=utc_now(),
                stored=store.get(spec.name) is not None,
            )
        )

    args: dict[str, Any] = {}
    if "enabled" in fields_set:
        args["enabled"] = body.enabled
    if "cadence" in fields_set:
        args["cadence"] = body.cadence
    if "timeout_seconds" in fields_set:
        args["timeout_seconds"] = body.timeout_seconds

    command = Command(
        operation=Operation.SCHEDULE_UPDATE,
        target_type="schedule",
        target_id=name,
        args=args,
        requested_by="api:admin.schedule-update",
        metadata=_audit_identity(ctx),
    )
    result = build_schedule_executor(registry).execute(command)
    if result.status != CommandStatus.SUCCESS:
        raise _schedule_result_http_error(result)

    # Re-read: ``store`` cached its rows at construction, before the
    # handler's own ``ScheduleStore`` instance committed the write.
    updated_store = _get_schedule_store()
    updated = _job_row_or_default(updated_store, spec)
    return AdminJobPatchResponse(
        job=_job_summary(
            spec,
            updated,
            event_log,
            now=utc_now(),
            stored=updated_store.get(spec.name) is not None,
        )
    )


@router.post(
    "/admin/jobs/{name}/run",
    response_model=AdminJobRunResponse,
    responses={
        422: {
            "description": (
                "The job-run-request command is REJECTED for a reason "
                "other than a degraded/stale store -- ``detail`` is a "
                "plain string (see ``_schedule_result_http_error``)."
            ),
            "model": StringDetailError,
        }
    },
)
def run_admin_job(
    name: str,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> AdminJobRunResponse:
    """Request an out-of-band run of a catalog job ("Run now").

    Writes only ``run_requested_at`` on the schedule-registry row — this
    route never executes the job inline and never claims to.
    :func:`trellis.core.cron.is_job_due` treats a ``run_requested_at``
    newer than the job's last completed run as always due, so a host
    dispatcher polling ``due-jobs`` on its own cadence (typically within
    ~5 minutes, if one is installed at all — see
    ``docs/ops/job-dispatcher.sh.example``) is what actually picks the
    job up from there. ``name`` must already be a
    :data:`trellis.schedule.catalog.JOB_CATALOG` key (404 otherwise). A
    write while the schedule store is degraded or stale is refused with
    409 and the store's own error text. Emits ``JOB_RUN_REQUESTED`` with
    the caller's identity.
    """
    spec = JOB_CATALOG.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown job: {name}")

    registry = get_registry()
    command = Command(
        operation=Operation.JOB_RUN_REQUEST,
        target_type="schedule",
        target_id=name,
        requested_by="api:admin.job-run-request",
        metadata=_audit_identity(ctx),
    )
    result = build_schedule_executor(registry).execute(command)
    if result.status != CommandStatus.SUCCESS:
        raise _schedule_result_http_error(result)

    # Re-read rather than trust a locally-computed timestamp: the
    # handler, not this route, is what decided and wrote ``now``. A
    # successful command always writes ``run_requested_at`` server-side,
    # but we report exactly what is on the row — never a value fabricated
    # here — so a future handler change can't make this response lie
    # (gate #860, F6).
    updated = _job_row_or_default(_get_schedule_store(), spec)
    run_requested_at = (
        updated.run_requested_at.isoformat()
        if updated.run_requested_at is not None
        else None
    )
    return AdminJobRunResponse(name=name, run_requested_at=run_requested_at)
