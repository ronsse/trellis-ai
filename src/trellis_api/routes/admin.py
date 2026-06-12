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
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from trellis.learning.scoring import (
    prepare_learning_promotions,
    submit_learning_promotion,
)
from trellis.learning.tuners import (
    preview_promotion,
    promote_proposal,
    reject_proposal,
)
from trellis.mutate import build_curate_executor
from trellis.retrieve.advisory_generator import AdvisoryGenerator
from trellis.retrieve.effectiveness import (
    analyze_effectiveness,
    run_effectiveness_feedback,
)
from trellis.retrieve.metrics_timeseries import compute_timeseries
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventLog, EventType
from trellis_api.app import get_registry
from trellis_api.auth import AuthContext, authenticate
from trellis_wire.dtos import (
    CodeProposalListResponse,
    CodeProposalSummary,
    DraftAdrResponse,
    HealthResponse,
    LearningCandidateListResponse,
    LearningPromotionRequest,
    LearningPromotionResponse,
    LearningPromotionResultRow,
    MetricsTimeseriesResponse,
    ProposalDecisionResponse,
    ProposalPreviewResponse,
    ProposalRejectRequest,
    SchemaEvolutionCandidate,
    SchemaEvolutionListResponse,
    StatsResponse,
    TimeseriesPointResponse,
    TimeseriesSeriesResponse,
    TunerProposalListResponse,
    TunerProposalSummary,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Check API and store health."""
    return HealthResponse(status="ok", checks={"api": True, "stores": True})


@router.get("/stats", response_model=StatsResponse)
def stats() -> StatsResponse:
    """Get store statistics."""
    registry = get_registry()
    return StatsResponse(
        traces=registry.operational.trace_store.count(),
        documents=registry.knowledge.document_store.count(),
        nodes=registry.knowledge.graph_store.count_nodes(),
        edges=registry.knowledge.graph_store.count_edges(),
        events=registry.operational.event_log.count(),
    )


@router.get("/effectiveness")
def effectiveness(
    days: int = Query(30, description="Days of history to analyze"),
    min_appearances: int = Query(2, description="Minimum item appearances"),
) -> dict[str, Any]:
    """Analyze context pack effectiveness."""
    registry = get_registry()
    report = analyze_effectiveness(
        registry.operational.event_log,
        days=days,
        min_appearances=min_appearances,
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
    report = run_effectiveness_feedback(
        registry.operational.event_log,
        registry.knowledge.document_store,
        days=days,
        min_appearances=min_appearances,
    )
    return {
        "status": "ok",
        "noise_candidates_tagged": len(report.noise_candidates),
        **report.model_dump(),
    }


# -- Improvement-metrics dashboard (WP11) --


@router.get("/metrics/timeseries", response_model=MetricsTimeseriesResponse)
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
        raise HTTPException(status_code=422, detail=str(exc)) from exc

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
    )


# -- Advisories --


@router.post("/advisories/generate")
def generate_advisories(
    days: int = Query(30, description="Days of history to analyze"),
    min_sample: int = Query(5, description="Min sample size"),
    min_effect: float = Query(0.15, description="Min effect size"),
) -> dict[str, Any]:
    """Generate advisories from outcome data.

    Analyzes PACK_ASSEMBLED and FEEDBACK_RECORDED events to find patterns,
    then stores deterministic advisories for delivery alongside packs.
    """
    registry = get_registry()
    stores_dir = registry.stores_dir
    if stores_dir is None:
        return {"status": "error", "message": "stores_dir not configured"}
    store = AdvisoryStore(stores_dir / "advisories.json")
    generator = AdvisoryGenerator(
        registry.operational.event_log,
        store,
        min_sample_size=min_sample,
        min_effect_size=min_effect,
    )
    report = generator.generate(days=days)
    return {"status": "ok", **report.model_dump()}


@router.get("/advisories")
def list_advisories(
    scope: str | None = Query(None, description="Filter by scope"),
    min_confidence: float = Query(0.0, description="Minimum confidence"),
) -> dict[str, Any]:
    """List stored advisories."""
    registry = get_registry()
    stores_dir = registry.stores_dir
    if stores_dir is None:
        return {"status": "error", "message": "stores_dir not configured"}
    store = AdvisoryStore(stores_dir / "advisories.json")
    advisories = store.list(scope=scope, min_confidence=min_confidence)
    return {
        "count": len(advisories),
        "advisories": [a.model_dump(mode="json") for a in advisories],
    }


# -- Vector store management --


@router.post("/vectors/reset")
def reset_vectors() -> dict[str, Any]:
    """Drop and recreate the vectors table with current configured dimensions."""
    registry = get_registry()
    vector_store = getattr(registry.knowledge, "vector_store", None)
    if vector_store is None:
        return {"status": "error", "message": "Vector store not configured"}

    try:
        # PgVectorStore exposes the pooled-connection helper ``_conn``
        # inherited from ``PostgresStoreBase``; SQLite's vector store
        # uses a plain ``sqlite3.Connection`` at ``_conn`` whose
        # ``execute`` runs SQL directly. Detect by attribute capability,
        # not type, so the route stays backend-agnostic.
        if hasattr(vector_store, "_pool"):
            with vector_store._conn() as conn, conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS vectors")
        else:
            vector_store._conn.execute("DROP TABLE IF EXISTS vectors")
            vector_store._conn.commit()
        vector_store._init_schema()
    # GRACEFUL-DEGRADATION: admin operator endpoint surfaces failure as
    # a structured JSON response (rather than a 5xx) so the caller can
    # display the message in the admin UI. Exception is logged for
    # operator visibility.
    except Exception as exc:
        logger.exception("vectors_reset_failed")
        return {"status": "error", "message": str(exc)}
    else:
        dims = vector_store._dimensions
        return {"status": "ok", "message": f"Recreated with {dims}D"}


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

#: Env var pointing at the directory the learning-candidate artifacts are
#: written to (``trellis analyze learning-candidates --output-dir``). When
#: unset we fall back to ``<data_dir>/learning`` (sibling of ``stores/``)
#: so a conventional install resolves without extra config.
LEARNING_ARTIFACTS_DIR_ENV = "TRELLIS_LEARNING_ARTIFACTS_DIR"

#: Filename the CLI writes the scored learning report to. Must match
#: ``trellis.learning.scoring.write_learning_review_artifacts``.
_LEARNING_CANDIDATES_FILENAME = "intent_learning_candidates.json"


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


def _resolve_learning_artifacts_dir() -> Path | None:
    """Return the directory holding learning-candidate artifacts, or ``None``.

    Honours ``TRELLIS_LEARNING_ARTIFACTS_DIR`` first, then falls back to
    ``<data_dir>/learning`` derived from the registry's ``stores_dir``.
    """
    override = os.environ.get(LEARNING_ARTIFACTS_DIR_ENV)
    if override and override.strip():
        return Path(override.strip())
    stores_dir = get_registry().stores_dir
    if stores_dir is None:
        return None
    # stores_dir is ``<data_dir>/stores``; artifacts live beside it.
    return stores_dir.parent / "learning"


# -- Section 1: Tuner proposals --------------------------------------------


@router.get("/proposals", response_model=TunerProposalListResponse)
def list_pending_proposals(
    limit: int = Query(100, description="Max proposals to return"),
) -> TunerProposalListResponse:
    """List pending tuner proposals awaiting a human approve / reject.

    Surfaces effect_size / sample_size / baseline / proposed_values per
    proposal so the operator can judge each one. Mirrors the read in
    ``trellis metrics proposals --status pending`` but enriches each row
    with the resolved baseline values (what the proposal is measured
    against).
    """
    registry = get_registry()
    tuner_state = registry.operational.tuner_state_store
    params = registry.operational.parameter_store
    proposals = tuner_state.list_proposals(status="pending", limit=limit)

    rows: list[TunerProposalSummary] = []
    for p in proposals:
        baseline = params.resolve(p.scope)
        baseline_values = dict(baseline.values) if baseline else {}
        rows.append(
            TunerProposalSummary(
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


@router.get("/proposals/{proposal_id}/preview", response_model=ProposalPreviewResponse)
def preview_proposal(proposal_id: str) -> ProposalPreviewResponse:
    """Dry-run a proposal promotion — predict the decision, mutate nothing.

    Backs the UI confirm step: the operator sees the predicted
    promote / reject outcome (and why) before committing. Wraps the same
    :func:`trellis.learning.tuners.preview_promotion` the CLI dry-run uses.
    """
    registry = get_registry()
    preview = preview_promotion(
        proposal_id,
        tuner_state=registry.operational.tuner_state_store,
        parameter_store=registry.operational.parameter_store,
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
    ``TUNER_PROPOSAL_REJECTED`` on a policy rejection). A second
    ``REVIEW_DECISION_RECORDED`` event records the reviewer identity.
    """
    registry = get_registry()
    result = promote_proposal(
        proposal_id,
        tuner_state=registry.operational.tuner_state_store,
        parameter_store=registry.operational.parameter_store,
        event_log=registry.operational.event_log,
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

    The CLI writes this file via ``trellis analyze learning-candidates
    --output-dir <dir>``. We resolve ``<dir>`` from
    ``TRELLIS_LEARNING_ARTIFACTS_DIR`` (or ``<data_dir>/learning``). When
    no artifact is found we return an empty list plus a ``hint`` telling
    the operator how to generate one — never a 5xx.
    """
    artifacts_dir = _resolve_learning_artifacts_dir()
    hint = (
        "Run 'trellis analyze learning-candidates --output-dir "
        f"{artifacts_dir or '<dir>'}' to generate candidates, or set "
        f"{LEARNING_ARTIFACTS_DIR_ENV} to point at an existing artifacts "
        "directory."
    )
    if artifacts_dir is None:
        return LearningCandidateListResponse(hint=hint)

    candidates_path = artifacts_dir / _LEARNING_CANDIDATES_FILENAME
    if not candidates_path.is_file():
        return LearningCandidateListResponse(hint=hint)

    try:
        payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    # GRACEFUL-DEGRADATION: a malformed artifact must not 500 the inbox;
    # surface it as an empty list with the file path in the hint.
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("learning_candidates_read_failed", error=str(exc))
        return LearningCandidateListResponse(
            hint=f"Could not read {candidates_path}: {exc}"
        )

    candidates = payload.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = []
    return LearningCandidateListResponse(
        generated_at_utc=payload.get("generated_at_utc"),
        candidate_count=len(candidates),
        candidates=candidates,
    )


@router.post("/learning/promotions", response_model=LearningPromotionResponse)
def promote_learning_candidates(
    req: LearningPromotionRequest,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> LearningPromotionResponse:
    """Promote approved learning candidates through the governed pipeline.

    Builds the decisions payload from the request body, joins it against
    the most-recent candidate artifact via
    :func:`trellis.learning.prepare_learning_promotions`, then submits
    each approved promotion as ``ENTITY_CREATE`` + per-target
    ``LINK_CREATE`` commands through :class:`MutationExecutor` — exactly
    the path ``trellis curate promote-learning`` uses. A
    ``REVIEW_DECISION_RECORDED`` event records the reviewer identity.
    """
    artifacts_dir = _resolve_learning_artifacts_dir()
    candidates_path = (
        (artifacts_dir / _LEARNING_CANDIDATES_FILENAME)
        if artifacts_dir is not None
        else None
    )
    if candidates_path is None or not candidates_path.is_file():
        raise HTTPException(
            status_code=409,
            detail=(
                "No learning-candidate artifact found. Run 'trellis analyze "
                "learning-candidates' first, or set "
                f"{LEARNING_ARTIFACTS_DIR_ENV}."
            ),
        )
    candidates_payload = json.loads(candidates_path.read_text(encoding="utf-8"))

    decisions_payload = {
        "decisions": [d.model_dump() for d in req.decisions],
    }
    plan = prepare_learning_promotions(
        candidates_payload=candidates_payload,
        decisions_payload=decisions_payload,
    )
    ready = [r for r in plan["results"] if r["status"] == "ready"]

    registry = get_registry()
    executor = build_curate_executor(registry)
    rows: list[LearningPromotionResultRow] = []
    promoted_count = 0
    for entry in plan["results"]:
        if entry["status"] != "ready":
            rows.append(
                LearningPromotionResultRow(
                    candidate_id=entry["candidate_id"], status=entry["status"]
                )
            )
            continue
        outcome = submit_learning_promotion(
            executor,
            entry["entity_payload"],
            entry["edge_payloads"],
            requested_by="api:review.promote-learning",
        )
        if outcome["status"] == "promoted":
            promoted_count += 1
        rows.append(
            LearningPromotionResultRow(
                candidate_id=entry["candidate_id"],
                status=outcome["status"],
                entity_id=entry["entity_id"],
                node_id=outcome.get("node_id"),
                message=outcome.get("message"),
            )
        )

    _emit_review_decision(
        registry.operational.event_log,
        surface="learning_promotion",
        action="promote",
        ctx=ctx,
        entity_id=None,
        detail={
            "approved_count": plan["approved_count"],
            "ready_count": len(ready),
            "promoted_count": promoted_count,
            "candidate_ids": [r.candidate_id for r in rows],
        },
    )
    return LearningPromotionResponse(
        approved_count=plan["approved_count"],
        ready_count=len(ready),
        promoted_count=promoted_count,
        results=rows,
    )


# -- Section 3: Schema-evolution candidates --------------------------------


@router.get("/schema-evolution/candidates", response_model=SchemaEvolutionListResponse)
def list_schema_evolution_candidates(
    limit: int = Query(200, description="Max WELL_KNOWN_CANDIDATE events to scan"),
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
    limit: int = Query(50, description="Max PROPOSAL_DRAFTED events to return"),
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
