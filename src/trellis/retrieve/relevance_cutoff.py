"""Native per-strategy scores, and a default-off relevance cutoff that reads them.

**Why.** A pack fills to its item and token budget however weak its tail is:
nothing sets a minimum relevance. A rank-position cut is a poor substitute,
because helpful items are spread through the ranks. A real cutoff needs each
candidate's *native* score from the strategy that found it, and the RRF
reranker (:mod:`trellis.retrieve.rerankers.rrf`) replaces ``relevance_score``
and ``score_breakdown`` with rank-derived values, so without this module the
native score is gone before ``PACK_ASSEMBLED`` is written.

**Part 1: native scores survive fusion (always on, telemetry only).** At the
collect seam, each candidate's ``score_breakdown`` gains
``native_<strategy>`` (:func:`stamp_native_scores`); exact-id dedup merges the
keys of every copy into the winner (:func:`merge_native_scores`), so an item
two strategies returned carries both; both rerankers carry the keys through
(:func:`native_scores_of`). The served item's ``score_breakdown`` reaches
``PACK_ASSEMBLED.injected_items[]``, and a rejected candidate's keys reach
``rejected_items[].native_scores``
(:meth:`~trellis.schemas.pack.RejectedItem.from_pack_item`). Nothing reads
them to score, order, render or select, so a pack's items, order and text are
unchanged (``tests/unit/retrieve/test_relevance_cutoff.py``). A public-only
caller never receives them (:mod:`trellis.retrieve.public_pack`).

What the native score is, per built-in strategy:

* ``native_semantic`` — the vector store's own ``score`` for the row, before
  importance weighting and recency decay. **Higher is more similar.** It is a
  cosine *similarity*, not a distance, but its range depends on the backend:
  SQLite and pgvector return ``cos(q, v)`` in ``[-1, 1]`` (pgvector as
  ``1 - (embedding <=> q)``), ArcadeDB returns ``1 - cosine distance`` (the
  same value), and Neo4j's vector index returns its normalised cosine score
  ``(1 + cos) / 2`` in ``[0, 1]``. An absolute floor is therefore not portable
  across backends; the ratio floor is less sensitive to that.
* ``native_keyword`` — the document store's full-text rank as the strategy
  reads it (``abs(rank)``, a BM25-style score), before importance and recency.
  Higher is better, but it is a function of corpus statistics and query
  length: not a similarity, and not comparable across queries.
* ``native_graph`` — the graph strategy's own ``relevance_score``: a position
  score (``1 - i * step``) times domain, curated, importance and description
  boosts and recency decay. It is an ordering device, not a similarity.
* Any other strategy — its ``relevance_score`` at the collect seam, stamped by
  :func:`stamp_native_scores` when the strategy did not stamp its own.

**Part 2: the cutoff gate (default off).** :func:`apply_relevance_cutoff` runs
at the collect seam after noise, archived, supersession and the class prior,
on the flat and sectioned paths, over the merged pool before dedup. Floors
exist for the **semantic axis only**: ``semantic_min`` (absolute floor on
``native_semantic``) and ``semantic_ratio`` (fraction of the best
``native_semantic`` among this build's candidates); the effective floor is the
larger of those configured. Keyword and graph have no floor because their
native scores are not comparable similarities (above). An item is cut only
when every strategy that returned it is an axis with a configured floor and it
fails that floor, so an item keyword or graph also returned is kept. The gate
never leaves fewer than :data:`MIN_KEEP` distinct candidates: it restores the
best-scoring cut items first.

Modes (``TRELLIS_RELEVANCE_CUTOFF_MODE`` > settings ``relevance_cutoff_mode``
> ``off``):

* ``off`` — returns the input list itself; no telemetry block, no key.
* ``shadow`` — cuts nothing; ``PACK_ASSEMBLED.relevance_cutoff`` records what
  ``on`` would have cut (count, tokens, ids and titles).
* ``on`` — each cut copy becomes a :class:`~trellis.schemas.pack.RejectedItem`
  under :data:`RELEVANCE_CUTOFF_REJECTION_REASON`, priced in
  ``estimated_tokens``, and the pack's withheld line counts it.

``on`` or ``shadow`` with neither threshold set is a no-op whose block reads
``status: "unconfigured"``. A held-out pack stays blind, as with the class
prior: the gate runs before the draw and only its block rides the row.

The cut items are kept for a later "nearby" section: ``on`` mode leaves
``Pack.metadata["relevance_cutoff"]["cut_items"]`` (id, title, tokens, native
semantic score) on the pack it serves, which nothing renders yet.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from trellis.schemas.pack import NATIVE_SCORE_KEY_PREFIX, PackItem, RejectedItem

logger = structlog.get_logger(__name__)

RelevanceCutoffMode = Literal["off", "shadow", "on"]

NATIVE_SCORE_PREFIX = NATIVE_SCORE_KEY_PREFIX
SEMANTIC_AXIS = "semantic"

RELEVANCE_CUTOFF_REJECTION_REASON = "below_relevance_cutoff"
RELEVANCE_CUTOFF_MODE_ENV = "TRELLIS_RELEVANCE_CUTOFF_MODE"
RELEVANCE_CUTOFF_SEMANTIC_MIN_ENV = "TRELLIS_RELEVANCE_CUTOFF_SEMANTIC_MIN"
RELEVANCE_CUTOFF_SEMANTIC_RATIO_ENV = "TRELLIS_RELEVANCE_CUTOFF_SEMANTIC_RATIO"
RELEVANCE_CUTOFF_MODE_SETTING = "relevance_cutoff_mode"

#: Never cut below this many distinct surviving candidates.
MIN_KEEP = 3

_VALID_MODES: frozenset[str] = frozenset({"off", "shadow", "on"})


def native_score_key(strategy_name: str) -> str:
    """The ``score_breakdown`` key holding ``strategy_name``'s native score."""
    return f"{NATIVE_SCORE_PREFIX}{strategy_name}"


