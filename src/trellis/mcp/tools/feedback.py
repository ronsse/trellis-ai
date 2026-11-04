"""``record_feedback`` and the pack-attribution checks behind it."""

from __future__ import annotations

from typing import Any, cast

import structlog

from trellis.auth import SCOPE_MUTATE
from trellis.core.write_config import resolve_write_behaviour
from trellis.feedback.attribution import (
    cited_item_ids,
    lookup_pack_bodied_item_ids,
    lookup_pack_item_ids,
    lookup_pack_membership,
)
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import feedback_log_dir
from trellis.feedback.recording import record_feedback as record_pack_feedback
from trellis.mcp.app import mcp
from trellis.mcp.auth import current_clearance, trellis_scope
from trellis.mcp.boundary import _raise_internal, _raise_invalid_params
from trellis.mcp.rejections import _record_boundary_rejection
from trellis.mcp.wiring import _get_registry
from trellis.retrieve.sensitivity import readable_ids
from trellis.stores.base.document import DocumentStore
from trellis.stores.registry import StoreRegistry
from trellis_wire.feedback_hints import rating_out_of_range_hint

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Macro Tool 7: record_feedback
# ---------------------------------------------------------------------------


#: Most item ids handed back in an attribution rejection. A pack is
#: budget-capped well below this; the cap only bounds the error payload
#: against a pathological pack, it is not a normal truncation point.
_MAX_CITABLE_IDS_IN_ERROR = 60

#: Most ids written into the *text* of an attribution refusal. ``McpError.data``
#: (which carries up to :data:`_MAX_CITABLE_IDS_IN_ERROR`) is dropped by FastMCP
#: before a client sees the result, so the message is the only place a caller
#: can read them. Twenty is enough to choose among and keeps the text near a
#: couple of hundred tokens; the count of the rest is stated beside the list.
_MAX_CITABLE_IDS_IN_MESSAGE = 20

#: Most ids named in the note on a feedback reply, and the longest one echoed.
#: Ids reach the reply and the refusal text from the caller or from a stored
#: pack, so :func:`_echo_id` bounds each one; the caps say nothing about the pack.
_MAX_UNSERVED_IDS_IN_NOTE = 10
_MAX_ECHOED_ID_CHARS = 100


def _echo_id(item_id: str) -> str:
    """An id for a line of text: clipped, and safe to put between backticks.

    An id is an arbitrary string, and FastMCP logs a refusal's message through
    Rich, so an unbounded one can cost minutes. It is clipped first, so the
    cost is the clip's, then each character that is not printable (a newline,
    ESC, a line separator, a bidi mark) or is a backtick becomes ``?``: an id
    cannot close its own span, start a second line or move the cursor.
    """
    clipped = item_id[:_MAX_ECHOED_ID_CHARS]
    safe = "".join(c if c.isprintable() and c != "`" else "?" for c in clipped)
    return safe + "…" if len(item_id) > _MAX_ECHOED_ID_CHARS else safe


def _id_list_clause(label: str, item_ids: list[str]) -> str:
    """`` <label>: `a`, `b`, ...`` for a refusal's text, capped, rest counted.

    ``item_ids`` must already be the ones this caller may read
    (:func:`_readable_served_ids`); nothing here re-checks clearance. Each id
    goes through :func:`_echo_id`, so a stored id cannot bloat or forge the text.
    """
    shown = item_ids[:_MAX_CITABLE_IDS_IN_MESSAGE]
    listed = ", ".join(f"`{_echo_id(item_id)}`" for item_id in shown)
    clause = f" {label}: {listed}"
    if len(item_ids) > len(shown):
        clause += f" (and {len(item_ids) - len(shown)} more not listed)"
    return f"{clause}."


