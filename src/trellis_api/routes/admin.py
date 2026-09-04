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
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from trellis.core.error_sanitize import sanitize_error_message
from trellis.core.vector_metadata import resolve_vector_store
from trellis.errors import StaleStoreWriteError
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
from trellis.stores.advisory_source import load_advisory_store
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.base.vector import VectorStore
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
        # #338: mirror the demotion onto the vector row, or the semantic
        # axis keeps serving the pre-demotion snapshot.
        vector_store=resolve_vector_store(registry),
    )
    # What the evidence gate admitted, not what the usage-rate rule
    # proposed (#336) — the key says "tagged", so it has to count writes.
    # ``demotion_screen`` in the dumped report carries the full accounting.
    screen = report.demotion_screen
    tagged = (
        len(screen.admitted) if screen is not None else len(report.noise_candidates)
    )
    return {
        "status": "ok",
        "noise_candidates_tagged": tagged,
        "noise_candidates_proposed": len(report.noise_candidates),
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
    min_sample: int = Query(5, description="Min sample size"),
    min_effect: float = Query(0.15, description="Min effect size"),
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
        min_sample_size=min_sample,
        min_effect_size=min_effect,
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
    # ``count`` silently under-reports on a partial load, so a corrupt file
    # would present to an API client (and to the Memory Explorer) exactly as
    # a deployment that has never generated an advisory — the state
    # ``advisory_source`` exists to make distinguishable (#393). The rows
    # that parsed are still returned: the read stays lenient.
    return {
        "count": len(advisories),
        "advisories": [a.model_dump(mode="json") for a in advisories],
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
    machine-readable half. They do **not** share a message. "Keeps no
    `vectors` table" is something the ABC's declaration entitles us to say
    about a ``VectorStore`` that declined; it would be a fact invented
    about an object that never implemented the interface, which is the
    #512 failure mode in the sentence that fixes #512.
    """
    if isinstance(vector_store, VectorStore):
        if vector_store.supports_reset():
            return None
        detail = (
            f"{type(vector_store).__name__} keeps no `vectors` table "
            "for this route to drop and recreate."
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
                "route cannot reset — a backend that keeps no `vectors` "
                "table to drop (ArcadeDB and Neo4j hold vectors as "
                "graph-node state), or an object that does not implement "
                "the vector-store interface at all. "
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