def native_scores_of(item: PackItem) -> dict[str, float]:
    """``item``'s ``native_*`` breakdown keys, verbatim (key -> score)."""
    return {
        k: v
        for k, v in item.score_breakdown.items()
        if k.startswith(NATIVE_SCORE_PREFIX)
    }


def stamp_native_scores(items: list[PackItem], strategy_name: str) -> list[PackItem]:
    """Record ``relevance_score`` as ``native_<strategy_name>`` where absent.

    A strategy that stamps its own (the built-in keyword and semantic axes
    record the store's score before importance and recency) keeps it; the
    item object is returned as is. Otherwise the copy carries the strategy's
    collect-seam ``relevance_score``. Items are never mutated in place.
    """
    key = native_score_key(strategy_name)
    out: list[PackItem] = []
    for item in items:
        if key in item.score_breakdown:
            out.append(item)
            continue
        out.append(
            item.model_copy(
                update={
                    "score_breakdown": {
                        **item.score_breakdown,
                        key: float(item.relevance_score),
                    }
                }
            )
        )
    return out


def merge_native_scores(winner: PackItem, loser: PackItem) -> PackItem:
    """``winner`` carrying ``loser``'s native keys too (winner's value wins)."""
    extra = {
        k: v
        for k, v in native_scores_of(loser).items()
        if k not in winner.score_breakdown
    }
    if not extra:
        return winner
    return winner.model_copy(
        update={"score_breakdown": {**winner.score_breakdown, **extra}}
    )


@dataclass(frozen=True)
class RelevanceCutoffConfig:
    """Config for :func:`apply_relevance_cutoff`. The default is off."""

    mode: RelevanceCutoffMode = "off"
    semantic_min: float | None = None
    semantic_ratio: float | None = None

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            msg = f"mode must be one of {sorted(_VALID_MODES)}; got {self.mode!r}"
            raise ValueError(msg)
        if self.semantic_ratio is not None and not 0.0 <= self.semantic_ratio <= 1.0:
            msg = f"semantic_ratio must be in [0, 1]; got {self.semantic_ratio!r}"
            raise ValueError(msg)

    @property
    def configured(self) -> bool:
        """Whether any floor is set."""
        return self.semantic_min is not None or self.semantic_ratio is not None


OFF = RelevanceCutoffConfig()


