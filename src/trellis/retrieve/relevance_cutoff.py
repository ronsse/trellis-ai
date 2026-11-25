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
  ``1 - (embedding <=> q)``), ArcadeDB with cosine similarity returns
  ``1 - cosine distance`` (the same value), ArcadeDB with any other
  similarity function returns ``-distance``, which is always ``<= 0``, and
  Neo4j's vector index returns its normalised cosine score ``(1 + cos) / 2``
  in ``[0, 1]``. An absolute floor is therefore not portable across
  backends; the ratio floor is less sensitive to that, but it is only
  meaningful when the best score is positive (below).
  A non-finite store score (NaN, inf) is never recorded: the key is omitted
  (:func:`native_breakdown`), because Postgres JSONB rejects NaN.
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
larger of those configured. The ratio floor applies only when the best
score is finite and ``> 0``: at or below zero, ``ratio * best`` sits *above*
``best`` and would cut every semantic-only item, the best included, so the
ratio is skipped and the block records ``ratio_skipped: "non_positive_best"``
(or ``"no_semantic_score"`` when no candidate has one). The ratio floor is
therefore discontinuous at ``best = 0``: skipped at exactly zero, applied in
full for any positive ``best``, however small. ``best`` is taken
over finite scores only, and an item with no finite semantic score is never
cut. Keyword and graph have no floor because their
native scores are not comparable similarities (above). An item is cut only
when every strategy that returned it is an axis with a configured floor and it
fails that floor, so an item keyword or graph also returned is kept. A
strategy counts as having returned an item when a copy carries its native key
*or* names it as ``strategy_source`` / ``metadata["source_strategy"]``, so an
item another axis returned with a non-finite score (no native key recorded)
is still kept. The gate
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
prior: the gate runs before the draw, and the held-out row carries only the
gate's configuration (:func:`holdout_telemetry`: mode and thresholds), never
its outcome or any item id.

**Public-only packs are not gated.** ``PackBuilder._build_public`` and
``_collect_public_pool`` do not call :func:`apply_relevance_cutoff`, so a
public-only build is uncut under every mode and its event carries no
``relevance_cutoff`` block (``tests/unit/retrieve/test_relevance_cutoff.py``).

The cut items are kept for the "nearby" section: ``on`` mode leaves
``Pack.metadata["relevance_cutoff"]["cut_items"]`` (id, title, tokens, native
semantic score, item type, domain, source system) on the pack it serves,
which :mod:`trellis.retrieve.nearby` renders when its own flag is on.

**Evidence exemption (default off).** The cutoff runs before ranking and the
feedback prior (:mod:`trellis.retrieve.feedback_prior`) adjusts rank after
it, so without this an item agents fetched or graded helpful is cut every
time and the evidence can never bring it back.
``relevance_cutoff_evidence_exempt`` (``TRELLIS_RELEVANCE_CUTOFF_EVIDENCE_EXEMPT``
> settings > ``off``) set to ``on``, with the cutoff ``on`` or ``shadow``,
keeps an item the floor failed when :func:`evidence_exempts` holds for its
row in the feedback prior's own ledger (the same table, the same held-out
exclusion, read whatever ``TRELLIS_FEEDBACK_PRIOR_MODE`` is). The ledger is
read only when some item failed the floor. Order, stated because it matters:

1. the floor picks the failing ids;
2. failing ids with positive evidence are exempted and count as kept;
3. :data:`MIN_KEEP` then restores the best-scoring of the *remaining*
   failing ids until enough survive, exempted ids included in the count.

