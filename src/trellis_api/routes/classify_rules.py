"""Classify-rules routes — review mined tag/domain candidates, apply or revoke.

Closes the loop ``trellis classify tag-candidates`` / ``domain-candidates``
leaves open: those commands mine and *propose* keyword/alias rules
(``TAG_KEYWORD_CANDIDATE`` / ``DOMAIN_ALIAS_CANDIDATE`` events) but never
write anywhere — approving one meant hand-pasting YAML into ``config.yaml``,
with no REST route, no UI, no audit trail. This module is the governed
"approve" step over
:class:`~trellis.stores.classify_overrides_store.ClassifyOverridesStore`
(the operator overlay) via ``Operation.CLASSIFY_RULE_APPLY`` /
``Operation.CLASSIFY_RULE_REVOKE`` (:mod:`trellis.mutate.handlers`).

Mounted with the read scope only (see ``trellis_api/app.py``), same mixed-scope
shape as ``policies.py`` / ``observations.py``: ``GET /classify/candidates``
and ``GET /classify/rules`` are read-scoped; the four write/action routes
(``POST /classify/candidates/mine``, ``POST /classify/rules``,
``DELETE /classify/rules/{kind}/{key}``, ``POST
/classify/candidates/{id}/dismiss``) are individually elevated to admin via
``dependencies=[Depends(require_scope(SCOPE_ADMIN))]`` on the route itself.

``GET /classify/candidates`` never mines — it only reads events an earlier
CLI or ``POST /classify/candidates/mine`` run already emitted, deduped to
each candidate's latest emission the same way
``admin.list_schema_evolution_candidates`` dedups schema-evolution
candidates: a Python-side latest-event-wins scan, not a second analyzer
pass.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from trellis.auth import SCOPE_ADMIN
from trellis.classify.factory import effective_classify_rule_rows
from trellis.core.error_sanitize import sanitize_error_message
from trellis.learning.domain_normalization import (
    DEFAULT_SCAN_LIMIT as _DOMAIN_DEFAULT_SCAN_LIMIT,
)
from trellis.learning.domain_normalization import (
    PARAM_COMPONENT_ID as _DOMAIN_PARAM_COMPONENT_ID,
)
from trellis.learning.domain_normalization import (
    RECOMMENDED_SEED_VALUES as _DOMAIN_RECOMMENDED_SEED_VALUES,
)
from trellis.learning.domain_normalization import (
    REQUIRED_PARAM_KEYS as _DOMAIN_REQUIRED_PARAM_KEYS,
)
from trellis.learning.domain_normalization import analyze_domain_alias_candidates
from trellis.learning.tag_evolution import (
    DEFAULT_SCAN_LIMIT as _TAG_DEFAULT_SCAN_LIMIT,
)
from trellis.learning.tag_evolution import (
    PARAM_COMPONENT_ID as _TAG_PARAM_COMPONENT_ID,
)
from trellis.learning.tag_evolution import (
    RECOMMENDED_SEED_VALUES as _TAG_RECOMMENDED_SEED_VALUES,
)
from trellis.learning.tag_evolution import (
    REQUIRED_PARAM_KEYS as _TAG_REQUIRED_PARAM_KEYS,
)
from trellis.learning.tag_evolution import analyze_tag_keyword_candidates
from trellis.mutate import (
    Command,
    CommandStatus,
    Operation,
    build_classify_rules_executor,
)
from trellis.ops import ParameterRegistry
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.base.event_log import Event, EventLog, EventType
from trellis.stores.base.parameter import ParameterStore
from trellis.stores.classify_overrides_store import (
    ClassifyOverridesStore,
    resolve_classify_overrides_path,
)
from trellis.stores.registry import StoreRegistry
from trellis_api.app import get_registry
from trellis_api.auth import AuthContext, authenticate, require_scope
from trellis_api.routes._results import command_response
from trellis_wire.dtos import (
    ClassifyCandidate,
    ClassifyCandidateDismissResponse,
    ClassifyCandidateListResponse,
    ClassifyMineRequest,
    ClassifyMineResponse,
    ClassifyRuleApplyRequest,
    ClassifyRuleListResponse,
    ClassifyRuleRow,
)

logger = structlog.get_logger(__name__)

router = APIRouter()

#: Candidate kinds this surface knows about. Any other value in
#: ``ClassifyMineRequest.kinds`` or a candidate row is a 400, not a
#: silent skip.
_VALID_KINDS = frozenset({"tag_keyword", "domain_alias"})

#: Payload keys every candidate event carries that the typed
#: :class:`~trellis_wire.dtos.ClassifyCandidate` fields already cover.
#: Everything else in the payload (``facet``, ``precision``, ``lift``,
#: ``support``, ``alias_documents``, ...) passes through ``metrics`` —
#: see that DTO's own docstring for why the two kinds share one field
#: instead of a second typed row shape.
_CANDIDATE_COMMON_KEYS = frozenset(
    {
        "candidate_id",
        "keyword",
        "tag",
        "alias",
        "canonical",
        "recurrence_count",
        "example_count",
        "notes",
    }
)

_CANDIDATE_EVENT_TYPES: dict[str, EventType] = {
    "tag_keyword": EventType.TAG_KEYWORD_CANDIDATE,
    "domain_alias": EventType.DOMAIN_ALIAS_CANDIDATE,
}

#: Events scanned per listing/dismiss-lookup call when the caller does not
#: name a tighter limit. Independent of a miner's own document ``scan_limit``.
_DEFAULT_EVENT_SCAN_LIMIT = 500


def _get_overrides_store(registry: StoreRegistry) -> ClassifyOverridesStore:
    """Build an overrides store from the registry's stores directory.

    Not cached, same reasoning as ``settings.py``'s ``_get_settings_store``
    and ``policies.py``'s ``_get_policy_store``: re-reading per call is
    what makes "an apply/revoke takes effect without a restart" true, and
    caching risks a stale in-memory copy clobbering a concurrent writer's
    row on this process's next write.
    """
    path = resolve_classify_overrides_path(registry.stores_dir)
    if path is None:
        msg = "stores_dir must be set on registry to use ClassifyOverridesStore"
        raise ValueError(msg)
    return ClassifyOverridesStore(path)


def _execute_classify_command(cmd: Command) -> Any:
    """Execute a classify-rule command through the mutation pipeline.

    Mirrors ``settings.py``'s module-private helper of the same shape:
    FAILED/REJECTED read as a 400-class outcome, except the
    degraded/stale-store rejection reasons the handlers raise
    (``ClassifyRuleApplyHandler`` / ``ClassifyRuleRevokeHandler`` both
    catch ``DegradedStoreWriteError`` / ``StaleStoreWriteError`` and
    re-raise with ``code=exc.code.lower()``), which read as 409 with the
    recovery command and (for a degraded load) the real
    ``store_degradation`` read fresh from the store, uncapped by
    ``sanitize_error_message``'s truncation.
    """
    result = build_classify_rules_executor(get_registry()).execute(cmd)
    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        reason = result.metadata.get("rejection_reason")
        if reason in ("degraded_store_write", "stale_store_write"):
            store = _get_overrides_store(get_registry())
            degradation = store.degradation
            recovery = (
                degradation.recovery
                if degradation is not None
                else store.stale_recovery
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": reason,
                    "message": sanitize_error_message(result.message),
                    "recovery": recovery,
                    "store_degradation": (
                        degradation.to_dict() if degradation is not None else None
                    ),
                },
            )
        raise HTTPException(
            status_code=400, detail=sanitize_error_message(result.message)
        )
    return command_response(result)


# ---------------------------------------------------------------------------
# Candidate listing — shared between GET /classify/candidates and the
# "candidates it just emitted" slice of POST /classify/candidates/mine.
# ---------------------------------------------------------------------------


def _candidate_key_target(kind: str, payload: dict[str, Any]) -> tuple[str, str]:
    if kind == "tag_keyword":
        return str(payload.get("keyword") or ""), str(payload.get("tag") or "")
    return str(payload.get("alias") or ""), str(payload.get("canonical") or "")


def _latest_candidate_events(
    event_log: EventLog, *, scan_limit: int
) -> dict[str, tuple[Event, str]]:
    """Latest ``TAG_KEYWORD_CANDIDATE``/``DOMAIN_ALIAS_CANDIDATE`` event per candidate.

    Scans each stream newest-first and keeps only the first (= latest)
    occurrence per ``candidate_id`` — the same latest-event-wins
    resolution :mod:`trellis.learning.cooldown` uses for recurrence, and
    ``admin.list_schema_evolution_candidates`` uses for its own
    single-stream case. Never re-mines: this only reads events an earlier
    ``tag-candidates``/``domain-candidates``/``mine`` run already emitted.
    """
    latest: dict[str, tuple[Event, str]] = {}
    for kind, event_type in _CANDIDATE_EVENT_TYPES.items():
        for event in event_log.get_events(
            event_type=event_type, limit=scan_limit, order="desc"
        ):
            candidate_id = str((event.payload or {}).get("candidate_id") or "")
            if candidate_id and candidate_id not in latest:
                latest[candidate_id] = (event, kind)
    return latest


def _latest_dismissals(event_log: EventLog, *, scan_limit: int) -> dict[str, Any]:
    """Latest ``CLASSIFY_CANDIDATE_DISMISSED`` emission per ``candidate_id``."""
    dismissed: dict[str, Any] = {}
    for event in event_log.get_events(
        event_type=EventType.CLASSIFY_CANDIDATE_DISMISSED,
        limit=scan_limit,
        order="desc",
    ):
        candidate_id = str((event.payload or {}).get("candidate_id") or "")
        if candidate_id and candidate_id not in dismissed:
            dismissed[candidate_id] = event.recorded_at
    return dismissed


def _candidate_status(
    *,
    kind: str,
    key: str,
    overlay_rows: dict[tuple[str, str], Any],
    event: Event,
    dismissed_at: Any,
) -> str:
    if (kind, key) in overlay_rows:
        return "applied"
    if dismissed_at is not None and dismissed_at >= event.recorded_at:
        return "dismissed"
    return "open"


def _to_candidate_row(event: Event, kind: str, status: str) -> ClassifyCandidate:
    payload = event.payload or {}
    key, target = _candidate_key_target(kind, payload)
    metrics = {k: v for k, v in payload.items() if k not in _CANDIDATE_COMMON_KEYS}
    return ClassifyCandidate(
        candidate_id=str(payload.get("candidate_id") or event.entity_id or ""),
        kind=kind,
        key=key,
        target=target,
        status=status,
        recurrence_count=int(payload.get("recurrence_count") or 0),
        example_count=int(payload.get("example_count") or 0),
        notes=[str(n) for n in (payload.get("notes") or [])],
        metrics=metrics,
        emitted_at=event.recorded_at.isoformat(),
        source_event_id=event.event_id,
    )


def _list_candidates(
    event_log: EventLog, overrides_store: ClassifyOverridesStore, *, scan_limit: int
) -> list[ClassifyCandidate]:
    overlay_rows: dict[tuple[str, str], Any] = {
        (row.kind, row.key): row for row in overrides_store.list()
    }
    latest = _latest_candidate_events(event_log, scan_limit=scan_limit)
    dismissed = _latest_dismissals(event_log, scan_limit=scan_limit)
    rows: list[ClassifyCandidate] = []
    for event, kind in latest.values():
        key, _target = _candidate_key_target(kind, event.payload or {})
        status = _candidate_status(
            kind=kind,
            key=key,
            overlay_rows=overlay_rows,
            event=event,
            dismissed_at=dismissed.get(
                str((event.payload or {}).get("candidate_id") or "")
            ),
        )
        rows.append(_to_candidate_row(event, kind, status))
    rows.sort(key=lambda r: r.emitted_at, reverse=True)
    return rows


@router.get("/classify/candidates")
def list_classify_candidates(
    limit: int = Query(
        _DEFAULT_EVENT_SCAN_LIMIT,
        ge=1,
        le=5000,
        description="Max candidate events to scan per kind. Never triggers mining.",
    ),
) -> ClassifyCandidateListResponse:
    """Recent tag/domain candidates, deduped to each one's latest emission.

    Never invokes a miner — see ``POST /classify/candidates/mine`` for
    that. A candidate already present in the overlay reads ``applied``;
    one with a more recent ``CLASSIFY_CANDIDATE_DISMISSED`` than its
    latest surfacing reads ``dismissed``; otherwise ``open``.
    """
    registry = get_registry()
    overrides_store = _get_overrides_store(registry)
    rows = _list_candidates(
        registry.operational.event_log, overrides_store, scan_limit=limit
    )
    degradation = overrides_store.degradation
    return ClassifyCandidateListResponse(
        count=len(rows),
        candidates=rows,
        store_degradation=degradation.to_dict() if degradation is not None else None,
    )


# ---------------------------------------------------------------------------
# Mining — admin-only, explicit about defaulted thresholds
# ---------------------------------------------------------------------------


class _SeededParameterStore(ParameterStore):
    """One-shot, in-process :class:`ParameterStore` for an opted-in default run.

    Mirrors the shape of ``trellis_cli.analyze._InMemoryParameterStore``
    and ``trellis_cli.classify``'s own ``_tag_evolution_registry`` /
    ``_domain_normalization_registry`` fallback (never imported — the
    ``trellis_api`` package does not depend on ``trellis_cli``). Exists
    only for the lifetime of one mine request: nothing here is
    persisted, so the next call sees the real registry's state again,
    missing keys and all, unless it also opts in.
    """

    def __init__(self, seed: ParameterSet) -> None:
        # Seeded at construction rather than through ``put``: this store is
        # an in-process stub, not a write to any plane, and building it
        # whole keeps it out of the governed-write rule's call-site scan.
        self._snapshots: dict[
            tuple[str, str | None, str | None, str | None], ParameterSet
        ] = {seed.scope.key(): seed}

    def put(self, params: ParameterSet) -> ParameterSet:
        self._snapshots[params.scope.key()] = params
        return params

    def get(self, params_version: str) -> ParameterSet | None:
        for snapshot in self._snapshots.values():
            if snapshot.params_version == params_version:
                return snapshot
        return None

    def get_active(self, scope: ParameterScope) -> ParameterSet | None:
        return self._snapshots.get(scope.key())

    def resolve(self, scope: ParameterScope) -> ParameterSet | None:
        return self.get_active(scope)

    def list_versions(
        self, scope: ParameterScope | None = None, *, limit: int = 100
    ) -> list[ParameterSet]:
        if scope is None:
            return list(self._snapshots.values())[:limit]
        snapshot = self.get_active(scope)
        return [snapshot] if snapshot is not None else []

    def close(self) -> None:
        self._snapshots.clear()


def _resolve_component_registry(
    registry: StoreRegistry,
    scope: ParameterScope,
    *,
    required_keys: tuple[str, ...],
    recommended_seed: dict[str, Any],
    use_recommended_defaults: bool,
    used_defaults: list[str],
) -> ParameterRegistry:
    """Resolve one component's thresholds, loudly defaulting only when asked.

    ``scope`` is constructed by the caller, inline, from the real component
    id -- never assembled from a parameter inside this function. That is
    not a style preference: a ``ParameterScope`` built from a local
    ``component_id: str`` parameter folds to no literal for
    ``tests/unit/test_parameter_reachability_rule.py``'s AST scan, which
    then has nothing to check except "some module constructs a scope",
    forcing a roster entry under a made-up component id. Built at the call
    site from the imported ``PARAM_COMPONENT_ID`` constants instead, the
    scan resolves this read site to the *real* ``learning.tag_evolution`` /
    ``learning.domain_normalization`` components that already have read
    points declared for their own direct reads in those modules -- one
    true roster entry per component, not a second fictitious one per
    caller.

    The persistent ``ParameterRegistry`` (``registry.operational.parameter_store``)
    wins when it carries every key in ``required_keys``. When it does not
    and ``use_recommended_defaults`` is ``False`` (the request's own
    default is ``True`` — see ``ClassifyMineRequest``'s docstring), this
    raises a 400 naming exactly what is missing rather than mining under
    thresholds nobody chose. When it is ``True``, this seeds a
    :class:`_SeededParameterStore` from ``recommended_seed``, appends
    ``component_id`` to ``used_defaults`` and logs a ``structlog`` WARN —
    never a silent fallback.
    """
    component_id = scope.component_id
    persistent = ParameterRegistry(registry.operational.parameter_store)
    values = persistent.get_values(scope)
    missing = sorted(k for k in required_keys if k not in values)
    if not missing:
        return persistent

    if not use_recommended_defaults:
        seed_hint = ", ".join(f"{k}={recommended_seed[k]!r}" for k in missing)
        raise HTTPException(
            status_code=400,
            detail=(
                f"ParameterRegistry is missing required thresholds for "
                f"{component_id!r}: {missing!r}. Seed defaults are: "
                f"{seed_hint}. Retry with use_recommended_defaults=true to "
                "mine against them, or seed the real thresholds first."
            ),
        )

    logger.warning(
        "classify.mine.seeded_recommended_defaults",
        component=component_id,
        missing=missing,
        defaults=dict(recommended_seed),
    )
    used_defaults.append(component_id)
    store = _SeededParameterStore(
        ParameterSet(
            scope=scope,
            values=dict(recommended_seed),
            source="api:classify:mine",
            notes="seeded by classify_rules._resolve_component_registry",
        )
    )
    return ParameterRegistry(store=store)


@router.post(
    "/classify/candidates/mine",
    dependencies=[Depends(require_scope(SCOPE_ADMIN))],
)
def mine_classify_candidates(req: ClassifyMineRequest) -> ClassifyMineResponse:
    """Run the tag-keyword / domain-alias miners and emit candidate events.

    Reuses :func:`trellis.learning.tag_evolution.analyze_tag_keyword_candidates`
    and :func:`trellis.learning.domain_normalization.analyze_domain_alias_candidates`
    directly — the same functions ``trellis classify tag-candidates`` /
    ``domain-candidates`` call — rather than re-implementing the mining
    logic here. Admin-scoped: unlike the read-only listing, this touches
    the corpus scan and, on a missing threshold with
    ``use_recommended_defaults=true``, mines under un-reviewed defaults.
    """
    kinds = set(req.kinds)
    unknown = kinds - _VALID_KINDS
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"Unknown candidate kind(s): {sorted(unknown)!r}"
        )
    if not kinds:
        raise HTTPException(
            status_code=400,
            detail="'kinds' must name at least one of: tag_keyword, domain_alias",
        )

    registry = get_registry()
    used_defaults: list[str] = []
    surfaced_ids: set[str] = set()
    tag_keyword_count = 0
    domain_alias_count = 0

    if "tag_keyword" in kinds:
        tag_registry = _resolve_component_registry(
            registry,
            ParameterScope(component_id=_TAG_PARAM_COMPONENT_ID),
            required_keys=_TAG_REQUIRED_PARAM_KEYS,
            recommended_seed=_TAG_RECOMMENDED_SEED_VALUES,
            use_recommended_defaults=req.use_recommended_defaults,
            used_defaults=used_defaults,
        )
        try:
            # Annotated ``Any``: this branch's list element type is
            # ``TagKeywordCandidate``, the ``domain_alias`` branch below
            # reuses the same name for its own ``DomainAliasCandidate``
            # rows, and both are read only through the attributes they
            # share (``candidate_id``) -- a narrower annotation here would
            # make mypy reject the second branch's assignment.
            surfaced: list[Any] = analyze_tag_keyword_candidates(
                document_store=registry.knowledge.document_store,
                event_log=registry.operational.event_log,
                registry=tag_registry,
                known_keywords=[
                    kw for kws in registry.domain_keyword_map().values() for kw in kws
                ],
                domain_aliases=registry.domain_alias_map(),
                emit_events=True,
                scan_limit=req.scan_limit
                if req.scan_limit is not None
                else _TAG_DEFAULT_SCAN_LIMIT,
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc).strip("'\"")) from exc
        tag_keyword_count = len(surfaced)
        surfaced_ids.update(c.candidate_id for c in surfaced)

    if "domain_alias" in kinds:
        domain_registry = _resolve_component_registry(
            registry,
            ParameterScope(component_id=_DOMAIN_PARAM_COMPONENT_ID),
            required_keys=_DOMAIN_REQUIRED_PARAM_KEYS,
            recommended_seed=_DOMAIN_RECOMMENDED_SEED_VALUES,
            use_recommended_defaults=req.use_recommended_defaults,
            used_defaults=used_defaults,
        )
        try:
            surfaced = analyze_domain_alias_candidates(
                document_store=registry.knowledge.document_store,
                event_log=registry.operational.event_log,
                registry=domain_registry,
                known_aliases=registry.domain_alias_map(),
                aspect_tags=registry.domain_aspect_tags(),
                emit_events=True,
                scan_limit=(
                    req.scan_limit
                    if req.scan_limit is not None
                    else _DOMAIN_DEFAULT_SCAN_LIMIT
                ),
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc).strip("'\"")) from exc
        domain_alias_count = len(surfaced)
        surfaced_ids.update(c.candidate_id for c in surfaced)

    overrides_store = _get_overrides_store(registry)
    scan_limit = max(_DEFAULT_EVENT_SCAN_LIMIT, len(surfaced_ids) * 2)
    all_rows = _list_candidates(
        registry.operational.event_log, overrides_store, scan_limit=scan_limit
    )
    rows = [row for row in all_rows if row.candidate_id in surfaced_ids]

    return ClassifyMineResponse(
        status="ok",
        tag_keyword_count=tag_keyword_count,
        domain_alias_count=domain_alias_count,
        used_recommended_defaults=used_defaults,
        candidates=rows,
    )


# ---------------------------------------------------------------------------
# Apply / revoke — governed, through MutationExecutor
# ---------------------------------------------------------------------------


@router.post(
    "/classify/rules",
    dependencies=[Depends(require_scope(SCOPE_ADMIN))],
)
def apply_classify_rule(
    req: ClassifyRuleApplyRequest,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> Any:
    """Apply one mined candidate into the operator overlay.

    Routes through ``Operation.CLASSIFY_RULE_APPLY`` — the same governed
    path ``trellis classify apply-rule`` uses — so a denying policy blocks
    both surfaces identically (Stage 2 always runs, CLAUDE.md). The
    candidate's own metrics (``support``/``precision``/``lift``) are
    carried through onto the audit event so a reviewer never has to join
    back to the original ``TAG_KEYWORD_CANDIDATE`` / ``DOMAIN_ALIAS_CANDIDATE``
    event to see what was approved.
    """
    principal = ctx.key_id or ctx.name or "unknown"
    row_id = f"{req.kind}:{req.key}"
    cmd = Command(
        operation=Operation.CLASSIFY_RULE_APPLY,
        args={
            "kind": req.kind,
            "key": req.key,
            "target": req.target,
            "source_event_id": req.source_event_id,
            "support": req.support,
            "precision": req.precision,
            "lift": req.lift,
        },
        target_id=row_id,
        target_type="classify_rule",
        requested_by=f"api:classify:{principal}",
    )
    return _execute_classify_command(cmd)


@router.delete(
    "/classify/rules/{kind}/{key}",
    dependencies=[Depends(require_scope(SCOPE_ADMIN))],
)
def revoke_classify_rule(
    kind: str,
    key: str,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> Any:
    """Revoke one applied rule from the operator overlay.

    Refused (400) when ``(kind, key)`` has no overlay row — including
    when the rule comes from ``config.yaml`` instead, which this was
    never written by and cannot un-apply; see
    ``ClassifyRuleRevokeHandler``'s own docstring for the two distinct
    refusal messages.
    """
    principal = ctx.key_id or ctx.name or "unknown"
    row_id = f"{kind}:{key}"
    cmd = Command(
        operation=Operation.CLASSIFY_RULE_REVOKE,
        args={"kind": kind, "key": key},
        target_id=row_id,
        target_type="classify_rule",
        requested_by=f"api:classify:{principal}",
    )
    return _execute_classify_command(cmd)


# ---------------------------------------------------------------------------
# Dismiss — plain audit telemetry, not a governed mutation
# ---------------------------------------------------------------------------


def _find_candidate(event_log: EventLog, candidate_id: str) -> tuple[str, Event] | None:
    """Find a candidate's kind and latest surfacing event by id.

    ``entity_id`` alone (no ``event_type``) is a supported
    :meth:`EventLog.get_events` filter, so this reads across both
    ``TAG_KEYWORD_CANDIDATE`` and ``DOMAIN_ALIAS_CANDIDATE`` streams in one
    call rather than probing each kind in turn — candidate events stamp
    ``entity_id=candidate.candidate_id``.
    """
    for event in event_log.get_events(entity_id=candidate_id, limit=10, order="desc"):
        if event.event_type == EventType.TAG_KEYWORD_CANDIDATE:
            return "tag_keyword", event
        if event.event_type == EventType.DOMAIN_ALIAS_CANDIDATE:
            return "domain_alias", event
    return None


@router.post(
    "/classify/candidates/{candidate_id}/dismiss",
    dependencies=[Depends(require_scope(SCOPE_ADMIN))],
)
def dismiss_classify_candidate(
    candidate_id: str,
    ctx: AuthContext = Depends(authenticate),  # noqa: B008 — FastAPI DI idiom
) -> ClassifyCandidateDismissResponse:
    """Mark a candidate dismissed — no store write, audit trail only.

    ``CLASSIFY_CANDIDATE_DISMISSED`` carries no dedicated ``Operation``:
    a dismiss changes no state ``MutationExecutor`` governs, only this
    event stream (see the ``EventType`` member's own docstring), the same
    posture :attr:`~trellis.stores.base.event_log.EventType.JOB_RUN_STARTED`
    has. ``GET /classify/candidates`` excludes a dismissed candidate from
    its "open" listing unless it resurfaces with a newer emission.
    """
    registry = get_registry()
    found = _find_candidate(registry.operational.event_log, candidate_id)
    if found is None:
        raise HTTPException(
            status_code=404, detail=f"No such classify candidate: {candidate_id!r}"
        )
    kind, _event = found
    principal = ctx.key_id or ctx.name or "unknown"
    registry.operational.event_log.emit(
        EventType.CLASSIFY_CANDIDATE_DISMISSED,
        source="api:classify:dismiss",
        entity_id=candidate_id,
        entity_type=kind,
        payload={
            "candidate_id": candidate_id,
            "kind": kind,
            "dismissed_by": f"api:classify:{principal}",
        },
    )
    return ClassifyCandidateDismissResponse(
        status="ok",
        candidate_id=candidate_id,
        kind=kind,
        message=f"Dismissed {kind} candidate {candidate_id!r}",
    )


# ---------------------------------------------------------------------------
# Effective rules — default / config.yaml / overlay, with provenance
# ---------------------------------------------------------------------------


@router.get("/classify/rules")
def list_classify_rules() -> ClassifyRuleListResponse:
    """Every effective tag-keyword / domain-alias rule, badged by source.

    ``source`` is ``"overlay"`` when the exact ``(kind, key) -> target``
    triple is an applied row in ``classify_overrides.json``, ``"config"``
    when it instead comes from ``config.yaml``, and ``"default"`` for the
    shipped built-ins otherwise (no built-in alias defaults exist today,
    so that branch is defensive for aliases). The badging itself is
    :func:`~trellis.classify.factory.effective_classify_rule_rows`,
    shared with ``trellis classify effective-rules`` so the precedence
    logic lives in one place.
    ``registry.domain_keyword_map()`` / ``domain_alias_map()`` already
    resolve the full precedence (defaults < config.yaml < overlay,
    overlay merged additively per domain) — this route only re-derives
    *which* layer won, never a second merge.
    """
    registry = get_registry()
    overrides_store = _get_overrides_store(registry)
    overlay_rows: dict[tuple[str, str], Any] = {
        (row.kind, row.key): row for row in overrides_store.list()
    }

    try:
        domain_keyword_map = registry.domain_keyword_map()
        domain_alias_map = registry.domain_alias_map()
    except (KeyError, ValueError) as exc:
        # registry.domain_alias_map() raises a bare ValueError when
        # config.yaml and/or the overlay merge into a self-map or chain
        # (effective_domain_aliases) -- Revoke now refuses the write that
        # would create one, but a config.yaml edit alone can still produce
        # this with no overlay write to have caught it. A 500 here would
        # be the only sign anything was wrong; 400 with the composed
        # message names exactly which rows to fix, same convention as
        # the mine route above.
        raise HTTPException(status_code=400, detail=str(exc).strip("'\"")) from exc

    rows = effective_classify_rule_rows(
        classify_config=registry.classify_config,
        domain_keyword_map=domain_keyword_map,
        domain_alias_map=domain_alias_map,
        overlay_rows=overlay_rows,
    )
    degradation = overrides_store.degradation
    return ClassifyRuleListResponse(
        count=len(rows),
        rules=[
            ClassifyRuleRow(
                kind=row["kind"],
                key=row["key"],
                target=row["target"],
                source=row["source"],
                applied_at=row["applied_at"].isoformat()
                if row["applied_at"] is not None
                else None,
                applied_by=row["applied_by"],
                source_event_id=row["source_event_id"],
            )
            for row in rows
        ],
        store_degradation=degradation.to_dict() if degradation is not None else None,
    )