def _resolve_mode(settings: dict[str, Any] | None) -> RelevanceCutoffMode:
    raw = os.environ.get(RELEVANCE_CUTOFF_MODE_ENV, "").strip().lower()
    if raw:
        if raw in _VALID_MODES:
            return raw  # type: ignore[return-value]
        logger.warning(
            "relevance_cutoff_mode_env_unrecognised",
            var=RELEVANCE_CUTOFF_MODE_ENV,
            effective="off",
        )
        return "off"
    stored = (settings or {}).get(RELEVANCE_CUTOFF_MODE_SETTING)
    value = stored.strip().lower() if isinstance(stored, str) else ""
    if value in _VALID_MODES:
        return value  # type: ignore[return-value]
    if value:
        logger.warning(
            "relevance_cutoff_mode_settings_unrecognised",
            setting=RELEVANCE_CUTOFF_MODE_SETTING,
        )
    return "off"


def _resolve_float(
    var: str, *, low: float | None = None, high: float | None = None
) -> float | None:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if (
        not math.isfinite(value)
        or (low is not None and value < low)
        or (high is not None and value > high)
    ):
        # Name the variable, never the value it held.
        logger.warning("relevance_cutoff_threshold_env_unrecognised", var=var)
        return None
    return value


def resolve_relevance_cutoff_config(
    settings: dict[str, Any] | None = None,
) -> RelevanceCutoffConfig:
    """Resolve env > settings > default for ``mode``; thresholds are env-only.

    An unrecognised mode degrades to ``off`` and an unparseable or
    out-of-range threshold to unset, each with a warning naming the variable.
    """
    return RelevanceCutoffConfig(
        mode=_resolve_mode(settings),
        semantic_min=_resolve_float(RELEVANCE_CUTOFF_SEMANTIC_MIN_ENV),
        semantic_ratio=_resolve_float(
            RELEVANCE_CUTOFF_SEMANTIC_RATIO_ENV, low=0.0, high=1.0
        ),
    )


@dataclass
class RelevanceCutoffResult:
    """What :func:`apply_relevance_cutoff` decided.

    ``kept`` is the pool to continue with (the input list itself unless
    ``on`` cut something); ``rejected`` holds one row per cut copy (``on``
    only); ``cut`` is one item per cut id, served (shadow) or not (on);
    ``telemetry`` is the ``PACK_ASSEMBLED.relevance_cutoff`` block, ``None``
    when off.
    """

    kept: list[PackItem]
    rejected: list[RejectedItem] = field(default_factory=list)
    cut: list[PackItem] = field(default_factory=list)
    telemetry: dict[str, Any] | None = None


def _floor(config: RelevanceCutoffConfig, best: float | None) -> float | None:
    floors: list[float] = []
    if config.semantic_min is not None:
        floors.append(config.semantic_min)
    if config.semantic_ratio is not None and best is not None:
        floors.append(config.semantic_ratio * best)
    return max(floors) if floors else None


def cut_item_row(
    item: PackItem, tokens: int, title_of: Callable[[PackItem], str | None]
) -> dict[str, Any]:
    """The per-cut-item record a later "nearby" section would read."""
    return {
        "item_id": item.item_id,
        "title": title_of(item),
        "estimated_tokens": tokens,
        "native_semantic": item.score_breakdown.get(native_score_key(SEMANTIC_AXIS)),
    }


