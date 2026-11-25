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

**Where.** The block lives at ``pack.metadata["nearby"]`` (flat and
sectioned packs alike, so REST and CLI JSON carry it) and, unchanged, as
``PACK_ASSEMBLED.nearby``. The markdown is rendered by
:func:`format_nearby_section`, which the core pack formatters
(:mod:`trellis.retrieve.formatters`) call after the items and before the
advisories, so every surface that renders a pack through them shows the same
text. The section is not charged to the item budget (the advisories'
doctrine); its own size is capped at :data:`MAX_SECTION_TOKENS`.

**Exclusions.** A public-only pack is never cut, so it never carries a
section. A held-out pack's metadata and telemetry are replaced wholesale, so
it carries neither the section nor any listed id.

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
#: Estimated-token cap for the whole rendered section (heading and footer
#: included). The first group is always listed.
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


def _first_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list | tuple):
        for entry in value:
            if isinstance(entry, str) and entry.strip():
                return entry.strip()
    return None


def group_label(row: dict[str, Any]) -> str:
    """The group a cut row belongs to: domain, else source system, else type."""
    domain = _first_str(row.get("domain"))
    if domain:
        return f"domain: {domain}"
    source = _first_str(row.get("source_system"))
    if source:
        return f"source: {source}"
    return f"type: {_first_str(row.get('item_type')) or 'item'}"


def group_cut_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group ``rows`` (cutoff ``cut_items``) by :func:`group_label`, uncapped.

    Pure and deterministic: largest group first, ties by label; rows keep
    their input order (the cutoff's) inside a group. A row without an
    ``item_id`` is skipped.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row.get("item_id"), str) or not row["item_id"]:
            continue
        groups.setdefault(group_label(row), []).append(row)
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    out: list[dict[str, Any]] = []
    for label, members in ordered:
        titles = [
            truncate_excerpt(" ".join(t.split()), TITLE_MAX_CHARS)
            for t in (m.get("title") for m in members)
            if isinstance(t, str) and t.strip()
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
    groups: list[dict[str, Any]], *, item_count: int, all_groups: int
) -> dict[str, Any]:
    listed = [i for g in groups for i in g["item_ids"]]
    return {
        "groups": groups,
        "item_count": item_count,
        "estimated_tokens": sum(g["estimated_tokens"] for g in groups),
        "listed_item_ids": listed,
        "groups_left_out": all_groups - len(groups),
        "items_left_out": item_count - len(listed),
        "section_tokens": 0,
    }


def build_nearby(
    cut_rows: list[dict[str, Any]] | None, *, mode: NearbyMode, pack_id: str
) -> dict[str, Any] | None:
    """The ``nearby`` block for a pack, or ``None`` when nothing renders.

    ``None`` when ``mode`` is off or ``cut_rows`` is empty. Groups are added
    in :func:`group_cut_items` order while the rendered section stays within
    :data:`MAX_SECTION_TOKENS` (the first group always), up to
    :data:`MAX_GROUPS`; what a cap left out is counted in
    ``groups_left_out`` / ``items_left_out`` and per group in
    ``ids_left_out``.
    """
    if mode != "on" or not cut_rows:
        return None
    grouped = group_cut_items(cut_rows)
    if not grouped:
        return None
    item_count = sum(g["count"] for g in grouped)
    kept: list[dict[str, Any]] = []
    block = _block(kept, item_count=item_count, all_groups=len(grouped))
    for group in grouped[:MAX_GROUPS]:
        trial = _block([*kept, group], item_count=item_count, all_groups=len(grouped))
        if kept and estimate_tokens(format_nearby_section(trial, pack_id)) > (
            MAX_SECTION_TOKENS
        ):
            break
        kept.append(group)
        block = trial
    block["section_tokens"] = estimate_tokens(format_nearby_section(block, pack_id))
    return block


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
    "MAX_GROUPS",
    "MAX_IDS_PER_GROUP",
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
    "nearby_listed_ids",
    "resolve_nearby_mode",
]
