"""Feedback recording — JSONL append-log for pack feedback signals."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from trellis.feedback.attribution import (
    EMPTY_PACK_SCOPE,
    PackScope,
    lookup_pack_items_by_strategy,
    lookup_pack_scope,
)
from trellis.feedback.models import PackFeedback
from trellis.schemas.outcome import (
    COMPONENT_ID_BY_SOURCE_STRATEGY,
    PACK_BUILDER_COMPONENT_ID,
)
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog
    from trellis.stores.base.outcome import OutcomeStore

logger = structlog.get_logger(__name__)

#: Default component id used when bridging PackFeedback into an OutcomeEvent.
#: Callers can override per-call via the ``component_id`` kwarg on
#: :func:`record_feedback`.  It names the *pack builder* because a
#: ``PackFeedback`` grades a pack; the per-strategy fan-out below stamps the
#: strategies' own ids alongside it.
_DEFAULT_COMPONENT_ID = PACK_BUILDER_COMPONENT_ID

#: Default ``source`` stamped on the emitted ``FEEDBACK_RECORDED`` event.
#: Overridable per-call so agent-facing surfaces keep their provenance.
_DEFAULT_EVENT_SOURCE = "feedback.record"

#: Sub-directory of ``StoreRegistry.stores_dir`` holding the audit log.
_FEEDBACK_LOG_SUBDIR = "feedback"


def feedback_log_dir(stores_dir: Path | str) -> Path:
    """Directory holding ``pack_feedback.jsonl`` for a given stores dir.

    One spelling of the location, so the MCP tool, the REST route and
    ``worker curate --reconcile-first`` cannot look in different places.
    Always derive ``stores_dir`` from ``StoreRegistry.stores_dir`` — it
    honours ``data_dir:`` in ``config.yaml``, which re-deriving from the
    environment does not.
    """
    return Path(stores_dir) / _FEEDBACK_LOG_SUBDIR


@dataclass(frozen=True)
class FeedbackRecordResult:
    """Outcome of a :func:`record_feedback` call.

    The JSONL append is always attempted and is the durability
    guarantee; ``log_path`` is populated even when downstream emissions
    fail. ``event_log_emitted`` / ``outcome_emitted`` tell callers
    whether the bridged sinks actually received the signal, so a
    retry or reconciliation can be scheduled without scanning logs.

    The error fields surface the last exception caught so callers can
    distinguish "sink not configured" (``*_error is None``) from
    "sink failed" (``*_error is not None``). Emissions still fail
    soft; callers opt into strict mode by checking these fields.
    """

    log_path: Path
    feedback_id: str
    event_log_emitted: bool = False
    outcome_emitted: bool = False
    event_log_error: Exception | None = None
    outcome_error: Exception | None = None
    event_log_skipped_as_duplicate: bool = False
    strategy_outcomes_emitted: int = 0
    """How many per-strategy ``OutcomeEvent`` rows the fan-out wrote.

    ``0`` on a pack-targeted call means the fan-out found nothing to
    attribute — no ``event_log`` to look the pack up in, a pack with no
    ``injected_items``, or no served strategy this build can map. It is
    deliberately a *count* and not a bool: the pack-level row is emitted
    either way, so there is no single yes/no this could answer.
    """

    @property
    def event_log_in_sync(self) -> bool:
        """True when the EventLog has a matching feedback entry.

        Either we emitted successfully this call, or a prior call /
        reconciliation already persisted it (duplicate-skip path).
        """
        return self.event_log_emitted or self.event_log_skipped_as_duplicate


@dataclass
class ReconcileResult:
    """Outcome of :func:`reconcile_feedback_log_to_event_log`."""

    scanned: int = 0
    already_present: int = 0
    emitted: int = 0
    failed: int = 0
    missing_feedback_ids: list[str] = field(default_factory=list)


def _feedback_id_in_event_log(event_log: EventLog, feedback_id: str) -> bool:
    """Return True when the EventLog already has a FEEDBACK_RECORDED event
    with this ``feedback_id`` in its payload.

    Pushes the ``feedback_id`` predicate into the backend via
    ``payload_filters`` so the lookup is a SQL ``WHERE`` against
    ``payload->>'feedback_id'`` (Postgres) / ``json_extract`` (SQLite),
    not a Python scan over the most-recent 10K events. ``limit=1`` is
    enough — the predicate identifies the row, ``order="desc"`` is
    retained for backends that don't honour limit-with-predicate
    semantics deterministically.
    """
    events = event_log.get_events(
        event_type=EventType.FEEDBACK_RECORDED,
        limit=1,
        order="desc",
        payload_filters={"feedback_id": feedback_id},
    )
    return bool(events)


def record_feedback(
    feedback: PackFeedback,
    *,
    log_dir: Path | str,
    event_log: EventLog | None = None,
    outcome_store: OutcomeStore | None = None,
    pack_id: str | None = None,
    component_id: str = _DEFAULT_COMPONENT_ID,
    source: str = _DEFAULT_EVENT_SOURCE,
    entity_id: str | None = None,
    entity_type: str | None = None,
) -> FeedbackRecordResult:
    """Append a feedback signal to the JSONL log.

    Creates the log directory and file if they don't exist.  When
    ``event_log`` is provided, also emits a ``FEEDBACK_RECORDED`` event
    so :class:`~trellis.retrieve.advisory_generator.AdvisoryGenerator`
    and :func:`~trellis.retrieve.effectiveness.analyze_effectiveness`
    pick up the signal.  When ``outcome_store`` is also provided, an
    :class:`~trellis.schemas.outcome.OutcomeEvent` is appended to the
    ops-tier store so tuners can consume it.

    The JSONL append is the authoritative file record; event and
    outcome emission bridge the feedback into the governed analytics
    and ops paths respectively.  Both emissions fail soft — log-only,
    never raise — since the file write is the durability guarantee.

    ``feedback.feedback_id`` is used as the idempotency key against the
    EventLog: if a prior call or replay already emitted this feedback_id,
    the event emission is skipped and the result reports
    ``event_log_skipped_as_duplicate=True``. The JSONL append is still
    performed (it is the authoritative file record). Callers who want
    the JSONL file itself to be de-duplicated should check by
    ``feedback_id`` before calling.

    Args:
        feedback: The feedback signal to record.
        log_dir: Directory for the feedback log
            (e.g. ``artifacts/runs/{run_id}/experience/``).
        event_log: Optional event log to also emit ``FEEDBACK_RECORDED``
            to.  When ``None`` (default), behavior is file-only —
            matching consumer workflows that consume the JSONL log
            directly.
        outcome_store: Optional :class:`OutcomeStore` to dual-emit an
            ``OutcomeEvent`` bridging PackFeedback into the ops tier.
        pack_id: Pack identifier for the event.  Used as both the
            event's ``entity_id`` and ``payload.pack_id`` so
            AdvisoryGenerator can join with ``PACK_ASSEMBLED`` events,
            and also stored on the OutcomeEvent's ``pack_id`` field.
            Ignored when neither emission sink is provided.
        component_id: Stable component identifier written onto the
            :class:`OutcomeEvent`.  Defaults to the PackBuilder.
        source: ``source`` recorded on the emitted event.  Defaults to
            ``"feedback.record"``; agent-facing surfaces pass their own
            (e.g. ``"mcp"``) so provenance stays visible in the log.
        entity_id: Event ``entity_id`` override.  Defaults to ``pack_id``.
            Trace-level feedback — a real case on the MCP surface, where
            an agent grades a trace with no pack involved — passes the
            ``trace_id`` here so the event is still reachable by entity.
        entity_type: Event ``entity_type`` override.  Defaults to
            ``"pack"`` when a ``pack_id`` is given.  Pass alongside
            ``entity_id`` (e.g. ``"trace"``).

    Returns:
        :class:`FeedbackRecordResult` carrying the log path,
        ``feedback_id``, per-sink emission status, and any captured
        errors.
    """
    log_path = Path(log_dir) / "pack_feedback.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    entry = asdict(feedback)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")

    event_log_emitted = False
    event_log_skipped_as_duplicate = False
    event_log_error: Exception | None = None
    if event_log is not None:
        try:
            if _feedback_id_in_event_log(event_log, feedback.feedback_id):
                event_log_skipped_as_duplicate = True
                logger.debug(
                    "feedback_event_skipped_duplicate",
                    feedback_id=feedback.feedback_id,
                    run_id=feedback.run_id,
                    pack_id=pack_id,
                )
            else:
                event_log.emit(
                    EventType.FEEDBACK_RECORDED,
                    source=source,
                    entity_id=entity_id if entity_id is not None else pack_id,
                    entity_type=(
                        entity_type
                        if entity_id is not None
                        else ("pack" if pack_id else None)
                    ),
                    payload=feedback.to_event_payload(pack_id=pack_id),
                )
                event_log_emitted = True
        # GRACEFUL-DEGRADATION: JSONL append is durable; EventLog
        # bridge is best-effort. Failure surfaces to caller via
        # FeedbackRecordResult.event_log_error so a retry/reconcile can run.
        # TODO(c2-phase5): add metrics.telemetry_failures counter (structlog-only).
        except Exception as exc:
            event_log_error = exc
            logger.exception(
                "feedback_event_emit_failed",
                run_id=feedback.run_id,
                pack_id=pack_id,
                feedback_id=feedback.feedback_id,
            )

    outcome_emitted = False
    outcome_error: Exception | None = None
    strategy_outcomes_emitted = 0
    if outcome_store is not None:
        try:
            # Resolved once, for both emitters: they must agree about which
            # cell a pack's outcomes belong to, and the axes come from the
            # pack rather than the feedback (#560 — see ``_resolve_scope``).
            scope = _resolve_scope(feedback, pack_id=pack_id, event_log=event_log)
            _emit_outcome(
                feedback,
                outcome_store=outcome_store,
                pack_id=pack_id,
                component_id=component_id,
                scope=scope,
            )
            outcome_emitted = True
            # Emitted *after* the pack-level row and reported separately:
            # the fan-out is an additional signal, and a partial fan-out
            # must not make ``outcome_emitted`` read False for a row that
            # is already in the store.
            strategy_outcomes_emitted = _emit_strategy_outcomes(
                feedback,
                outcome_store=outcome_store,
                pack_id=pack_id,
                event_log=event_log,
                scope=scope,
            )
        # GRACEFUL-DEGRADATION: JSONL append is durable; OutcomeStore
        # bridge is best-effort. Failure surfaces via
        # FeedbackRecordResult.outcome_error.
        # TODO(c2-phase5): add metrics.telemetry_failures counter (structlog-only).
        except Exception as exc:
            outcome_error = exc
            logger.exception(
                "feedback_outcome_emit_failed",
                run_id=feedback.run_id,
                pack_id=pack_id,
                feedback_id=feedback.feedback_id,
            )

    logger.debug(
        "feedback_recorded",
        feedback_id=feedback.feedback_id,
        run_id=feedback.run_id,
        phase=feedback.phase,
        outcome=feedback.outcome,
        items_served=len(feedback.items_served),
        log_path=str(log_path),
        event_log_emitted=event_log_emitted,
        event_log_skipped_as_duplicate=event_log_skipped_as_duplicate,
        outcome_emitted=outcome_emitted,
        strategy_outcomes_emitted=strategy_outcomes_emitted,
    )
    return FeedbackRecordResult(
        log_path=log_path,
        feedback_id=feedback.feedback_id,
        event_log_emitted=event_log_emitted,
        outcome_emitted=outcome_emitted,
        event_log_error=event_log_error,
        outcome_error=outcome_error,
        event_log_skipped_as_duplicate=event_log_skipped_as_duplicate,
        strategy_outcomes_emitted=strategy_outcomes_emitted,
    )


def reconcile_feedback_log_to_event_log(
    log_dir: Path | str,
    event_log: EventLog,
    *,
    pack_id_lookup: dict[str, str] | None = None,
) -> ReconcileResult:
    """Emit any JSONL feedback entries missing from the EventLog.

    Closes the divergence path where JSONL was written but the
    ``FEEDBACK_RECORDED`` event was not (sink unavailable, process
    crashed between writes, file-only capture being promoted into the
    governed pipeline, etc.).

    Safe to run repeatedly: each JSONL row is matched against the
    EventLog by ``feedback_id``; entries that are already present are
    left alone.

    Args:
        log_dir: Directory containing ``pack_feedback.jsonl``.
        event_log: EventLog to backfill.
        pack_id_lookup: Optional ``feedback_id -> pack_id`` map for
            entries that carry a pack association. ``pack_id`` is not a
            ``PackFeedback`` field, so it is recovered from this mapping
            first and from ``metadata["pack_id"]`` second — the writers
            that know the pack stamp it there precisely so an emit that
            failed at record time can be replayed with its pack
            association intact. Trace-level feedback (no pack) falls
            back to ``metadata["trace_id"]`` and is re-emitted with
            ``entity_type="trace"``, matching what the original emit
            would have written. Without any of the three, the emitted
            event has ``entity_id=None`` and no ``payload.pack_id``,
            which the advisory/effectiveness joins cannot use.

    Returns:
        :class:`ReconcileResult` with counts and the list of
        ``feedback_id``s that failed to emit.
    """
    signals = load_feedback_log(log_dir)
    result = ReconcileResult(scanned=len(signals))
    lookup = pack_id_lookup or {}

    for fb in signals:
        if _feedback_id_in_event_log(event_log, fb.feedback_id):
            result.already_present += 1
            continue
        recovered = lookup.get(fb.feedback_id) or fb.metadata.get("pack_id")
        pack_id = str(recovered) if recovered else None
        entity_id: str | None = pack_id
        entity_type: str | None = "pack" if pack_id else None
        if entity_id is None:
            trace_id = fb.metadata.get("trace_id")
            if trace_id:
                entity_id, entity_type = str(trace_id), "trace"
        try:
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="feedback.reconcile",
                entity_id=entity_id,
                entity_type=entity_type,
                payload=fb.to_event_payload(pack_id=pack_id),
            )
            result.emitted += 1
        # GRACEFUL-DEGRADATION: reconciliation loop must drain the
        # JSONL log; per-row failures are recorded on ReconcileResult so the
        # caller can retry the missing ids.
        # TODO(c2-phase5): add metrics.telemetry_failures counter (structlog-only).
        except Exception:
            result.failed += 1
            result.missing_feedback_ids.append(fb.feedback_id)
            logger.exception(
                "feedback_reconcile_emit_failed",
                feedback_id=fb.feedback_id,
                run_id=fb.run_id,
            )

    logger.info(
        "feedback_reconcile_completed",
        log_dir=str(log_dir),
        scanned=result.scanned,
        already_present=result.already_present,
        emitted=result.emitted,
        failed=result.failed,
    )
    return result


def _parse_timestamp(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning ``None`` on failure.

    Empty input is a first-class "unknown timestamp" signal and returns
    ``None`` silently. A *malformed* non-empty input is data corruption
    and must surface — the bridge into the OutcomeStore loses
    ``occurred_at`` otherwise and the operator has no signal that a row
    arrived bad. We log a warning and still return ``None`` because the
    bridge contract is fail-soft (the JSONL append is the audit trail),
    but the loud log closes the silent-fallback gap.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    # GRACEFUL-DEGRADATION: bridge contract is fail-soft (JSONL is the
    # audit trail); the loud warning closes the silent-fallback gap so
    # operators see corrupted rows. See function docstring.
    except (TypeError, ValueError):
        logger.warning(
            "feedback_timestamp_parse_failed",
            raw=raw,
            exc_info=True,
        )
        return None


def _resolve_scope(
    feedback: PackFeedback,
    *,
    pack_id: str | None,
    event_log: EventLog | None,
) -> PackScope:
    """Resolve the ``(domain, intent_family)`` an outcome row is keyed on.

    **Feedback first, pack as the fallback** — the same precedence
    :func:`~trellis.learning.pack_observations.build_learning_observations_from_event_log`
    applies, deliberately, because two joins of the same two events
    disagreeing about which cell a pack belongs to is worse than either
    ordering. The feedback is the closest witness to the run that consumed
    the pack; the pack is what knows the axes when the feedback does not.

    Today the fallback carries every row. ``PackFeedback`` has no ``domain``
    field at all, and
    :meth:`~trellis.feedback.models.PackFeedback.from_agent_signal` takes no
    ``intent_family``, so on both agent-facing surfaces the feedback side is
    empty by construction and the pack supplies both (#560). A caller that
    builds a ``PackFeedback`` directly can still set ``intent_family`` and
    have it win, which is why this is a precedence rule and not a plain read
    of the pack.

    Fails soft to :data:`~trellis.feedback.attribution.EMPTY_PACK_SCOPE`,
    which is the ``None``/``None`` the axes held before this existed — so a
    missing event log or an unknown pack degrades to the old behaviour rather
    than to an error or a guessed cell.

    This reads the same ``PACK_ASSEMBLED`` event
    :func:`~trellis.feedback.attribution.lookup_pack_items_by_strategy` reads
    a moment later, so a pack-targeted call costs two ``limit=1`` lookups on
    one indexed key. Left unfused deliberately: both lookups answer one
    question from a pack id, which is the shape every reader in
    :mod:`trellis.feedback.attribution` has, and threading a payload dict
    through the emitters to save an indexed read on a path that runs a few
    times a day buys nothing.
    """
    from_feedback = feedback.intent_family.strip() or None
    if event_log is None or not pack_id:
        return PackScope(domain=None, intent_family=from_feedback)

    from_pack = lookup_pack_scope(event_log, pack_id)
    return PackScope(
        domain=from_pack.domain,
        intent_family=from_feedback or from_pack.intent_family,
    )


def _emit_outcome(
    feedback: PackFeedback,
    *,
    outcome_store: OutcomeStore,
    pack_id: str | None,
    component_id: str,
    scope: PackScope = EMPTY_PACK_SCOPE,
) -> None:
    """Bridge a :class:`PackFeedback` into an :class:`OutcomeEvent`.

    ``scope`` carries the learning axes the row is keyed on; it defaults to
    the unscoped cell so a caller that has no pack to resolve them from gets
    exactly the behaviour this bridge had before #560.
    """
    # Import deferred to avoid importing ops in the hot path for callers
    # that never pass an outcome_store.  (``trellis.schemas`` is already
    # resident by way of ``trellis.feedback.models``, so it costs nothing
    # to name a schema constant at module scope.)
    from trellis.ops import record_outcome  # noqa: PLC0415

    occurred_at = _parse_timestamp(feedback.timestamp_utc)
    # ``from_agent_signal`` leaves ``items_served`` empty on purpose: an
    # agent cites what helped, it does not enumerate what it was shown.
    # Passing ``len([]) == 0`` would clear ``OutcomeEvent``'s
    # ``is not None`` guard and report a *measured* denominator of zero,
    # which the aggregator cannot tell from a real one. ``None`` is the
    # honest value — "this surface did not report a serving count".
    items_served = len(feedback.items_served) or None
    items_referenced = len(feedback.items_referenced)
    success = feedback.succeeded

    metadata: dict[str, object] = {
        "pack_outcome": feedback.outcome,
        "intent": feedback.intent,
        "feedback_id": feedback.feedback_id,
    }
    if feedback.relevance_scores:
        metadata["relevance_scores"] = dict(feedback.relevance_scores)
    if feedback.metadata:
        metadata["feedback_metadata"] = dict(feedback.metadata)

    record_outcome(
        outcome_store,
        component_id=component_id,
        success=success,
        latency_ms=0.0,
        domain=scope.domain,
        intent_family=scope.intent_family,
        phase=feedback.phase or None,
        agent_id=feedback.agent_id,
        run_id=feedback.run_id,
        pack_id=pack_id,
        items_served=items_served,
        items_referenced=items_referenced,
        occurred_at=occurred_at,
        metadata=metadata,
    )


def _emit_strategy_outcomes(
    feedback: PackFeedback,
    *,
    outcome_store: OutcomeStore,
    pack_id: str | None,
    event_log: EventLog | None,
    scope: PackScope = EMPTY_PACK_SCOPE,
) -> int:
    """Fan one pack grade out into one :class:`OutcomeEvent` per strategy.

    **Why this exists.** Every rule in
    :data:`~trellis.learning.tuners.rule_tuner.DEFAULT_RULES` targets a
    *strategy* (``retrieve.strategies.KeywordSearch`` and friends), because
    that is the scope ``SearchStrategy._resolve_param`` reads a tuned
    parameter back under. The bridge above
    stamps the *pack builder*. So until now the two halves of the learning
    loop addressed different components and could never meet (#557 D2) — the
    tuner saw only ``retrieve.pack_builder.PackBuilder`` cells and every
    shipped rule waited on an id nothing emitted.

    **What each side supplies.** The agent's ``PackFeedback`` is the
    numerator: the ids it found helpful. It cannot supply the denominator —
    ``from_agent_signal`` leaves ``items_served`` empty on purpose, because
    an agent cites what helped and does not enumerate what it was shown.
    The *pack* knows what it was shown and, per item, which strategy showed
    it. Joining them gives each strategy a real
    ``items_referenced / items_served``, which is what
    ``reference_rate`` means.

    **``success`` is the pack's bit, repeated — not a per-strategy
    measurement.** :class:`~trellis.schemas.outcome.ComponentOutcome`
    requires a ``success: bool`` and the only value in hand is the one grade
    the agent gave the whole pack; a typical pack contains every strategy,
    so crediting each contributor with it makes their success rates
    near-identical by construction (measured over 30 days on the reference
    deployment: 0.189 / 0.191 / 0.192 across semantic / keyword / graph).
    Read ``success_rate`` on these rows as a property of the *packs* a
    strategy appeared in, never as a comparison between strategies. The
    separating signal on these rows is ``reference_rate``.

    Fails soft as a whole and per row: the pack-level outcome is already in
    the store by the time this runs, and a lookup that comes back empty
    yields no rows rather than a guessed one. A ``strategy_source`` this
    build cannot map is **dropped**, never bucketed under some other
    component — an unattributable serving must not inflate a denominator
    that decides a parameter change.

    Idempotency is inherited, not added: the pack-level bridge re-emits on a
    replayed ``feedback_id`` (only the ``FEEDBACK_RECORDED`` emit is
    de-duplicated), and so does this. A replay inflates the strategy rows in
    the same proportion as the pack row.

    **Every row lands in the same learning cell as the pack-level row.**
    ``scope`` is resolved once by the caller and passed to both bridges, so a
    strategy's ``reference_rate`` and the pack ``reference_rate`` it was
    derived from are always comparable rather than sitting in cells that a
    tuner aggregates apart (#560).

    Returns:
        Number of per-strategy rows emitted. ``0`` whenever there is no
        event log, no pack, no ``injected_items``, or nothing mappable.
    """
    if event_log is None or not pack_id:
        return 0

    # Imports deferred for the same reason the pack-level bridge defers
    # them: ``trellis.ops`` must stay out of the import path of callers
    # that never pass an outcome_store.
    from trellis.ops import record_outcome  # noqa: PLC0415

    served_by_strategy = lookup_pack_items_by_strategy(event_log, pack_id)
    if not served_by_strategy:
        return 0

    cited = set(feedback.items_referenced)
    occurred_at = _parse_timestamp(feedback.timestamp_utc)
    emitted = 0

    for strategy_source, served_ids in served_by_strategy.items():
        component_id = COMPONENT_ID_BY_SOURCE_STRATEGY.get(strategy_source)
        if component_id is None:
            logger.debug(
                "feedback_strategy_outcome_unmapped",
                strategy_source=strategy_source,
                pack_id=pack_id,
                feedback_id=feedback.feedback_id,
            )
            continue
        record_outcome(
            outcome_store,
            component_id=component_id,
            # The pack's bit, credited to each contributor — see docstring.
            success=feedback.succeeded,
            latency_ms=0.0,
            # The same cell as the pack-level row, so a per-strategy rate and
            # the pack rate it was derived from are comparable.
            domain=scope.domain,
            intent_family=scope.intent_family,
            phase=feedback.phase or None,
            agent_id=feedback.agent_id,
            run_id=feedback.run_id,
            pack_id=pack_id,
            items_served=len(served_ids),
            items_referenced=sum(1 for item_id in served_ids if item_id in cited),
            occurred_at=occurred_at,
            metadata={
                "pack_outcome": feedback.outcome,
                "feedback_id": feedback.feedback_id,
                "strategy_source": strategy_source,
                # Marks the row as derived from a pack grade rather than
                # measured at the strategy itself, so a consumer can tell
                # the two apart if a strategy ever reports for itself.
                "fanned_out_from": PACK_BUILDER_COMPONENT_ID,
            },
        )
        emitted += 1

    return emitted


def load_feedback_log(log_dir: Path | str) -> list[PackFeedback]:
    """Load all feedback signals from a JSONL log.

    Args:
        log_dir: Directory containing pack_feedback.jsonl.

    Returns:
        List of PackFeedback objects in chronological order.
    """
    log_path = Path(log_dir) / "pack_feedback.jsonl"
    if not log_path.exists():
        return []

    signals: list[PackFeedback] = []
    with log_path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            stripped = raw_line.strip()
            if not stripped:
                continue
            data = json.loads(stripped)
            metadata = data.get("metadata") or {}
            kwargs: dict[str, object] = {
                "run_id": data["run_id"],
                "phase": data["phase"],
                "intent": data["intent"],
                "outcome": data["outcome"],
                "items_served": data.get("items_served", []),
                "items_referenced": data.get("items_referenced", []),
                "relevance_scores": data.get("relevance_scores", {}),
                # Absent on rows written before these fields existed;
                # the defaults match "ungraded, no attribution". The
                # ``metadata`` fallback recovers the grade from rows the
                # REST route wrote while ``rating`` was still a metadata
                # key — without it a replay would overwrite a real 0.3
                # with a derived 1.0 at the key consumers read.
                "rating": data.get("rating", metadata.get("rating")),
                "unhelpful_item_ids": data.get("unhelpful_item_ids", []),
                "followed_advisory_ids": data.get("followed_advisory_ids", []),
                "intent_family": data.get("intent_family", ""),
                "timestamp_utc": data.get("timestamp_utc", ""),
                "agent_id": data.get("agent_id"),
                "metadata": metadata,
            }
            # Older JSONL rows pre-date feedback_id; only pass through when
            # present so the dataclass default (fresh ULID) doesn't stomp
            # an existing id and break reconciliation idempotency.
            if data.get("feedback_id"):
                kwargs["feedback_id"] = data["feedback_id"]
            signals.append(PackFeedback(**kwargs))  # type: ignore[arg-type]

    logger.debug("feedback_log_loaded", count=len(signals), log_path=str(log_path))
    return signals