An exempted id is not in ``cut_items``, not rejected, and so never listed by
the nearby section. The block gains ``evidence_exempt`` (``state``,
``count``, ``item_ids`` and per-id ``evidence`` counts); under ``shadow``
it is what ``on`` would have exempted, and ``cut_items`` is what ``on``
would then have cut. An unreadable or truncated ledger exempts nothing and
says so in ``state``. A held-out pack's row keeps :func:`holdout_telemetry`'s
shape, configuration only, gaining ``evidence_exempt_mode`` when the flag is
on. The public-only path never runs the cutoff, so never the exemption.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from trellis.retrieve.feedback_prior import (
    DEFAULT_FETCH_WEIGHT,
    STATE_APPLIED,
    STATE_ERROR,
    EvidenceResult,
    ItemEvidence,
)
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
RELEVANCE_CUTOFF_EVIDENCE_EXEMPT_ENV = "TRELLIS_RELEVANCE_CUTOFF_EVIDENCE_EXEMPT"
RELEVANCE_CUTOFF_EVIDENCE_EXEMPT_SETTING = "relevance_cutoff_evidence_exempt"

#: Fewest positive events (helpful grades plus fetches, distinct packs) an
#: exemption needs. A constant so the rule can be tightened later.
MIN_POSITIVE_EVENTS = 1

EvidenceExemptMode = Literal["off", "on"]

#: Never cut below this many distinct surviving candidates.
MIN_KEEP = 3

_VALID_MODES: frozenset[str] = frozenset({"off", "shadow", "on"})
_VALID_EXEMPT_MODES: frozenset[str] = frozenset({"off", "on"})


def native_score_key(strategy_name: str) -> str:
    """The ``score_breakdown`` key holding ``strategy_name``'s native score."""
    return f"{NATIVE_SCORE_PREFIX}{strategy_name}"


def native_breakdown(strategy_name: str, value: float) -> dict[str, float]:
    """``{native_<strategy_name>: value}``, or ``{}`` when ``value`` is not finite.

    A non-finite score is omitted rather than written: Postgres JSONB rejects
    NaN, and an item without a finite native score is never cut.
    """
    score = float(value)
    if not math.isfinite(score):
        return {}
    return {native_score_key(strategy_name): score}


