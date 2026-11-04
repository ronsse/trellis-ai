"""Seeds shared by the replay tests and the golden capture.

Imports only what ``origin/main`` already has, so the golden file can be
captured by running these builders against a ``git archive`` of ``main`` and
read back by a test on the branch. Keep it that way: a seed that reaches for a
branch-only symbol cannot be captured from the code it is meant to pin.
"""

from __future__ import annotations

import random
from typing import Any

from trellis.stores.base.event_log import EventType

AXES = ("keyword", "semantic", "graph")
FORMS = (None, None, "conversation", "note", "runbook")
ITEM_TYPES = ("document", "vector", "entity", "observation", "trace", "")


def _emit_pack(
    log: Any,
    pack_id: str,
    rows: list[dict[str, Any]],
    *,
    helpful: list[str],
    unhelpful: list[str],
    rejected: list[dict[str, Any]] | None = None,
) -> None:
    payload: dict[str, Any] = {"pack_id": pack_id, "injected_items": rows}
    if rejected:
        payload["rejected_items"] = rejected
    log.emit(
        EventType.PACK_ASSEMBLED,
        source="pack_builder",
        entity_id=pack_id,
        entity_type="pack",
        payload=payload,
    )
    log.emit(
        EventType.FEEDBACK_RECORDED,
        source="mcp",
        entity_id=pack_id,
        payload={
            "pack_id": pack_id,
            "helpful_item_ids": helpful,
            "unhelpful_item_ids": unhelpful,
            "rating": 0.5,
            "success": True,
        },
    )


def _row(
    item_id: str,
    axis: str,
    tokens: int,
    *,
    item_type: str = "document",
    form: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "item_id": item_id,
        "item_type": item_type,
        "rank": 1,
        "estimated_tokens": tokens,
        "strategy_source": axis,
    }
    if form is not None:
        row["document_form"] = form
    return row


def seed_class_prior_edges(log: Any, docs: Any) -> None:
    """Two paths ``replay_class_prior`` has that a plain seed never reaches.

    * a ``dedup`` rejection whose losing copy is on an axis outside the
      configured ones (the id is still served by that axis, so it is not
      removed), beside the cases that must NOT count that way: a ``dedup``
      loser inside the axes, and a rejection for another reason;
    * a chunk-scope parent lookup: chunks whose own row carries no form, whose
      parent holds one, holds none, or is gone.
    """
    docs.put("conv_p", "parent of a conversation", {"document_form": "conversation"})
    docs.put("conv_p#chunk-0", "first chunk", {"parent_doc_id": "conv_p"})
    docs.put("conv_p#chunk-1", "second chunk", {"parent_doc_id": "conv_p"})
    docs.put("note_p", "parent of a note", {"document_form": "note"})
    docs.put("note_p#chunk-0", "note chunk", {"parent_doc_id": "note_p"})
    docs.put("plain_p", "parent with no form", {})
    docs.put("plain_p#chunk-0", "plain chunk", {"parent_doc_id": "plain_p"})
    docs.put("gone_p#chunk-0", "orphan chunk", {"parent_doc_id": "gone_p"})

    _emit_pack(
        log,
        "pack-chunks",
        [
            _row("conv_p#chunk-0", "semantic", 30, item_type="vector"),
            _row("conv_p#chunk-1", "keyword", 20),
            _row("note_p#chunk-0", "semantic", 25, item_type="vector"),
            _row("plain_p#chunk-0", "semantic", 15, item_type="vector"),
            _row("gone_p#chunk-0", "semantic", 10, item_type="vector"),
            _row("conv_p#chunk-0x", "graph", 5, item_type="vector"),
        ],
        helpful=["conv_p#chunk-0", "plain_p#chunk-0"],
        unhelpful=["conv_p#chunk-1", "gone_p#chunk-0"],
    )
    _emit_pack(
        log,
        "pack-dedup",
        [
            _row("dup_other_axis", "keyword", 40, form="conversation"),
            _row("dup_same_axis", "keyword", 35, form="conversation"),
            _row("dup_budget", "keyword", 30, form="conversation"),
            _row("dup_semantic_win", "semantic", 22, form="conversation"),
            _row("plain_doc", "keyword", 12, form="note"),
        ],
        helpful=["dup_other_axis", "plain_doc"],
        unhelpful=["dup_same_axis", "dup_budget", "dup_semantic_win"],
        rejected=[
            # keyword won, a semantic copy lost: withholding keyword lets the
            # semantic copy through when only keyword is configured.
            {
                "item_id": "dup_other_axis",
                "reason": "dedup",
                "strategy_source": "semantic",
            },
            # the loser is on the configured axis too: nothing survives.
            {
                "item_id": "dup_same_axis",
                "reason": "dedup",
                "strategy_source": "keyword",
            },
            # not a dedup loss: says nothing about what withholding would do.
            {
                "item_id": "dup_budget",
                "reason": "budget",
                "strategy_source": "semantic",
            },
            {
                "item_id": "dup_semantic_win",
                "reason": "dedup",
                "strategy_source": "keyword",
            },
        ],
    )