def _readable_served_ids(registry: StoreRegistry, item_ids: list[str]) -> list[str]:
    """The ids of a named pack's items that the caller may read (#1029).

    A pack is looked up by id alone, so the caller need not be the one who
    built it, and the ids it served were cleared for *that* caller. The
    attribution refusal hands them back, so each is judged again against this
    caller's clearance, as ``get_items`` judges a by-id read. An id that fails
    is neither named nor counted: the refusal reads as it would for a pack
    that served only the readable ids, and a pack that served none reads as
    an unknown pack, which the check lets through. The rule is by clearance
    and not by owner because ``PACK_ASSEMBLED`` records no principal, and a
    pack's own caller reads everything it was served.
    """
    return readable_ids(
        registry.knowledge.document_store, item_ids, current_clearance()
    )


class _FailureNotingStore:
    """A document-store view that remembers whether any read raised.

    ``readable_ids`` fails closed on a store error, so an outage reads the same
    as "nothing the caller may read". The note has to tell the two apart, so it
    hands ``readable_ids`` this view: every read still raises into it as before.
    """

    def __init__(self, store: DocumentStore) -> None:
        self._store = store
        self.failed = False

    def get(self, doc_id: str) -> dict[str, Any] | None:
        try:
            return self._store.get(doc_id)
        except Exception:
            self.failed = True
            raise


def _readable_ids_if_checked(
    registry: StoreRegistry, item_ids: list[str]
) -> list[str] | None:
    """:func:`_readable_served_ids`, or ``None`` when a read of it failed.

    ``[]`` means the store answered and the caller may read none of them;
    ``None`` means it could not be established, which is no answer at all.
    """
    view = _FailureNotingStore(registry.knowledge.document_store)
    readable = readable_ids(cast("DocumentStore", view), item_ids, current_clearance())
    return None if view.failed else readable


def _require_pack_attribution(
    registry: StoreRegistry, pack_id: str, *, ignored_count: int = 0
) -> None:
    """Reject an uncited pack-targeted feedback call, when configured to.

    Off by default — see
    :data:`trellis.core.write_config.REQUIRE_PACK_ATTRIBUTION_FLAG` for
    why the default is the operator's call and not this module's.

    Fails **open** in every uncertain case. The rejection is only raised
    when the pack resolves to at least one item the caller could have
    cited: an unknown ``pack_id``, a pack that predates
    ``injected_item_ids``, a sectioned pack (which emits no per-item rows
    at all), or an event-log outage each let the call through. Refusing a
    caller for not citing ids nobody can produce would convert a recorded
    rating into a lost one and teach the agent that the tool is
    unreliable — the opposite of the intent.

    Nothing here writes attribution. The served ids ride the error's message
    (its ``data`` does not reach a client) so the caller can *choose* among
    them; which of them helped is a judgement only the caller holds, and
    synthesising it would manufacture exactly the signal this whole change
    exists to measure honestly.

    ``ignored_count`` is not a citation and does not satisfy this gate —
    it only changes what the refusal *says*. The trigger deliberately
    stays the three keys
    :func:`~trellis.feedback.attribution.payload_is_attributed` reads, so
    the boundary and the health report cannot disagree about what
    "attributed" means; an ignored-only call is honest, complete and
    still joins to nothing, and telling the caller that is more use than
    a message claiming they cited nothing.
    """
    if not resolve_write_behaviour(registry).require_pack_attribution:
        return

    item_ids = _readable_served_ids(
        registry, lookup_pack_item_ids(registry.operational.event_log, pack_id)
    )
    if not item_ids:
        logger.debug("pack_attribution_not_enforceable", pack_id=pack_id)
        return

    if ignored_count:
        detail = (
            f"{ignored_count} item(s) marked ignored, which records that they "
            "were not used but contributes no rows to the learning join"
        )
        remedy = (
            "name at least one id in helpful_item_ids, or in unhelpful_item_ids "
            "if the pack was actively misleading"
        )
    else:
        detail = "none cited"
        remedy = (
            "Put the ids that helped in helpful_item_ids; if none did, put the "
            "ones that were noise in unhelpful_item_ids"
        )

    _record_boundary_rejection(
        tool="record_feedback",
        rejections=[
            {
                "kind": "missing",
                "loc": "helpful_item_ids|unhelpful_item_ids",
                "msg": f"pack {pack_id} served {len(item_ids)} item(s), {detail}",
            }
        ],
        hints=[
            "cite the item_ids that helped in helpful_item_ids",
            "a pack that missed is cited in unhelpful_item_ids, not left blank",
            "ignored_item_ids records non-use and does not stand in for either",
        ],
    )
    _raise_invalid_params(
        f"feedback on pack {pack_id} must cite at least one item — it served "
        f"{len(item_ids)} and {detail}. {remedy}."
        + _id_list_clause("Served ids", item_ids),
        data={
            "pack_id": pack_id,
            "item_ids": item_ids[:_MAX_CITABLE_IDS_IN_ERROR],
            "fields": [
                "helpful_item_ids",
                "unhelpful_item_ids",
                "followed_advisory_ids",
            ],
        },
    )


