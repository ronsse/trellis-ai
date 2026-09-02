"""Candidate resolution for the governed ``retention.prune`` verb.

Retention answers "this content stopped earning its storage" — hygiene,
batch, criteria-driven. That makes it the opposite shape from
``redaction.apply``, which answers "this content must cease to exist" for a
single named target. The consequences, from
``docs/design/adr-retention-prune.md`` §3.1:

* **Criteria-driven, not target-driven.** A command names a *predicate*
  (:class:`RetentionCriteria`); this module resolves it to concrete ids at
  execute time, so the candidate set is computed against the store as it is
  when the command runs rather than as it was when the operator looked.
* **Reversibility is a virtue.** Phase one is *archival*, not purge — the
  handler stamps ``Lifecycle.state="archived"`` and retrieval stops serving
  the item. A wrong prune is walked back by re-stamping, not restored from
  a backup.

**Where the ADR's candidate set was wrong, and why this differs.** §3.2
lists "graph entities tagged ``signal_quality='noise'``" first. Measured
against the live corpus that set is **empty**: ``signal_quality`` is a
:class:`~trellis.schemas.classification.ContentTags` facet, and the demote
loop that writes it — :func:`~trellis.classify.feedback.apply_noise_tags` —
takes a ``DocumentStore`` and writes through ``document_store.put``. No
graph node has ever carried the facet. Taking §3.2 literally would ship a
criterion that can only ever return zero, which is the exact defect class
the ADR exists to end.

The ADR's *reasoning* resolves the contradiction in favour of documents.
§3.4 promises "the demote loop closes physically: today ``apply_noise_tags``
demotes items into a store-forever purgatory; pruning is the missing
terminal state" — a promise only documents can keep. And §3.2's exclusion
is explicitly age-based ("confirmed entities and their documents,
*regardless of age* — age alone is not a value signal"), which a noise tag
is not: it is a quality verdict recorded by the feedback loop. So noise
*documents* are candidates here and noise-tagged nodes are not, because the
latter do not exist.

**Two lifecycle states can never select anything, and the resolver says
so (#419).** :attr:`RetentionCriteria.lifecycle_states` is typed against the
whole :data:`~trellis.schemas.classification.LifecycleState` vocabulary, but
phase one short-circuits ``archived`` (already archived — re-archiving is a
second version bump) and ``current`` (an assertion that the item belongs in
service, whoever wrote it) *before* the age gate. Both early returns are
correct; the problem is that they made those two values **unfalsifiable
criteria** — an operator who ran
``--lifecycle-state archived --older-than-days 90``, got zero, and concluded
"nothing is that stale" was told something true by accident. The pair is
named once (:data:`UNSELECTABLE_STATES`), subtracted once, reported on
:attr:`ResolutionReport.unselectable_lifecycle_states`, and carried into the
``RETENTION_PRUNED`` payload and the operator message — the same "no silent
caps" posture :attr:`ResolutionReport.scan_truncated` already takes. When a
pass requests *nothing but* unselectable states the corpus scan is skipped
outright rather than run to produce a guaranteed zero.

**The remaining three are reachable, and narrowing the type would have been
wrong.** ``draft`` and ``deprecated`` have no ``Lifecycle(...)`` construction
anywhere in ``src/`` — which is a fact about this package's own writers, not
about the criterion. ``lifecycle`` is an ordinary key in an open metadata /
property bag: MCP ``save_memory(metadata=...)`` and
``save_knowledge(properties=...)`` forward a caller's bag verbatim to the
store, and the governed ``entity.update`` verb *merges* caller properties
onto a node. Any of them can write any state, and this resolver reads the
stored string rather than a validated enum. So all three of ``draft`` /
``deprecated`` / ``superseded`` can produce a candidate; whether they do is a
question about the corpus, which is the operator's to ask. They are also
where the vocabulary is *going* — ``adr-tag-vocabulary-split.md`` §4.4 Phase 2
plans a ``LifecycleKeywordClassifier`` to populate exactly these, and
``Lifecycle`` defines the shape ahead of its consumers precisely so that
landing it is not a semantic migration. Rejecting them today would break that
on arrival. The CLI already documents the criterion this way
(``trellis curate prune --lifecycle-state`` lists "draft, deprecated,
superseded"), so what #419 found was the API and schema accepting two more
without saying they are inert — not a surface claiming five it never had.

The general rule, because this area keeps re-learning it: **state the rule,
not the writers.** Successive comments here enumerated ``updated_at``'s
readers and were wrong every time (#397 scoped it to one, #406 to two, a
review pass found a third); #419 enumerated ``Lifecycle``'s writers and
missed every open-bag surface. What a criterion can select is a property of
*this resolver's control flow*, which is checkable here — and
``TestLifecycleStateReachability`` checks it exhaustively over the enum, so a
sixth state cannot be added without landing on one side or the other.

**Grace periods apply to the age-based criteria only.** A noise tag is a
verdict, and waiting 30 days does not make it more true; requiring a grace
period there would have made the population that motivated this build
(24 job-description captures demoted on 2026-08-24) unarchivable for a
month. ``older_than_days`` therefore gates
:attr:`RetentionCriteria.unconfirmed_mints` and
:attr:`RetentionCriteria.lifecycle_states`, where age genuinely is the
signal, and does not gate :attr:`RetentionCriteria.noise_documents`.

**What is structurally excluded.** Traces and EventLog rows are not
reachable from here at all — this module reads the document store and the
graph store and nothing else, so the "never prune a trace" hard rule is
enforced by construction rather than by a check that could be edited away.
Confirmed entities are dropped explicitly (:data:`EXTRACTION_STATUS_CONFIRMED`),
and items already archived are skipped so a re-run is a no-op instead of a
second version bump.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.schemas.classification import LIFECYCLE_KEY, LifecycleState
from trellis.schemas.extraction import (
    EXTRACTION_STATUS_CONFIRMED,
    EXTRACTION_STATUS_PROPERTY,
    EXTRACTION_STATUS_UNCONFIRMED,
)

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

#: Terminal lifecycle state phase one writes. Named once so the writer, the
#: retrieval-side exclusion, and the "already archived, skip it" idempotency
#: check cannot drift apart.
ARCHIVED_STATE: LifecycleState = "archived"

#: The state :func:`~trellis.mutate.handlers.RetentionRestoreHandler._restore`
#: re-stamps, and the value both resolvers read as "a caller asserted this
#: item belongs in service".
CURRENT_STATE: LifecycleState = "current"

#: Lifecycle states a phase-one prune **can never select**, whatever the
#: corpus holds. Both resolvers short-circuit these before the age gate —
#: :data:`ARCHIVED_STATE` because re-archiving is a second version bump, and
#: :data:`CURRENT_STATE` because it is an explicit assertion retention does
#: not override by inference.
#:
#: Named so that the subtraction, the early returns and the operator-facing
#: report cannot drift apart, and so a criterion that can only ever return
#: zero is *reported* rather than silently indistinguishable from an
#: exhaustive scan that found nothing (#419). Membership is pinned by
#: behaviour, exhaustively over ``LifecycleState``, in
#: ``tests/unit/mutate/test_retention_handler.py``.
UNSELECTABLE_STATES: frozenset[LifecycleState] = frozenset(
    {ARCHIVED_STATE, CURRENT_STATE}
)

#: Hard ceiling on documents scanned per resolution pass, independent of
#: ``max_items``. ``DocumentStore`` exposes no metadata-predicate listing
#: (``search`` needs a non-empty FTS query, ``list_documents`` takes only
#: limit/offset), so noise resolution pages the corpus and filters
#: client-side. The cap keeps a maintenance verb from turning into a full
#: table scan on a corpus that has outgrown this approach — and when it
#: bites, the resolver says so rather than silently returning a short list.
MAX_DOCUMENTS_SCANNED = 20_000

#: Page size for that scan.
_SCAN_PAGE = 500

#: Cap on ``already_archived_ids`` — a follow-up pointer for vector
#: re-sync, not an exhaustive index.
_ARCHIVED_ID_LIMIT = 1000

CandidateKind = Literal["document", "entity"]

#: Why an item is a candidate. Rides the audit payload so a prune is
#: reviewable without re-running the query that produced it.
ReasonCode = Literal["noise_document", "unconfirmed_mint", "lifecycle_stale"]


class RetentionCriteria(TrellisModel):
    """Predicate naming a retention candidate population.

    Every criterion defaults to *off*: an empty criteria object resolves to
    an empty candidate set. A prune has to say what it is pruning.
    """

    noise_documents: bool = False
    """Documents whose ``content_tags.signal_quality`` is ``"noise"`` — the
    demote loop's output. Not gated by ``older_than_days`` (see module
    docstring)."""

    unconfirmed_mints: bool = False
    """Graph entities stamped ``extraction_status="unconfirmed"`` that no
    curation pass ever confirmed, older than ``older_than_days``."""

    lifecycle_states: list[LifecycleState] = Field(default_factory=list)
    """Items already carrying one of these lifecycle states, older than
    ``older_than_days``.

    Accepts the full vocabulary. :data:`UNSELECTABLE_STATES` — ``archived``
    (it is how a phase-two purge would select its input, but archiving an
    archived item is a no-op) and ``current`` (an assertion that the item
    belongs in service) — are short-circuited by phase one and reported on
    :attr:`ResolutionReport.unselectable_lifecycle_states` rather than
    silently returning zero. The rest are selectable; see the module
    docstring for why ``draft`` / ``deprecated`` are kept despite having no
    in-package writer."""

    older_than_days: int = Field(default=30, ge=0)
    """Grace period for the age-based criteria above."""

    max_items: int = Field(default=500, ge=1)
    """Cap on candidates returned by one pass. A batch verb that can select
    the whole corpus in one command is a footgun; page instead."""


class RetentionCandidate(TrellisModel):
    """One resolved candidate, with the reason it qualified."""

    item_id: str
    kind: CandidateKind
    reason_code: ReasonCode
    name: str | None = None


class ResolutionReport(TrellisModel):
    """What one resolution pass looked at and what it selected.

    ``scan_truncated`` exists so a capped scan is never mistaken for an
    exhaustive one — the "no silent caps" rule. When it is ``True`` the
    candidate set is a prefix of the real population, not the whole of it.
    ``unselectable_lifecycle_states`` is the same rule one level up: a zero
    that was *guaranteed by the criteria* must not read as a zero the corpus
    produced.
    """

    candidates: list[RetentionCandidate] = Field(default_factory=list)
    documents_scanned: int = 0
    entities_scanned: int = 0
    scan_truncated: bool = False
    skipped_already_archived: int = 0
    skipped_confirmed: int = 0
    skipped_restored: int = 0

    unselectable_lifecycle_states: list[LifecycleState] = Field(default_factory=list)
    """Requested ``lifecycle_states`` this phase can never select (#419).

    The intersection of the caller's request with
    :data:`UNSELECTABLE_STATES`, sorted. Non-empty means part of the
    criteria was inert: those states contributed no scan and no candidate,
    so a zero result says nothing about how many such items exist. Empty is
    the ordinary case and asserts the converse — every requested state
    reached the age gate.
    """

    already_archived_ids: list[str] = Field(default_factory=list)
    """Ids skipped because they are already archived.

    Surfaced so the handler can re-sync their vector rows: a document
    archived before :func:`~trellis.mutate.handlers._sync_vector_lifecycle`
    existed still has a stale snapshot, and re-running the prune is the
    natural place to repair it. Capped like every other id pointer."""

    @property
    def truncated_by_max_items(self) -> bool:
        """Whether ``max_items`` (rather than the scan cap) bounded the set."""
        return not self.scan_truncated and bool(self.candidates)


def _lifecycle_state(bag: dict[str, Any] | None) -> str | None:
    """Read ``Lifecycle.state`` out of a metadata / properties bag."""
    if not bag:
        return None
    record = bag.get(LIFECYCLE_KEY)
    if isinstance(record, dict):
        state = record.get("state")
        return state if isinstance(state, str) else None
    return None


def _signal_quality(metadata: dict[str, Any] | None) -> str | None:
    """Read ``ContentTags.signal_quality`` out of a document metadata bag."""
    if not metadata:
        return None
    tags = metadata.get("content_tags")
    if isinstance(tags, dict):
        value = tags.get("signal_quality")
        return value if isinstance(value, str) else None
    return None


def _parse_ts(value: Any) -> datetime | None:
    """Best-effort ISO-8601 parse; unparseable timestamps read as unknown."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_older_than(value: Any, cutoff: datetime) -> bool:
    """Whether ``value`` is a timestamp strictly older than ``cutoff``.

    An unparseable or absent timestamp reads as **not** old enough. Age is
    the whole justification for the age-based criteria, so an item whose age
    cannot be established is not a candidate — failing closed keeps a
    missing field from reading as "infinitely old".
    """
    parsed = _parse_ts(value)
    return parsed is not None and parsed < cutoff


def _classify_document(
    doc: dict[str, Any],
    metadata: dict[str, Any],
    criteria: RetentionCriteria,
    wanted_states: set[LifecycleState],
    cutoff: datetime,
    report: ResolutionReport,
) -> ReasonCode | None:
    """Decide whether one document is a candidate, updating skip counters.

    Split out of :func:`_resolve_documents` to keep the paging loop legible;
    the lifecycle-state guards below are the reason it grew.
    """
    state = _lifecycle_state(metadata)

    # Already archived: a re-run must not bump a second version. The id is
    # still collected so the handler can repair a vector row that was
    # stamped before the sync existed.
    if state == ARCHIVED_STATE:
        report.skipped_already_archived += 1
        doc_id = doc.get("doc_id") or doc.get("id")
        if doc_id and len(report.already_archived_ids) < _ARCHIVED_ID_LIMIT:
            report.already_archived_ids.append(str(doc_id))
        return None

    # Explicitly restored: never re-select.
    #
    # The rule is about the *value*, not its provenance. ``state="current"``
    # is an assertion that this item belongs in service, and retention never
    # overrides an explicit assertion by inference. ``retention.restore`` is
    # the writer that motivated the guard: the tag that selected the item is
    # still on the document (restore un-archives; it does not re-classify,
    # which is the classify layer's business), so without this branch the
    # next prune re-archives everything a human just rescued — precisely
    # what the first production restore set up.
    #
    # Nothing here depends on that being the *only* writer, and it is not.
    # An earlier draft of this comment asserted "``Lifecycle`` has exactly
    # one writer — retention", which was already false:
    # ``mcp.reconcile.mark_document_superseded`` writes one, and more to the
    # point ``lifecycle`` is an ordinary key in an open metadata / property
    # bag, so every surface that forwards a caller's bag verbatim (MCP
    # ``save_memory`` / ``save_knowledge``, the governed ``entity.update``
    # property merge) can write any state at all. State the rule, not the
    # writers — the same lesson the ``updated_at`` comment below records,
    # and the one #419 itself tripped on.
    #
    # A restored item can still be archived deliberately by naming it: it is
    # protected from the *criteria*, not from the operator.
    if state == CURRENT_STATE:
        report.skipped_restored += 1
        return None

    if criteria.noise_documents and _signal_quality(metadata) == "noise":
        return "noise_document"
    if wanted_states and state in wanted_states:
        # One of the consumers ``DocumentStore.put(preserve_updated_at=...)``
        # protects, and the one most easily missed (#406). The age meant here
        # is the *document's* age, per this module's docstring; a
        # metadata-only write that omits the flag makes the criterion measure
        # time since that write instead, shielding a genuinely stale row for
        # a further ``older_than_days``.
        #
        # Deliberately not phrased as "the second reader". That column is
        # public on every ``DocumentStore`` row and its readers are an open
        # set — #397 scoped it to one, #406 to two, and a review pass found a
        # third (``retrieve.file_context._newest_timestamp``). An ordinal here
        # would be the fourth restatement of a closed enumeration that has
        # been wrong every time it was written down.
        #
        # This module's own handlers cannot reach here: ``archived`` and
        # ``current`` both return above. The writers that can are the ones
        # leaving ``state`` alone while rewriting derived metadata — neither
        # ``worker enrich`` nor ``classify.feedback.apply_noise_tags`` filters
        # on lifecycle, so a ``superseded`` row is an ordinary target for
        # either. (``apply_noise_tags`` takes an explicit id list rather than
        # running a query; the point is that nothing upstream screens it.)
        if not _is_older_than(doc.get("updated_at") or doc.get("created_at"), cutoff):
            return None
        return "lifecycle_stale"
    return None


def resolve_candidates(
    criteria: RetentionCriteria,
    registry: StoreRegistry,
) -> ResolutionReport:
    """Resolve ``criteria`` to concrete candidates against the live stores.

    Reads the document store and the graph store only. Traces and EventLog
    rows are unreachable from here by construction.
    """
    report = ResolutionReport()
    cutoff = datetime.now(UTC) - timedelta(days=criteria.older_than_days)
    seen: set[str] = set()

    # Split the requested states once, here, rather than re-deriving the
    # subtraction inside each resolver — two copies of "which states count"
    # is how a report stops matching what the scan did.
    requested = set(criteria.lifecycle_states)
    wanted_states = requested - UNSELECTABLE_STATES
    report.unselectable_lifecycle_states = sorted(requested & UNSELECTABLE_STATES)
    if report.unselectable_lifecycle_states:
        # Not a failure: ``archived`` is a documented phase-two input and an
        # operator may legitimately pass it. But it selected nothing and
        # scanned nothing, so the zero it contributes is a property of the
        # criteria and has to be visible as one.
        logger.warning(
            "retention_unselectable_lifecycle_states",
            requested=sorted(requested),
            unselectable=report.unselectable_lifecycle_states,
            selectable=sorted(wanted_states),
        )

    def _remaining() -> int:
        return criteria.max_items - len(report.candidates)

    # Gated on ``wanted_states``, not on the raw request: a pass asking only
    # for unselectable states would otherwise page the whole corpus to
    # produce a guaranteed zero.
    if criteria.noise_documents or wanted_states:
        _resolve_documents(
            criteria, registry, report, cutoff, seen, _remaining, wanted_states
        )

    if criteria.unconfirmed_mints or wanted_states:
        _resolve_entities(
            criteria, registry, report, cutoff, seen, _remaining, wanted_states
        )

    logger.info(
        "retention_candidates_resolved",
        candidates=len(report.candidates),
        documents_scanned=report.documents_scanned,
        entities_scanned=report.entities_scanned,
        scan_truncated=report.scan_truncated,
        unselectable_lifecycle_states=report.unselectable_lifecycle_states,
    )
    return report


def _resolve_documents(
    criteria: RetentionCriteria,
    registry: StoreRegistry,
    report: ResolutionReport,
    cutoff: datetime,
    seen: set[str],
    remaining: Any,
    wanted_states: set[LifecycleState],
) -> None:
    """Page the document corpus and select noise / lifecycle-stale rows.

    ``wanted_states`` arrives already stripped of :data:`UNSELECTABLE_STATES`
    by :func:`resolve_candidates` — the one place that subtraction happens.
    """
    store = registry.knowledge.document_store
    offset = 0

    while remaining() > 0 and report.documents_scanned < MAX_DOCUMENTS_SCANNED:
        page = store.list_documents(limit=_SCAN_PAGE, offset=offset)
        if not page:
            return
        offset += len(page)

        for doc in page:
            report.documents_scanned += 1
            doc_id = doc.get("doc_id") or doc.get("id")
            if not doc_id or doc_id in seen:
                continue
            metadata = doc.get("metadata") or {}

            reason = _classify_document(
                doc, metadata, criteria, wanted_states, cutoff, report
            )
            if reason is None:
                continue

            seen.add(doc_id)
            report.candidates.append(
                RetentionCandidate(
                    item_id=doc_id,
                    kind="document",
                    reason_code=reason,
                    name=(
                        metadata.get("title") if isinstance(metadata, dict) else None
                    ),
                )
            )
            if remaining() <= 0:
                return

        hit_cap = report.documents_scanned >= MAX_DOCUMENTS_SCANNED
        if hit_cap and len(page) == _SCAN_PAGE:
            report.scan_truncated = True
            logger.warning(
                "retention_scan_truncated",
                scanned=report.documents_scanned,
                cap=MAX_DOCUMENTS_SCANNED,
            )
            return


def _resolve_entities(
    criteria: RetentionCriteria,
    registry: StoreRegistry,
    report: ResolutionReport,
    cutoff: datetime,
    seen: set[str],
    remaining: Any,
    wanted_states: set[LifecycleState],
) -> None:
    """Select unconfirmed mints / lifecycle-stale graph entities.

    ``wanted_states`` arrives already stripped of :data:`UNSELECTABLE_STATES`
    by :func:`resolve_candidates` — the one place that subtraction happens.
    """
    graph = registry.knowledge.graph_store

    rows: list[dict[str, Any]] = []
    if criteria.unconfirmed_mints:
        rows.extend(
            graph.query(
                properties={EXTRACTION_STATUS_PROPERTY: EXTRACTION_STATUS_UNCONFIRMED},
                limit=criteria.max_items,
            )
        )
    if wanted_states:
        # Lifecycle lives in the property bag; query by type-free scan and
        # filter client-side, mirroring the document path.
        rows.extend(graph.query(limit=criteria.max_items))

    for node in rows:
        report.entities_scanned += 1
        node_id = node.get("node_id")
        if not node_id or node_id in seen:
            continue
        props = node.get("properties") or {}

        if props.get(EXTRACTION_STATUS_PROPERTY) == EXTRACTION_STATUS_CONFIRMED:
            # Never a candidate, on any criterion: confirmation is the
            # signal that a human decided this entity is real.
            report.skipped_confirmed += 1
            continue
        node_state = _lifecycle_state(props)
        if node_state == ARCHIVED_STATE:
            report.skipped_already_archived += 1
            continue
        if node_state == CURRENT_STATE:
            report.skipped_restored += 1
            continue

        reason: ReasonCode | None = None
        if (
            criteria.unconfirmed_mints
            and props.get(EXTRACTION_STATUS_PROPERTY) == EXTRACTION_STATUS_UNCONFIRMED
            and _is_older_than(node.get("valid_from") or node.get("created_at"), cutoff)
        ):
            reason = "unconfirmed_mint"
        elif (
            wanted_states
            # ``node_state``, not a second ``_lifecycle_state(props)`` call:
            # the guards above already read the state, and two reads of one
            # bag is how a guard and its criterion start disagreeing.
            and node_state in wanted_states
            and _is_older_than(node.get("valid_from") or node.get("created_at"), cutoff)
        ):
            reason = "lifecycle_stale"

        if reason is None:
            continue

        seen.add(node_id)
        report.candidates.append(
            RetentionCandidate(
                item_id=node_id,
                kind="entity",
                reason_code=reason,
                name=props.get("name") if isinstance(props, dict) else None,
            )
        )
        if remaining() <= 0:
            return


__all__ = [
    "ARCHIVED_STATE",
    "CURRENT_STATE",
    "MAX_DOCUMENTS_SCANNED",
    "UNSELECTABLE_STATES",
    "CandidateKind",
    "ReasonCode",
    "ResolutionReport",
    "RetentionCandidate",
    "RetentionCriteria",
    "resolve_candidates",
]