def native_scores_of(item: PackItem) -> dict[str, float]:
    """``item``'s finite ``native_*`` breakdown keys (key -> score)."""
    return {
        k: v
        for k, v in item.score_breakdown.items()
        if k.startswith(NATIVE_SCORE_PREFIX)
        and isinstance(v, int | float)
        and math.isfinite(v)
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
        stamp = native_breakdown(strategy_name, item.relevance_score)
        if key in item.score_breakdown or not stamp:
            out.append(item)
            continue
        out.append(
            item.model_copy(
                update={"score_breakdown": {**item.score_breakdown, **stamp}}
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
    #: Keep a failing item the feedback ledger holds positive evidence for.
    evidence_exempt: EvidenceExemptMode = "off"

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            msg = f"mode must be one of {sorted(_VALID_MODES)}; got {self.mode!r}"
            raise ValueError(msg)
        if self.evidence_exempt not in _VALID_EXEMPT_MODES:
            msg = (
                f"evidence_exempt must be one of {sorted(_VALID_EXEMPT_MODES)}; "
                f"got {self.evidence_exempt!r}"
            )
            raise ValueError(msg)
        for name in ("semantic_min", "semantic_ratio"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(value):
                msg = f"{name} must be a finite number; got {value!r}"
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


def resolve_evidence_exempt(settings: dict[str, Any] | None) -> EvidenceExemptMode:
    """Env > settings > ``off``; an unrecognised value degrades to ``off``."""
    raw = os.environ.get(RELEVANCE_CUTOFF_EVIDENCE_EXEMPT_ENV, "").strip().lower()
    if raw:
        if raw in _VALID_EXEMPT_MODES:
            return raw  # type: ignore[return-value]
        logger.warning(
            "relevance_cutoff_evidence_exempt_env_unrecognised",
            var=RELEVANCE_CUTOFF_EVIDENCE_EXEMPT_ENV,
            effective="off",
        )
        return "off"
    stored = (settings or {}).get(RELEVANCE_CUTOFF_EVIDENCE_EXEMPT_SETTING)
    value = stored.strip().lower() if isinstance(stored, str) else ""
    if value in _VALID_EXEMPT_MODES:
        return value  # type: ignore[return-value]
    if value:
        logger.warning(
            "relevance_cutoff_evidence_exempt_settings_unrecognised",
            setting=RELEVANCE_CUTOFF_EVIDENCE_EXEMPT_SETTING,
        )
    return "off"


def evidence_exempts(evidence: ItemEvidence | None) -> bool:
    """Whether ``evidence`` exempts an item from the cutoff.

    Positive evidence (helpful grades plus fetches weighted as the feedback
    prior weighs them, :data:`~trellis.retrieve.feedback_prior.DEFAULT_FETCH_WEIGHT`)
    must exceed unhelpful grades, from at least :data:`MIN_POSITIVE_EVENTS`
    positive events. No evidence never exempts.
    """
    if evidence is None:
        return False
    positive_events = evidence.helpful + evidence.fetched
    pos = evidence.helpful + DEFAULT_FETCH_WEIGHT * evidence.fetched
    return positive_events >= MIN_POSITIVE_EVENTS and pos > evidence.unhelpful


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
    """Resolve env > settings > default for ``mode`` and ``evidence_exempt``.

    Thresholds are env-only.

    An unrecognised mode degrades to ``off`` and an unparseable or
    out-of-range threshold to unset, each with a warning naming the variable.
    """
    return RelevanceCutoffConfig(
        mode=_resolve_mode(settings),
        semantic_min=_resolve_float(RELEVANCE_CUTOFF_SEMANTIC_MIN_ENV),
        semantic_ratio=_resolve_float(
            RELEVANCE_CUTOFF_SEMANTIC_RATIO_ENV, low=0.0, high=1.0
        ),
        evidence_exempt=resolve_evidence_exempt(settings),
    )


@dataclass
class RelevanceCutoffResult:
    """What :func:`apply_relevance_cutoff` decided.

    ``kept`` is the pool to continue with (the input list itself unless
    ``on`` cut something); ``rejected`` holds one row per cut copy (``on``
    only); ``cut`` is one item per cut id, served (shadow) or not (on);
    ``telemetry`` is the ``PACK_ASSEMBLED.relevance_cutoff`` block, ``None``
    when off; ``restored`` is the ids the floor failed that ``min_keep``
    kept; ``exempted`` is the ids the floor failed that feedback evidence
    kept (would have kept, under ``shadow``).
    """

    kept: list[PackItem]
    rejected: list[RejectedItem] = field(default_factory=list)
    cut: list[PackItem] = field(default_factory=list)
    telemetry: dict[str, Any] | None = None
    restored: list[str] = field(default_factory=list)
    exempted: list[str] = field(default_factory=list)


def _floor(
    config: RelevanceCutoffConfig, best: float | None
) -> tuple[float | None, str | None]:
    """The effective floor, and why the ratio floor was skipped (or ``None``).

    The ratio floor needs a finite, positive ``best``: at or below zero,
    ``ratio * best >= best`` and every semantic-only item would be cut.
    """
    floors: list[float] = []
    skipped: str | None = None
    if config.semantic_min is not None:
        floors.append(config.semantic_min)
    if config.semantic_ratio is not None:
        if best is None:
            skipped = "no_semantic_score"
        elif not math.isfinite(best) or best <= 0:
            skipped = "non_positive_best"
        else:
            floors.append(config.semantic_ratio * best)
    return (max(floors) if floors else None), skipped


def holdout_telemetry(block: dict[str, Any] | None) -> dict[str, Any] | None:
    """The block a held-out pack's row carries: configuration only.

    Mode and thresholds, as ``class_prior`` records for a held-out pack; no
    outcome (candidates, counts, threshold reached) and no item id or title.
    """
    if block is None:
        return None
    out = {k: block[k] for k in ("mode", "semantic_min", "semantic_ratio")}
    if "evidence_exempt_mode" in block:
        out["evidence_exempt_mode"] = block["evidence_exempt_mode"]
    return out


def cut_item_row(
    item: PackItem, tokens: int, title_of: Callable[[PackItem], str | None]
) -> dict[str, Any]:
    """The per-cut-item record :mod:`trellis.retrieve.nearby` groups.

    ``domain`` is ``content_tags.domain`` (a list, possibly empty) and
    ``source_system`` the item's own, so grouping never re-reads a store.
    """
    meta = item.metadata or {}
    tags = meta.get("content_tags")
    domain = tags.get("domain") if isinstance(tags, dict) else None
    if isinstance(domain, str):
        domain = [domain]
    source = meta.get("source_system")
    return {
        "item_id": item.item_id,
        "title": title_of(item),
        "estimated_tokens": tokens,
        "native_semantic": item.score_breakdown.get(native_score_key(SEMANTIC_AXIS)),
        "item_type": item.item_type,
        "domain": [d for d in domain or [] if isinstance(d, str) and d],
        "source_system": source if isinstance(source, str) and source else None,
    }


def _declared_axis(item: PackItem) -> str | None:
    """The strategy ``item`` names as its source, or ``None``."""
    declared = item.strategy_source or (item.metadata or {}).get("source_strategy")
    return declared if isinstance(declared, str) and declared else None


def _default_title(item: PackItem) -> str | None:
    meta = item.metadata or {}
    for key in ("title", "capture_title", "name"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _returned_by(
    items: list[PackItem],
) -> tuple[dict[str, dict[str, float]], dict[str, set[str]], list[str]]:
    """Per id: its native scores, the axes that returned it, first-seen order.

    The axes are the native keys plus each copy's declared source, so a copy
    whose score was non-finite (no native key recorded) still counts.
    """
    natives: dict[str, dict[str, float]] = {}
    axes: dict[str, set[str]] = {}
    order: list[str] = []
    prefix_len = len(NATIVE_SCORE_PREFIX)
    for item in items:
        if item.item_id not in natives:
            natives[item.item_id] = {}
            axes[item.item_id] = set()
            order.append(item.item_id)
        scores = native_scores_of(item)
        natives[item.item_id].update(scores)
        axes[item.item_id].update(k[prefix_len:] for k in scores)
        declared = _declared_axis(item)
        if declared is not None:
            axes[item.item_id].add(declared)
    return natives, axes, order


def _read_evidence(
    evidence: Callable[[], EvidenceResult] | None,
) -> EvidenceResult:
    """Call ``evidence`` once; never raises."""
    if evidence is None:
        return EvidenceResult(state="evidence_unavailable")
    try:
        return evidence()
    except Exception:
        # GRACEFUL-DEGRADATION: the exemption is advisory; nothing is
        # exempted and the block's state says so.
        logger.exception("relevance_cutoff_evidence_read_failed")
        return EvidenceResult(state=STATE_ERROR)


def _exempt(
    failing: list[str], evidence: Callable[[], EvidenceResult] | None
) -> tuple[list[str], dict[str, Any]]:
    """The failing ids evidence exempts, and the ``evidence_exempt`` block."""
    result = _read_evidence(evidence)
    exempted: list[str] = []
    counts: dict[str, dict[str, int]] = {}
    if result.state == STATE_APPLIED:
        for item_id in failing:
            row = result.table.get(item_id)
            if row is not None and evidence_exempts(row):
                exempted.append(item_id)
                counts[item_id] = {
                    "helpful": row.helpful,
                    "unhelpful": row.unhelpful,
                    "fetched": row.fetched,
                }
    return exempted, {
        "state": result.state,
        "count": len(exempted),
        "item_ids": list(exempted),
        "evidence": counts,
    }


def apply_relevance_cutoff(  # noqa: PLR0912, PLR0915
    items: list[PackItem],
    *,
    config: RelevanceCutoffConfig,
    price: Callable[[PackItem], int],
    title_of: Callable[[PackItem], str | None] = _default_title,
    evidence: Callable[[], EvidenceResult] | None = None,
) -> RelevanceCutoffResult:
    """Apply ``config`` to the merged pool ``items`` (one copy per strategy).

    Off returns ``items`` itself and no telemetry. ``items`` is never
    mutated. ``price`` gives an item's token estimate for the record.
    ``evidence`` supplies the feedback ledger's table; it is called at most
    once, and only when ``config.evidence_exempt`` is on and some item
    failed the floor. Exempted ids count toward :data:`MIN_KEEP` before
    ``min_keep`` restores any cut id (see the module docstring).
    """
    if config.mode == "off":
        return RelevanceCutoffResult(kept=items)

    sem_key = native_score_key(SEMANTIC_AXIS)
    block: dict[str, Any] = {
        "mode": config.mode,
        "semantic_min": config.semantic_min,
        "semantic_ratio": config.semantic_ratio,
    }
    exempt_on = config.evidence_exempt == "on"
    if exempt_on:
        block["evidence_exempt_mode"] = "on"
    if not config.configured:
        block["status"] = "unconfigured"
        return RelevanceCutoffResult(kept=items, telemetry=block)

    # Every native key any copy of an id carries: the strategies that
    # returned it. Dedup has not run, so copies are grouped here.
    natives, axes, order = _returned_by(items)

    sem_scores = [n[sem_key] for n in natives.values() if sem_key in n]
    best = max(sem_scores) if sem_scores else None
    threshold, ratio_skipped = _floor(config, best)

    failing: list[str] = []
    if threshold is not None:
        for item_id in order:
            keys = natives[item_id]
            # Cut only when the semantic axis alone returned it: any other
            # axis has no floor, so an item it also returned is kept.
            if (
                sem_key in keys
                and axes[item_id] == {SEMANTIC_AXIS}
                and keys[sem_key] < threshold
            ):
                failing.append(item_id)

    # Evidence exemption first: an exempted id counts as a survivor before
    # min_keep restores anything.
    exempted: list[str] = []
    exempt_block: dict[str, Any] | None = None
    if exempt_on:
        if failing:
            exempted, exempt_block = _exempt(failing, evidence)
            exempted_set = set(exempted)
            failing = [i for i in failing if i not in exempted_set]
        else:
            exempt_block = {
                "state": "nothing_failed",
                "count": 0,
                "item_ids": [],
                "evidence": {},
            }

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

    if ratio_skipped is not None:
        block["ratio_skipped"] = ratio_skipped
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
    if exempt_block is not None:
        block["evidence_exempt"] = exempt_block
    if config.mode == "shadow" or not cut_ids:
        return RelevanceCutoffResult(
            kept=items, cut=cut, telemetry=block, restored=restored, exempted=exempted
        )

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
    return RelevanceCutoffResult(
        kept=kept,
        rejected=rejected,
        cut=cut,
        telemetry=block,
        restored=restored,
        exempted=exempted,
    )


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
    "MIN_POSITIVE_EVENTS",
    "NATIVE_SCORE_PREFIX",
    "OFF",
    "RELEVANCE_CUTOFF_EVIDENCE_EXEMPT_ENV",
    "RELEVANCE_CUTOFF_EVIDENCE_EXEMPT_SETTING",
    "RELEVANCE_CUTOFF_MODE_ENV",
    "RELEVANCE_CUTOFF_MODE_SETTING",
    "RELEVANCE_CUTOFF_REJECTION_REASON",
    "RELEVANCE_CUTOFF_SEMANTIC_MIN_ENV",
    "RELEVANCE_CUTOFF_SEMANTIC_RATIO_ENV",
    "SEMANTIC_AXIS",
    "EvidenceExemptMode",
    "RelevanceCutoffConfig",
    "RelevanceCutoffMode",
    "RelevanceCutoffResult",
    "apply_relevance_cutoff",
    "cut_item_row",
    "evidence_exempts",
    "holdout_telemetry",
    "merge_native_scores",
    "native_breakdown",
    "native_score_key",
    "native_scores_of",
    "pack_metadata_for",
    "resolve_evidence_exempt",
    "resolve_relevance_cutoff_config",
    "stamp_native_scores",
]