_UNKNOWN_PACK_NOTE = (
    "note: pack {pack_id} is unknown (no assembled pack with that id is visible "
    "to this caller), so this feedback cannot be confirmed to join any item or "
    "earn credit"
)


def _unserved_citation_note(
    registry: StoreRegistry, pack_id: str, cited: list[str]
) -> str | None:
    """A line for the reply saying which cited ids the pack did not serve.

    Advisory only: it is read after the feedback is written and changes
    neither the event, the JSONL row nor whether the call is refused. ``cited``
    is the two lists the learning join credits
    (:func:`~trellis.feedback.attribution.cited_item_ids`), tested for
    membership as :func:`~trellis.feedback.attribution.stray_citations` tests
    it, at the moment the caller can still fix the id.

    The note is optional, so a sentence the code cannot establish is left out:
    a failed event-log read or a failed readability check gives no note, since
    neither says whether the pack exists or what it served.

    * An **unknown pack** (no assembled-pack record) says so; the call was
      recorded as it always was.
    * A pack with **no recorded membership** (assembled empty, withheld, older
      than the payload key) says nothing. A withheld pack must not read any
      differently from one assembled empty, so nothing here reads its
      would-be items, and an empty membership is never subtracted from.
    * Clearance: an id the pack served that this caller may not read counts as
      unserved, the way :func:`_readable_served_ids` leaves it out of a
      refusal, and a pack whose served ids are all unreadable reads as unknown,
      whether or not the call cites anything, so two calls cannot tell
      "above my clearance" from "no such pack". The note never confirms that
      an id the caller cannot read is in the pack.

    Wrapped whole. It runs on the write path and an escaped exception would
    fail a call whose feedback is already recorded.
    """
    try:
        unknown = _UNKNOWN_PACK_NOTE.format(pack_id=_echo_id(pack_id.strip()))
        served = lookup_pack_membership(registry.operational.event_log, pack_id)
        if served is None:
            return unknown
        if not served:
            return None
        readable = _readable_ids_if_checked(registry, sorted(served))
        if readable is None:
            return None
        if not readable:
            return unknown
        strays = [item_id for item_id in cited if item_id not in set(readable)]
        if not strays:
            return None
        shown = strays[:_MAX_UNSERVED_IDS_IN_NOTE]
        listed = ", ".join(f"`{_echo_id(item_id)}`" for item_id in shown)
        rest = len(strays) - len(shown)
        more = f" (and {rest} more)" if rest else ""
        noun = "id was" if len(strays) == 1 else "ids were"
        possessive = "its" if len(strays) == 1 else "their"
        return (
            f"note: {len(strays)} cited {noun} not among the items this pack "
            f"served that this caller can see, so {possessive} credit cannot be "
            f"confirmed: {listed}{more}"
        )
    except Exception:
        # GRACEFUL-DEGRADATION: wrapped whole, not by exception type. The
        # note is advice on a reply whose feedback is already recorded, and
        # one escaped exception here would turn a recorded call into a
        # failed one.
        logger.exception("unserved_citation_note_failed", pack_id=pack_id)
        return None


