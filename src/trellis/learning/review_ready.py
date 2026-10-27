"""The reviewer's view of the promotion-ready learning candidates.

``trellis curate review-ready`` is the human half of the promote loop in one
place: it lists the candidates the #845 digest announces, with the evidence a
reviewer needs to say yes or no, and approves the ones named. Nothing here
decides anything on a reviewer's behalf and nothing here writes: the rows are
a read, and the approval itself goes through
:func:`trellis.learning.scoring.submit_learning_promotion` in the CLI.

Three things make a thin ready set hard to review from a count alone, and each
is a field on the row rather than a hidden filter:

* most candidates rest on one helpful citation, so the counts are shown
  (``helpful_count``, ``unhelpful_count``, ``distinct_helpful_sessions``,
  ``sessionless_helpful_count``) beside the item's own words;
* the same item is often an ``investigate_noise`` candidate under another
  intent family (``cross_family_noise``), which contradicts the promotion
  even though neither candidate's own citations do;
* a precedent may already exist (``already_promoted``).

The readiness predicate is :func:`trellis.learning.scoring.is_promotion_ready`,
imported rather than restated, so a digest count and this list cannot drift.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from trellis.learning.candidate_auto_promote import _scan_already_promoted_entity_ids
from trellis.learning.scoring import (
    is_promotion_ready,
    precedent_entity_id_for_candidate,
    promotion_ready_sort_key,
    resolve_source_excerpt,
)
from trellis.retrieve.excerpts import truncate_excerpt

if TYPE_CHECKING:
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.event_log import EventLog
    from trellis.stores.base.graph import GraphStore

#: Longest source excerpt a review row carries. Shorter than the 600 chars a
#: promotion embeds: this is for deciding, not for serving.
REVIEW_EXCERPT_MAX_CHARS = 300

#: ``recommendation_type`` of the candidates that argue an item is noise.
NOISE_RECOMMENDATION = "investigate_noise"

#: Per-candidate approval statuses this module assigns. The remaining ones
#: (``promoted``, ``entity_failed``, ``refused_<lifecycle state>``) come back
#: from :func:`trellis.learning.scoring.submit_learning_promotion`.
REFUSED_NOT_READY = "refused_not_ready"
REFUSED_ALREADY_PROMOTED = "refused_already_promoted"
REFUSED_CONTRADICTED = "refused_contradicted"


def noise_families_by_item(
    candidates: Iterable[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Map ``item_id`` to the intent families that propose it as noise.

    Built over *every* candidate, not the ready set: the contradiction a
    reviewer needs is precisely a candidate this list would not otherwise show.
    """
    families: dict[str, set[str]] = {}
    for candidate in candidates:
        if candidate.get("recommendation_type") != NOISE_RECOMMENDATION:
            continue
        item_id = str(candidate.get("item_id") or "").strip()
        if item_id:
            families.setdefault(item_id, set()).add(
                str(candidate.get("intent_family") or "").strip()
            )
    return {item_id: sorted(found) for item_id, found in families.items()}


def make_promoted_probe(
    event_log: EventLog, graph_store: GraphStore
) -> Callable[[str], bool]:
    """Return ``entity_id -> bool``: does a precedent already exist there?

    True on either signal: a successful ``precedent.promote`` event, which is
    how ``auto-promote-candidates`` defines it (that scan is newest-first and
    capped, so it can lose an old promotion on a busy log), or a ``precedent``
    node at the id, which the cap cannot lose. A precedent in any lifecycle
    state counts: a demoted one is still not a candidate to promote afresh.
    """
    promoted_by_event = _scan_already_promoted_entity_ids(event_log)

    def probe(entity_id: str) -> bool:
        if entity_id in promoted_by_event:
            return True
        node = graph_store.get_node(entity_id)
        return node is not None and node.get("node_type") == "precedent"

    return probe


def _int_or_none(value: Any) -> int | None:
    """An int count, or ``None`` when an older artifact never wrote the field."""
    return int(value or 0) if value is not None else None


