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
    ``older_than_days``. ``archived`` is accepted (it is how a phase-two
    purge would select its input) but archiving an archived item is a
    no-op, so phase one drops those."""

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
    """

    candidates: list[RetentionCandidate] = Field(default_factory=list)
    documents_scanned: int = 0
    entities_scanned: int = 0
    scan_truncated: bool = False
    skipped_already_archived: int = 0
    skipped_confirmed: int = 0

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

    def _remaining() -> int:
        return criteria.max_items - len(report.candidates)

    if criteria.noise_documents or criteria.lifecycle_states:
        _resolve_documents(criteria, registry, report, cutoff, seen, _remaining)

    if criteria.unconfirmed_mints or criteria.lifecycle_states:
        _resolve_entities(criteria, registry, report, cutoff, seen, _remaining)

    logger.info(
        "retention_candidates_resolved",
        candidates=len(report.candidates),
        documents_scanned=report.documents_scanned,
        entities_scanned=report.entities_scanned,
        scan_truncated=report.scan_truncated,
    )
    return report


def _resolve_documents(
    criteria: RetentionCriteria,
    registry: StoreRegistry,
    report: ResolutionReport,
    cutoff: datetime,
    seen: set[str],
    remaining: Any,
) -> None:
    """Page the document corpus and select noise / lifecycle-stale rows."""
    store = registry.knowledge.document_store
    offset = 0
    wanted_states = set(criteria.lifecycle_states) - {ARCHIVED_STATE}

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

            # Already archived: a re-run must not bump a second version.
            if _lifecycle_state(metadata) == ARCHIVED_STATE:
                report.skipped_already_archived += 1
                continue

            reason: ReasonCode | None = None
            if criteria.noise_documents and _signal_quality(metadata) == "noise":
                reason = "noise_document"
            elif wanted_states and _lifecycle_state(metadata) in wanted_states:
                if not _is_older_than(
                    doc.get("updated_at") or doc.get("created_at"), cutoff
                ):
                    continue
                reason = "lifecycle_stale"

            if reason is None:
                continue

            seen.add(doc_id)
            report.candidates.append(
                RetentionCandidate(
                    item_id=doc_id,
                    kind="document",
                    reason_code=reason,
                    name=(
                        metadata.get("title")
                        if isinstance(metadata, dict)
                        else None
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
) -> None:
    """Select unconfirmed mints / lifecycle-stale graph entities."""
    graph = registry.knowledge.graph_store
    wanted_states = set(criteria.lifecycle_states) - {ARCHIVED_STATE}

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
        if _lifecycle_state(props) == ARCHIVED_STATE:
            report.skipped_already_archived += 1
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
            and _lifecycle_state(props) in wanted_states
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
    "MAX_DOCUMENTS_SCANNED",
    "CandidateKind",
    "ReasonCode",
    "ResolutionReport",
    "RetentionCandidate",
    "RetentionCriteria",
    "resolve_candidates",
]