def _require_bodied_attribution(
    registry: StoreRegistry, pack_id: str, *, judged: set[str]
) -> None:
    """Reject a pack-targeted call that leaves a bodied item unjudged.

    Off by default — see
    :data:`trellis.core.write_config.REQUIRE_BODIED_ATTRIBUTION_FLAG`,
    which carries the measurement behind both the denominator and the
    cost of the ask.

    The question is coverage, not joinability: every item the pack put an
    *excerpt* in front of the caller should come back in one of
    ``helpful_item_ids`` / ``unhelpful_item_ids`` / ``ignored_item_ids``.
    A followed advisory is not a pack item and cannot discharge an item's
    verdict, so it is absent from that list.

    Pointers are outside the ask by construction. A one-line pointer
    names an item and withholds its body, so a caller has no basis for a
    verdict beyond the label — and the two populations behave nothing
    alike: bodied items draw a verdict at twice the rate and a *helpful*
    verdict at six times it. :func:`lookup_pack_bodied_item_ids` is the
    one place that boundary is read.

    Fails **open** wherever the bodied set is unknown — an unresolvable
    pack, a sectioned pack, an index pack, an outage, or a disclosure
    record whose shape this build does not recognise. Refusing a caller
    for not judging an item they were never shown is the expensive
    mistake, and a gate that cannot be computed must not be enforced.
    """
    if not resolve_write_behaviour(registry).require_bodied_attribution:
        return

    bodied = _readable_served_ids(
        registry, lookup_pack_bodied_item_ids(registry.operational.event_log, pack_id)
    )
    if not bodied:
        logger.debug("bodied_attribution_not_enforceable", pack_id=pack_id)
        return

    unjudged = [item_id for item_id in bodied if item_id not in judged]
    if not unjudged:
        return

    _record_boundary_rejection(
        tool="record_feedback",
        rejections=[
            {
                "kind": "missing",
                "loc": "helpful_item_ids|unhelpful_item_ids|ignored_item_ids",
                "msg": (
                    f"pack {pack_id} showed {len(bodied)} item(s) with an "
                    f"excerpt, {len(unjudged)} carry no verdict"
                ),
            }
        ],
        hints=[
            "every item shown with an excerpt takes one of the three verdicts",
            "ignored_item_ids is the honest answer for one you did not use",
            "items served as one-line pointers are not part of this requirement",
        ],
    )
    _raise_invalid_params(
        f"feedback on pack {pack_id} must judge every item it showed with an "
        f"excerpt — {len(unjudged)} of {len(bodied)} carry no verdict. Put each "
        "remaining id in helpful_item_ids, unhelpful_item_ids, or "
        "ignored_item_ids; 'ignored' is the honest answer for one you did not "
        "use. Items served as one-line pointers are not being asked about."
        + _id_list_clause("Ids still owed a verdict", unjudged),
        data={
            "pack_id": pack_id,
            "item_ids": unjudged[:_MAX_CITABLE_IDS_IN_ERROR],
            "bodied_item_count": len(bodied),
            "fields": [
                "helpful_item_ids",
                "unhelpful_item_ids",
                "ignored_item_ids",
            ],
        },
    )