def _row(
    candidate: Mapping[str, Any],
    *,
    noise_families: Mapping[str, list[str]],
    is_promoted: Callable[[str], bool],
    document_store: DocumentStore | None,
) -> dict[str, Any]:
    candidate_id = str(candidate.get("candidate_id") or "")
    item_id = str(candidate.get("item_id") or "").strip()
    citations = candidate.get("citation_evidence")
    citations = citations if isinstance(citations, Mapping) else {}
    excerpt, excerpt_status = resolve_source_excerpt(item_id, document_store)
    if excerpt is not None:
        excerpt = truncate_excerpt(excerpt, limit=REVIEW_EXCERPT_MAX_CHARS)
    entity_id = precedent_entity_id_for_candidate(candidate_id)
    noise_in = noise_families.get(item_id, [])
    metrics = candidate.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    targets = candidate.get("target_entity_ids")
    return {
        "candidate_id": candidate_id,
        "intent_family": candidate.get("intent_family"),
        "item_id": item_id,
        "entity_id": entity_id,
        "source_excerpt": excerpt,
        "source_excerpt_status": excerpt_status,
        "helpful_count": int(citations.get("helpful_count", 0) or 0),
        "unhelpful_count": int(citations.get("unhelpful_count", 0) or 0),
        "distinct_helpful_sessions": _int_or_none(
            citations.get("distinct_helpful_sessions")
        ),
        "sessionless_helpful_count": _int_or_none(
            citations.get("sessionless_citations")
        ),
        "times_served": int(metrics.get("times_served", 0) or 0),
        "target_entity_count": len(targets) if isinstance(targets, list) else 0,
        "cross_family_noise": bool(noise_in),
        "noise_intent_families": noise_in,
        "already_promoted": is_promoted(entity_id),
    }


def build_review_rows(
    candidates: Sequence[Mapping[str, Any]],
    *,
    document_store: DocumentStore | None,
    is_promoted: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """Every promotion-ready candidate as a review row, best-supported first.

    Order is the digest's (:func:`promotion_ready_sort_key`), except that a
    cross-family-contradicted candidate sorts after the clean ones and an
    already-promoted one last of all: the head of the list is what a reviewer
    can act on.
    """
    mappings = [c for c in candidates if isinstance(c, Mapping)]
    noise_families = noise_families_by_item(mappings)
    ready = sorted(
        (c for c in mappings if is_promotion_ready(c)), key=promotion_ready_sort_key
    )
    rows = [
        _row(
            candidate,
            noise_families=noise_families,
            is_promoted=is_promoted,
            document_store=document_store,
        )
        for candidate in ready
    ]
    # Stable: ties keep the digest order from the sort above.
    rows.sort(key=lambda row: (row["already_promoted"], row["cross_family_noise"]))
    return rows


def approval_refusal(
    candidate_id: str,
    row: Mapping[str, Any] | None,
    *,
    force_contradicted: bool,
) -> dict[str, Any] | None:
    """Why ``candidate_id`` may not be approved, as an outcome, or ``None``.

    ``row`` is the candidate's review row, ``None`` when it is not in the
    ready set. Checked in the order a reviewer should hear them: not ready,
    already promoted, cross-family contradicted. ``force_contradicted``
    lifts only the last.

    A blank id is not ready whatever ``row`` holds. A malformed artifact can
    carry a ready row whose ``candidate_id`` is blank, and ``--approve ""``
    then finds it; the planner strips the id and skips the row, so there is
    nothing to submit. It is refused here, before that.
    """
    if row is None or not candidate_id.strip():
        return {
            "candidate_id": candidate_id,
            "status": REFUSED_NOT_READY,
            "message": (
                "not in the promotion-ready set (unknown id, not a "
                "promote_guidance candidate, no helpful citation, or an "
                "unhelpful one)"
            ),
        }
    if row["already_promoted"]:
        return {
            "candidate_id": candidate_id,
            "status": REFUSED_ALREADY_PROMOTED,
            "entity_id": row["entity_id"],
            "message": f"a precedent already exists at {row['entity_id']}",
        }
    if row["cross_family_noise"] and not force_contradicted:
        families = ", ".join(row["noise_intent_families"])
        return {
            "candidate_id": candidate_id,
            "status": REFUSED_CONTRADICTED,
            "noise_intent_families": row["noise_intent_families"],
            "message": (
                f"the same item is an {NOISE_RECOMMENDATION} candidate under "
                f"another intent family ({families}); pass --force-contradicted "
                "to promote it anyway"
            ),
        }
    return None


__all__ = [
    "NOISE_RECOMMENDATION",
    "REFUSED_ALREADY_PROMOTED",
    "REFUSED_CONTRADICTED",
    "REFUSED_NOT_READY",
    "REVIEW_EXCERPT_MAX_CHARS",
    "approval_refusal",
    "build_review_rows",
    "make_promoted_probe",
    "noise_families_by_item",
]