def _default_title(item: PackItem) -> str | None:
    meta = item.metadata or {}
    for key in ("title", "capture_title", "name"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def apply_relevance_cutoff(  # noqa: PLR0912
    items: list[PackItem],
    *,
    config: RelevanceCutoffConfig,
    price: Callable[[PackItem], int],
    title_of: Callable[[PackItem], str | None] = _default_title,
) -> RelevanceCutoffResult:
    """Apply ``config`` to the merged pool ``items`` (one copy per strategy).

    Off returns ``items`` itself and no telemetry. ``items`` is never
    mutated. ``price`` gives an item's token estimate for the record.
    """
    if config.mode == "off":
        return RelevanceCutoffResult(kept=items)

    sem_key = native_score_key(SEMANTIC_AXIS)
    block: dict[str, Any] = {
        "mode": config.mode,
        "semantic_min": config.semantic_min,
        "semantic_ratio": config.semantic_ratio,
    }
    if not config.configured:
        block["status"] = "unconfigured"
        return RelevanceCutoffResult(kept=items, telemetry=block)

    # Every native key any copy of an id carries: the strategies that
    # returned it. Dedup has not run, so copies are grouped here.
    natives: dict[str, dict[str, float]] = {}
    order: list[str] = []
    for item in items:
        if item.item_id not in natives:
            natives[item.item_id] = {}
            order.append(item.item_id)
        natives[item.item_id].update(native_scores_of(item))

    sem_scores = [n[sem_key] for n in natives.values() if sem_key in n]
    best = max(sem_scores) if sem_scores else None
    threshold = _floor(config, best)

    failing: list[str] = []
    if threshold is not None:
        for item_id in order:
            keys = natives[item_id]
            # Cut only when the semantic axis alone returned it: any other
            # axis has no floor, so an item it also returned is kept.
            if keys and set(keys) == {sem_key} and keys[sem_key] < threshold:
                failing.append(item_id)

    # min_keep: restore the best-scoring cut ids until MIN_KEEP survive.
    survivors = len(order) - len(failing)
    restored: list[str] = []
    if survivors < MIN_KEEP and failing:
        need = min(MIN_KEEP - survivors, len(failing))
        by_score = sorted(failing, key=lambda i: natives[i][sem_key], reverse=True)
        restored = by_score[:need]
    cut_ids = [i for i in failing if i not in set(restored)]
    cut_set = set(cut_ids)

    first_copy: dict[str, PackItem] = {}
    for item in items:
        if item.item_id in cut_set:
            first_copy.setdefault(item.item_id, item)
    cut = [first_copy[i] for i in cut_ids]
    tokens = {i: price(first_copy[i]) for i in cut_ids}

    block.update(
        {
            "status": "applied",
            "best_semantic": best,
            "threshold": threshold,
            "candidates": len(order),
            "cut_count": len(cut_ids),
            "cut_tokens": sum(tokens.values()),
            "kept_by_min_keep": len(restored),
            "cut_items": [cut_item_row(it, tokens[it.item_id], title_of) for it in cut],
        }
    )
    if config.mode == "shadow" or not cut_ids:
        return RelevanceCutoffResult(kept=items, cut=cut, telemetry=block)

    kept: list[PackItem] = []
    rejected: list[RejectedItem] = []
    for item in items:
        if item.item_id in cut_set:
            rejected.append(
                RejectedItem.from_pack_item(
                    item,
                    RELEVANCE_CUTOFF_REJECTION_REASON,
                    strategy_source=SEMANTIC_AXIS,
                    estimated_tokens=tokens[item.item_id],
                )
            )
        else:
            kept.append(item)
    return RelevanceCutoffResult(kept=kept, rejected=rejected, cut=cut, telemetry=block)


def pack_metadata_for(result: RelevanceCutoffResult) -> dict[str, Any]:
    """``Pack.metadata`` entry an ``on`` build leaves for a later renderer.

    Empty unless ``on`` cut something, so a default build's pack is unchanged.
    """
    block = result.telemetry
    if not block or block.get("mode") != "on" or not result.rejected:
        return {}
    return {"relevance_cutoff": {"cut_items": list(block["cut_items"])}}


__all__ = [
    "MIN_KEEP",
    "NATIVE_SCORE_PREFIX",
    "OFF",
    "RELEVANCE_CUTOFF_MODE_ENV",
    "RELEVANCE_CUTOFF_MODE_SETTING",
    "RELEVANCE_CUTOFF_REJECTION_REASON",
    "RELEVANCE_CUTOFF_SEMANTIC_MIN_ENV",
    "RELEVANCE_CUTOFF_SEMANTIC_RATIO_ENV",
    "SEMANTIC_AXIS",
    "RelevanceCutoffConfig",
    "RelevanceCutoffMode",
    "RelevanceCutoffResult",
    "apply_relevance_cutoff",
    "cut_item_row",
    "merge_native_scores",
    "native_score_key",
    "native_scores_of",
    "pack_metadata_for",
    "resolve_relevance_cutoff_config",
    "stamp_native_scores",
]