@mcp.tool(auth=trellis_scope(SCOPE_MUTATE))
def record_feedback(
    trace_id: str = "",
    pack_id: str = "",
    success: bool | None = None,
    rating: float | None = None,
    notes: str | None = None,
    helpful_item_ids: list[str] | None = None,
    unhelpful_item_ids: list[str] | None = None,
    followed_advisory_ids: list[str] | None = None,
    ignored_item_ids: list[str] | None = None,
) -> str:
    """Record outcome feedback on a trace or context pack.

    Supply ``pack_id`` (preferred) to attribute feedback to a context
    pack returned by one of the ``get_*_context`` tools. The pack_id is
    shown in the response header of each pack and can be copied verbatim.

    Grade the pack with ``rating`` (0.0 to 1.0) whenever you can: a pack that
    contained one useful item out of six is not the same signal as one
    that nailed the task, and only graded feedback gives the fitness loops
    the variance they need. ``success`` alone still works and is treated
    as ``rating=1.0`` / ``0.0``.

    When citing specific elements:

    * ``helpful_item_ids`` — item_ids (shown in backticks in the pack)
      that actually helped the task succeed.
    * ``unhelpful_item_ids`` — items that were noise or misleading.
    * ``ignored_item_ids`` — items you read and did not use. Not a
      complaint: an item can be sound, on-topic and simply not what this
      task needed. It is the honest answer for most of a pack, and the
      one verdict you can give without inventing a judgement you do not
      hold.
    * ``followed_advisory_ids`` — advisory_ids (shown in backticks in the
      Advisories section) that you followed.

    Element-level signals are stored on the feedback event for the fitness
    loops (``trellis analyze apply-noise-tags`` and ``trellis analyze
    advisory-effectiveness``) to attribute outcomes more precisely.

    **Feedback naming a pack but citing no items joins to nothing.** The
    learning loop matches per-item rows from the pack against the ids you
    cite; a pack-level rating with no ids grades the delivery but teaches
    nothing about which memories earned their tokens. If the pack was a
    miss, that is not a reason to cite nothing — it is what
    ``unhelpful_item_ids`` is for, and it is the more valuable signal of
    the two. Deployments can require this (``TRELLIS_REQUIRE_PACK_ATTRIBUTION``);
    where they do, an uncited pack-targeted call is rejected and the
    rejection carries the ids the pack served so the retry is a choice
    among them. ``ignored_item_ids`` does **not** satisfy that
    requirement — it records non-use, which is real information, but it
    contributes no rows to the learning join, so a call citing only
    ignored ids is still rejected where attribution is required.

    A second, separate requirement (``TRELLIS_REQUIRE_BODIED_ATTRIBUTION``,
    off by default) asks for a verdict on every item the pack showed you
    with an *excerpt*, in any of the three item fields. Items served as
    one-line pointers are excluded: you were shown a label, not a body,
    so there is nothing to judge.

    ``trace_id`` is still accepted for trace-level feedback when no pack
    is involved — grading work that no pack informed is a legitimate,
    honest signal, and it is *not* what the requirement above is about.
    It carries no attribution because there is none to carry, and the
    health report counts it separately for exactly that reason.

    Args:
        trace_id: Trace ID for trace-level feedback (optional).
        pack_id: Pack ID for pack-level feedback (optional but preferred
            when feedback follows a context retrieval).
        success: Whether the task succeeded. Omit when passing ``rating``
            — it is then derived from the grade. Defaults to success only
            when neither is given.
        rating: Graded usefulness of the pack, 0.0 (useless) to 1.0
            (everything served was on point).
        notes: Optional notes about what worked or didn't.
        helpful_item_ids: IDs of pack items that were actually useful.
        unhelpful_item_ids: IDs of pack items that were noise.
        followed_advisory_ids: IDs of advisories the agent followed.
        ignored_item_ids: IDs of pack items you read and did not use.
            Distinct from ``unhelpful_item_ids``, which claims the item
            was noise or misleading.
    """
    has_trace = bool(trace_id and trace_id.strip())
    has_pack = bool(pack_id and pack_id.strip())
    if not has_trace and not has_pack:
        _record_boundary_rejection(
            tool="record_feedback",
            rejections=[
                {
                    "kind": "missing",
                    "loc": "trace_id|pack_id",
                    "msg": "neither target provided",
                }
            ],
        )
        _raise_invalid_params(
            "one of trace_id or pack_id must be provided",
            data={"fields": ["trace_id", "pack_id"]},
        )
    if rating is not None and not 0.0 <= rating <= 1.0:
        _record_boundary_rejection(
            tool="record_feedback",
            rejections=[
                {
                    "kind": "value",
                    "loc": "rating",
                    "msg": f"out of range: {rating}",
                }
            ],
        )
        unit_hint = rating_out_of_range_hint(rating)
        message = "rating must be between 0.0 and 1.0"
        if unit_hint is not None:
            message = f"{message}; {unit_hint}"
        _raise_invalid_params(
            message,
            data={"field": "rating", "value": rating},
        )

    registry = _get_registry()
    stores_dir = registry.stores_dir
    if stores_dir is None:
        _raise_internal(
            "stores_dir is not configured; cannot record feedback",
            data={"setting": "stores_dir"},
        )

    if has_pack:
        # Bodied coverage first: its message names the specific ids still
        # owed, so when both gates would fire the caller gets the more
        # informative refusal. The second gate's trigger is unchanged —
        # ``ignored_item_ids`` is deliberately absent from it, so it
        # stays the same predicate ``payload_is_attributed`` applies.
        _require_bodied_attribution(
            registry,
            pack_id.strip(),
            judged={
                *(helpful_item_ids or ()),
                *(unhelpful_item_ids or ()),
                *(ignored_item_ids or ()),
            },
        )
        if not (helpful_item_ids or unhelpful_item_ids or followed_advisory_ids):
            _require_pack_attribution(
                registry,
                pack_id.strip(),
                ignored_count=len(ignored_item_ids or ()),
            )

    # Shared with the REST pack-feedback route so the two agent-facing
    # surfaces agree on what identical inputs mean — including deriving
    # ``success`` from ``rating`` and leaving ``items_served`` empty
    # rather than fabricating it from the cited ids.
    feedback = PackFeedback.from_agent_signal(
        run_id=trace_id if has_trace else pack_id,
        success=success,
        rating=rating,
        helpful_item_ids=helpful_item_ids or (),
        unhelpful_item_ids=unhelpful_item_ids or (),
        followed_advisory_ids=followed_advisory_ids or (),
        ignored_item_ids=ignored_item_ids or (),
        pack_id=pack_id if has_pack else None,
        trace_id=trace_id if has_trace else None,
        notes=notes,
    )

    # One path writes both sinks: the durable pack_feedback.jsonl row and
    # the authoritative FEEDBACK_RECORDED event. The emit fails soft in
    # there, so a sink outage degrades to a file row that
    # ``trellis admin reconcile-feedback`` replays — it no longer raises
    # out of an agent-facing tool.
    result = record_pack_feedback(
        feedback,
        log_dir=feedback_log_dir(stores_dir),
        event_log=registry.operational.event_log,
        outcome_store=registry.operational.outcome_store,
        pack_id=pack_id if has_pack else None,
        source="mcp",
        entity_id=None if has_pack else trace_id,
        entity_type=None if has_pack else "trace",
    )

    target = f"pack: {pack_id}" if has_pack else f"trace: {trace_id}"
    status = "positive" if feedback.succeeded else "negative"
    graded = feedback.effective_rating
    message = f"Feedback recorded ({status}, rating {graded:.2f}) for {target}"
    if not result.event_log_in_sync:
        message += (
            " — event log unavailable, audit row kept;"
            " run `trellis admin reconcile-feedback` to replay it"
        )
    if has_pack:
        # A further line, never an edit to the first: the feedback above is
        # already recorded, and the note only tells the caller which of its
        # ids joined to nothing.
        note = _unserved_citation_note(
            registry,
            pack_id,
            cited_item_ids(
                {
                    "helpful_item_ids": list(helpful_item_ids or ()),
                    "unhelpful_item_ids": list(unhelpful_item_ids or ()),
                }
            ),
        )
        if note is not None:
            message += f"\n{note}"
    return message