# -- randomised packs, for the property and oracle tests ----------------------


def random_payload(rng: random.Random, pack_id: str) -> dict[str, Any]:
    """One ``PACK_ASSEMBLED`` payload with every shape the readers meet.

    Mixed item types, forms and axes; chunk ids; occasional duplicate rows,
    rejected ``dedup`` copies, a held-out flag and unknown token counts.
    """
    pool = [f"d{i}" for i in range(12)] + [f"e{i}" for i in range(8)]
    pool += [f"p{i}#chunk-{j}" for i in range(4) for j in range(3)]
    items = rng.sample(pool, rng.randint(0, 9))
    if items and rng.random() < 0.15:
        items.append(items[0])
    rows: list[dict[str, Any]] = []
    for rank, item_id in enumerate(items, 1):
        row: dict[str, Any] = {
            "item_id": item_id,
            "item_type": (
                "entity"
                if item_id.startswith("e") and rng.random() < 0.8
                else rng.choice(ITEM_TYPES)
            ),
            "rank": rank,
            "strategy_source": rng.choice(AXES),
        }
        draw = rng.random()
        row["estimated_tokens"] = (
            rng.randint(1, 400)
            if draw < 0.8
            else (rng.random() * 50 if draw < 0.9 else None)
        )
        form = rng.choice(FORMS)
        if form is not None:
            row["document_form"] = form
        rows.append(row)
    payload: dict[str, Any] = {"pack_id": pack_id, "injected_items": rows}
    rejected = [
        {
            "item_id": item_id,
            "reason": rng.choice(["dedup", "budget", "noise"]),
            "strategy_source": rng.choice(AXES),
        }
        for item_id in items
        if rng.random() < 0.3
    ]
    if rejected:
        payload["rejected_items"] = rejected
    if rng.random() < 0.1:
        payload["holdout"] = True
    return payload


def random_parent_docs(docs: Any, rng: random.Random) -> None:
    """Parents (some missing, some form-less) and chunks for ``random_payload``."""
    for i in range(4):
        if rng.random() < 0.8:
            docs.put(f"p{i}", "parent body", {"document_form": rng.choice(FORMS[2:])})
        for j in range(3):
            if rng.random() < 0.7:
                docs.put(
                    f"p{i}#chunk-{j}",
                    "chunk body words here",
                    {"parent_doc_id": f"p{i}"},
                )


# -- the golden arm set --------------------------------------------------------


def class_prior_edge_dump(log: Any, docs: Any) -> dict[str, Any]:
    """``replay_class_prior`` under four configs, as plain dicts.

    Imported lazily so this module loads on ``origin/main`` too. The configs
    reach the dedup-loser path (1, 4), the chunk-scope parent lookup (2) and
    its no-store fallback (3).
    """
    from trellis.retrieve.class_prior import ClassPriorConfig
    from trellis.retrieve.pack_replay import replay_class_prior

    def cfg(forms: set[str], axes: set[str], scope: str) -> ClassPriorConfig:
        return ClassPriorConfig(
            mode="withhold",
            forms=frozenset(forms),
            axes=frozenset(axes),
            scope=scope,
        )

    return {
        "keyword_conversation": replay_class_prior(
            log, config=cfg({"conversation"}, {"keyword"}, "parents")
        ).model_dump(),
        "chunks_with_store": replay_class_prior(
            log,
            config=cfg(
                {"conversation", "note"},
                {"semantic", "keyword"},
                "parents_and_chunks",
            ),
            document_store=docs,
        ).model_dump(),
        "chunks_without_store": replay_class_prior(
            log,
            config=cfg({"conversation"}, {"semantic"}, "parents_and_chunks"),
        ).model_dump(),
        "two_axes_conversation": replay_class_prior(
            log, config=cfg({"conversation"}, {"keyword", "semantic"}, "parents")
        ).model_dump(),
    }
