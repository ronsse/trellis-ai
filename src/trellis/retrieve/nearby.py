"""A default-off "Nearby" section: what the relevance cutoff removed, one call away.

**Why.** :mod:`trellis.retrieve.relevance_cutoff` in mode ``on`` withholds
semantic-only candidates below a similarity floor. Cutting by score drops
items an agent would have graded helpful, so a cut should demote rather than
hide: the pack names what else is nearby, grouped, and says how to open a
group with one ``get_items`` call that joins the fetch to the pack.

**When it renders.** Only with ``nearby_section`` on
(``TRELLIS_NEARBY_SECTION`` > settings ``nearby_section`` > ``off``) *and*
the cutoff in mode ``on`` having cut something, i.e. when
``Pack.metadata["relevance_cutoff"]["cut_items"]`` is non-empty
(:func:`~trellis.retrieve.relevance_cutoff.pack_metadata_for`). Otherwise
:func:`build_nearby` returns ``None`` and no key is added to the pack, its
markdown or ``PACK_ASSEMBLED``. Off is byte-identical to a build without the
feature.

**What it lists.** Only what the pack could have served: a cut row that the
structural filter, the meta-activity filter or session dedup would have
rejected anyway is not listed (the builder passes those ids as
``unservable``), nor is an id that cannot be shown as an inline code span.
Each such row is counted by reason in the block's ``not_listed``. The
cutoff's own ``cut_items`` and ``rejected_items`` are untouched.

**Where.** The block lives at ``pack.metadata["nearby"]`` (flat and
sectioned packs alike) with the rendered text at ``markdown``, written once
by :func:`format_nearby_section`; ``PACK_ASSEMBLED.nearby`` records the same
block without ``markdown`` (:func:`nearby_event_block`). Every formatter, the
core ones (:mod:`trellis.retrieve.formatters`) and the SDK's
(``trellis_sdk._format``), prints that string through
:func:`trellis_wire.nearby.nearby_markdown`, after the items and before the
advisories, so no surface re-derives it. The REST pack responses carry no
``metadata``, so an SDK caller over REST gets no section today. The section is
not charged to ``max_tokens`` (the advisories' doctrine); its own size is
capped at :data:`MAX_SECTION_TOKENS`, a guarantee because labels, titles and
listed ids are each length-capped and the first group is trimmed to fit.

**Exclusions.** A public-only pack is never cut, so it never carries a
section. A held-out pack's metadata and telemetry are replaced wholesale, so
it carries neither the section nor any listed id, and a served pack with no
items drops the block while the holdout is live
(``PackBuilder._blind_advisories_for_empty_pack``) so it cannot be told apart
from a held-out one.

**Joining a later fetch.** ``listed_item_ids`` names every id the section
showed. A ``PACK_ITEMS_FETCHED`` naming the pack and one of those ids joins
to the pack as a nearby fetch: the feedback prior counts it as fetch evidence
(:func:`trellis.retrieve.feedback_prior.build_ledger`) and ``analyze value``
reports it apart from served fetches (``fetch_joined_nearby``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Literal

import structlog

from trellis.core.hashing import estimate_tokens
from trellis.retrieve.excerpts import truncate_excerpt
from trellis_wire.nearby import NEARBY_MARKDOWN_KEY

logger = structlog.get_logger(__name__)

NearbyMode = Literal["off", "on"]

NEARBY_SECTION_ENV = "TRELLIS_NEARBY_SECTION"
NEARBY_SECTION_SETTING = "nearby_section"
#: ``PACK_ASSEMBLED`` and ``Pack.metadata`` key.
NEARBY_KEY = "nearby"

#: At most this many groups are listed.
MAX_GROUPS = 5
#: At most this many ids are listed per group.
MAX_IDS_PER_GROUP = 5
#: At most this many titles are shown per group.
MAX_TITLES_PER_GROUP = 2
#: A shown title is cut to this many characters.
TITLE_MAX_CHARS = 60
#: A group label (``domain: ...``) is cut to this many characters.
LABEL_MAX_CHARS = 60
#: An id longer than this is not listed (counted as ``unlistable_id``).
MAX_ID_CHARS = 128
#: Estimated-token cap for the whole rendered section (heading and footer
#: included). At least one id of the first group is always listed, trimmed
#: until the section fits; see :func:`build_nearby`.
MAX_SECTION_TOKENS = 200

_VALID_MODES: frozenset[str] = frozenset({"off", "on"})
_HEADING = "## Nearby (below the relevance cutoff, not shown)"


def resolve_nearby_mode(settings: dict[str, Any] | None = None) -> NearbyMode:
    """Env > settings > ``off``; an unrecognised value degrades to ``off``."""
    raw = os.environ.get(NEARBY_SECTION_ENV, "").strip().lower()
    if raw:
        if raw in _VALID_MODES:
            return raw  # type: ignore[return-value]
        logger.warning(
            "nearby_section_env_unrecognised", var=NEARBY_SECTION_ENV, effective="off"
        )
        return "off"
    stored = (settings or {}).get(NEARBY_SECTION_SETTING)
    value = stored.strip().lower() if isinstance(stored, str) else ""
    if value in _VALID_MODES:
        return value  # type: ignore[return-value]
    if value:
        logger.warning(
            "nearby_section_settings_unrecognised", setting=NEARBY_SECTION_SETTING
        )
    return "off"


def _clean(text: str) -> str:
    """``text`` on one line of single spaces, with every backtick removed.

    A label or title is shown inside markdown next to inline code spans; a
    newline would start a new block (``## heading``) and a backtick would
    break the id spans that follow.
    """
    return " ".join(text.replace("`", "").split())


def _first_str(value: Any) -> str | None:
    if isinstance(value, str):
        return _clean(value) or None
    if isinstance(value, list | tuple):
        for entry in value:
            if isinstance(entry, str) and _clean(entry):
                return _clean(entry)
    return None


def group_label(row: dict[str, Any]) -> str:
    """The group a cut row belongs to: domain, else source system, else type.

    One line, no backticks, at most :data:`LABEL_MAX_CHARS` characters.
    """
    domain = _first_str(row.get("domain"))
    if domain:
        label = f"domain: {domain}"
    else:
        source = _first_str(row.get("source_system"))
        if source:
            label = f"source: {source}"
        else:
            label = f"type: {_first_str(row.get('item_type')) or 'item'}"
    return truncate_excerpt(label, LABEL_MAX_CHARS)


def listable_id(item_id: Any) -> bool:
    """Whether ``item_id`` can be shown as an inline code span.

    A backtick would end the span early and a newline or other
    non-printable character would break the line, so such an id is not
    listed; nor is one longer than :data:`MAX_ID_CHARS`, which keeps the
    section's token cap a guarantee.
    """
    return (
        isinstance(item_id, str)
        and bool(item_id)
        and len(item_id) <= MAX_ID_CHARS
        and "`" not in item_id
        and item_id.isprintable()
    )


def group_cut_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group ``rows`` (cutoff ``cut_items``) by :func:`group_label`, uncapped.

    Pure and deterministic: largest group first, ties by label; rows keep
    their input order (the cutoff's) inside a group. A row whose
    ``item_id`` is missing or not :func:`listable_id` is skipped.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not listable_id(row.get("item_id")):
            continue
        groups.setdefault(group_label(row), []).append(row)
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    out: list[dict[str, Any]] = []
    for label, members in ordered:
        titles = [
            truncate_excerpt(_clean(t), TITLE_MAX_CHARS)
            for t in (m.get("title") for m in members)
            if isinstance(t, str) and _clean(t)
        ][:MAX_TITLES_PER_GROUP]
        ids = [m["item_id"] for m in members]
        out.append(
            {
                "label": label,
                "count": len(members),
                "estimated_tokens": sum(
                    int(m.get("estimated_tokens") or 0) for m in members
                ),
                "titles": titles,
                "item_ids": ids[:MAX_IDS_PER_GROUP],
                "ids_left_out": max(0, len(ids) - MAX_IDS_PER_GROUP),
            }
        )
    return out


def _block(
    groups: list[dict[str, Any]],
    *,
    item_count: int,
    all_groups: int,
    not_listed: dict[str, int],
) -> dict[str, Any]:
    listed = [i for g in groups for i in g["item_ids"]]
    return {
        "groups": groups,
        "item_count": item_count,
        "estimated_tokens": sum(g["estimated_tokens"] for g in groups),
        "listed_item_ids": listed,
        "groups_left_out": all_groups - len(groups),
        "items_left_out": item_count - len(listed),
        "not_listed": dict(not_listed),
        "section_tokens": 0,
    }


def _fits(block: dict[str, Any], pack_id: str) -> bool:
    return estimate_tokens(format_nearby_section(block, pack_id)) <= (
        MAX_SECTION_TOKENS
    )


def _trim_first_group(
    group: dict[str, Any], *, rebuild: Any, pack_id: str
) -> dict[str, Any]:
    """Drop ``group``'s trailing ids (then titles) until its block fits.

    At least one id stays. ``rebuild`` makes the trial block for a group.
    """
    trimmed = {**group, "item_ids": list(group["item_ids"])}
    while not _fits(rebuild(trimmed), pack_id) and len(trimmed["item_ids"]) > 1:
        trimmed["item_ids"].pop()
        trimmed["ids_left_out"] = trimmed["ids_left_out"] + 1
    while not _fits(rebuild(trimmed), pack_id) and trimmed["titles"]:
        trimmed["titles"] = trimmed["titles"][:-1]
    return trimmed


def build_nearby(
    cut_rows: list[dict[str, Any]] | None,
    *,
    mode: NearbyMode,
    pack_id: str,
    unservable: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """The ``nearby`` block for a pack, or ``None`` when nothing renders.

    ``None`` when ``mode`` is off, ``cut_rows`` is empty, or no row is left
    to list. ``unservable`` maps a cut id to the rejection reason a later
    collect-seam gate (structural, meta-activity, session dedup) would have
    given it: the pack could not have served it, so it is not listed. Those
    rows, and rows whose id is not :func:`listable_id`
    (``"unlistable_id"``), are counted per reason in ``not_listed`` and are
    not part of ``item_count``.

    Groups are added in :func:`group_cut_items` order while the rendered
    section stays within :data:`MAX_SECTION_TOKENS`, up to
    :data:`MAX_GROUPS`. When the first group alone is over the cap its
    trailing ids, then its titles, are dropped until it fits; at least one
    id is always listed, and with labels, titles and ids each length-capped
    the section then fits. What a cap left out is counted in
    ``groups_left_out`` / ``items_left_out`` and per group in
    ``ids_left_out``. The rendered text is at ``markdown``; the
    ``PACK_ASSEMBLED`` copy leaves that key out.
    """
    if mode != "on" or not cut_rows:
        return None
    skip = unservable or {}
    not_listed: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for row in cut_rows:
        item_id = row.get("item_id")
        reason = skip.get(item_id) if isinstance(item_id, str) else None
        if reason is None and not listable_id(item_id):
            reason = "unlistable_id"
        if reason is not None:
            not_listed[reason] = not_listed.get(reason, 0) + 1
            continue
        rows.append(row)
    grouped = group_cut_items(rows)
    if not grouped:
        return None
    item_count = sum(g["count"] for g in grouped)

    def rebuild(groups: list[dict[str, Any]]) -> dict[str, Any]:
        return _block(
            groups,
            item_count=item_count,
            all_groups=len(grouped),
            not_listed=not_listed,
        )

    first = _trim_first_group(
        grouped[0], rebuild=lambda g: rebuild([g]), pack_id=pack_id
    )
    kept: list[dict[str, Any]] = [first]
    block = rebuild(kept)
    for group in grouped[1:MAX_GROUPS]:
        trial = rebuild([*kept, group])
        if not _fits(trial, pack_id):
            break
        kept.append(group)
        block = trial
    text = format_nearby_section(block, pack_id)
    block["section_tokens"] = estimate_tokens(text)
    block[NEARBY_MARKDOWN_KEY] = text
    return block


def nearby_event_block(block: Mapping[str, Any]) -> dict[str, Any]:
    """``block`` as ``PACK_ASSEMBLED.nearby`` records it: without the text."""
    return {k: v for k, v in block.items() if k != NEARBY_MARKDOWN_KEY}


def nearby_listed_ids(payload: Mapping[str, Any]) -> set[str]:
    """The ids a ``PACK_ASSEMBLED`` payload's ``nearby`` block listed."""
    block = payload.get(NEARBY_KEY)
    ids = block.get("listed_item_ids") if isinstance(block, Mapping) else None
    if not isinstance(ids, list):
        return set()
    return {i for i in ids if isinstance(i, str) and i}


def format_nearby_section(block: dict[str, Any] | None, pack_id: str | None) -> str:
    """The markdown for ``block``, or ``""`` when there is none.

    Reads only the block's own keys, so the text is a function of what
    ``PACK_ASSEMBLED.nearby`` records.
    """
    if not isinstance(block, dict) or not block.get("groups"):
        return ""
    lines = [_HEADING]
    for group in block["groups"]:
        ids = " ".join(f"`{i}`" for i in group["item_ids"])
        more = group.get("ids_left_out") or 0
        if more:
            ids += f" (+{more} more)"
        titles = "; ".join(f'"{t}"' for t in group.get("titles") or [])
        noun = "item" if group["count"] == 1 else "items"
        line = (
            f"- **{group['label']}**: {group['count']} {noun}, "
            f"~{group['estimated_tokens']} tok"
        )
        if titles:
            line += f": {titles}"
        lines.append(f"{line}. {ids}")
    groups_out = block.get("groups_left_out") or 0
    if groups_out:
        lines.append(
            f"*[{groups_out} more groups left out; "
            f"{block.get('items_left_out', 0)} items in all not listed]*"
        )
    elif block.get("items_left_out"):
        lines.append(f"*[{block['items_left_out']} more items not listed]*")
    call = "get_items(item_ids=[<a group's ids>]"
    call += f', pack_id="{pack_id}")' if pack_id else ")"
    lines.append(f"*Open a group with one call: `{call}`.*")
    return "\n".join(lines)


__all__ = [
    "LABEL_MAX_CHARS",
    "MAX_GROUPS",
    "MAX_IDS_PER_GROUP",
    "MAX_ID_CHARS",
    "MAX_SECTION_TOKENS",
    "MAX_TITLES_PER_GROUP",
    "NEARBY_KEY",
    "NEARBY_SECTION_ENV",
    "NEARBY_SECTION_SETTING",
    "TITLE_MAX_CHARS",
    "NearbyMode",
    "build_nearby",
    "format_nearby_section",
    "group_cut_items",
    "group_label",
    "listable_id",
    "nearby_event_block",
    "nearby_listed_ids",
    "resolve_nearby_mode",
]
