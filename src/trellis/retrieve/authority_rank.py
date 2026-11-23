"""Optional post-rerank "authority" boost for pack ranking. Off by default.

**What it does.** When ``mode == "on"``, an item carrying one of a small set
of authority signals has its post-fusion ``relevance_score`` multiplied by
``1 + w``, where ``w`` is the **largest** weight among the signals it
carries — not their sum, so an item with three signals is boosted exactly as
much as one with its strongest signal alone. The boosted item records the
multiplier as ``score_breakdown["authority_boost"]``. Nothing is ever
scaled down: every multiplier is ``>= 1.0`` and an item without a signal is
returned untouched. That is the project rule
(:mod:`trellis.classify.demotion_gate`) applied to ranking: demotion needs
evidence of unhelpfulness, and *lacking* an authority signal is not that.

**Why after the reranker.** Production always wires the RRF reranker
(:mod:`trellis.retrieve.rerankers.rrf`), which rebuilds every score from
within-axis rank, so a multiplier applied at the collect seam is discarded
by fusion — the reason :mod:`trellis.retrieve.class_prior` removed its
``demote`` mode. This module runs at the post-rerank seam, beside
:func:`~trellis.retrieve.excerpts.apply_content_floor`, on all three
:class:`~trellis.retrieve.pack_builder.PackBuilder` paths (flat ``build``,
``build_sectioned`` and the public-only path). Each caller already sorts by
``relevance_score`` after that seam, so the boost moves an item in the pack.

**Signals** (:data:`DEFAULT_WEIGHTS`, each ``0.15``). Every one is read from
a field ``PACK_ASSEMBLED.injected_items[]`` already records, so
:func:`~trellis.retrieve.pack_replay.replay_authority_rank` re-derives the
same multiplier from a served row that serving computed from the item:

* ``precedent_form`` — ``document_form == "precedent"`` **and** the item id is
  in the reserved promoted-precedent namespace
  (:func:`~trellis.core.precedent_document.is_promoted_precedent_document`).
  The form alone is caller-writable metadata (``save_memory`` and
  ``POST /documents`` forward the bag verbatim), so keying on it would let a
  caller rank their own memory as a precedent; the id namespace is minted
  only by promotion.
* ``reference_form`` — ``document_form == "reference"``
  (:data:`~trellis.retrieve.reference.REFERENCE_FORM`). A reference is
  caller-declared by design (``save_memory(summary=...)``), so this signal
  rewards a write shape, not a verified quality; it is listed separately so
  the replay can price it on its own.
* ``curated_node`` — graph ``node_role == "curated"``.
* ``high_signal`` — ``content_tags.signal_quality == "high"``.

The weights are fixed defaults, not env-parsed: :mod:`class_prior` parses
comma-separated *names*, not a weight map, so there is no existing pattern to
reuse, and a weight is a code change reviewed with its replay.

**Off means absent.** With the default config :func:`apply_authority_rank`
returns the very list it was given, no item is copied, no key is added to any
item or to the event, so a default build is byte-identical to one built
before this module existed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from trellis.core.precedent_document import is_promoted_precedent_document
from trellis.retrieve.reference import REFERENCE_FORM
from trellis.schemas.document_metadata import document_form_of
from trellis.schemas.pack import PackItem

logger = structlog.get_logger(__name__)

AuthorityRankMode = Literal["off", "on"]

AUTHORITY_RANK_MODE_ENV = "TRELLIS_AUTHORITY_RANK_MODE"
AUTHORITY_RANK_MODE_SETTING = "authority_rank_mode"
#: ``score_breakdown`` key a boosted item carries; its value is the multiplier.
AUTHORITY_BOOST_KEY = "authority_boost"

#: By-value duplicate of
#: :data:`trellis.learning.scoring.PRECEDENT_DOCUMENT_FORM` — importing
#: ``trellis.learning.scoring`` at module load from ``trellis.retrieve`` is
#: avoided elsewhere (see ``retrieve/precedents.py``'s local imports);
#: ``tests/unit/retrieve/test_authority_rank.py`` pins the two equal.
PRECEDENT_DOCUMENT_FORM = "precedent"

PRECEDENT_FORM_SIGNAL = "precedent_form"
REFERENCE_FORM_SIGNAL = "reference_form"
CURATED_NODE_SIGNAL = "curated_node"
HIGH_SIGNAL_SIGNAL = "high_signal"

#: Every signal's weight; the multiplier is ``1 + max(weights carried)``.
DEFAULT_WEIGHTS: Mapping[str, float] = {
    PRECEDENT_FORM_SIGNAL: 0.15,
    REFERENCE_FORM_SIGNAL: 0.15,
    CURATED_NODE_SIGNAL: 0.15,
    HIGH_SIGNAL_SIGNAL: 0.15,
}

_VALID_MODES: frozenset[str] = frozenset({"off", "on"})


@dataclass(frozen=True)
class AuthorityRankConfig:
    """Config for :func:`apply_authority_rank`. The default is a no-op."""

    mode: AuthorityRankMode = "off"
    weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            msg = f"mode must be one of {sorted(_VALID_MODES)}; got {self.mode!r}"
            raise ValueError(msg)
        unknown = set(self.weights) - set(DEFAULT_WEIGHTS)
        if unknown:
            msg = f"unknown authority signals: {sorted(unknown)}"
            raise ValueError(msg)
        for name, weight in self.weights.items():
            # Boost-only: a negative weight would scale an item down.
            if not isinstance(weight, int | float) or not weight >= 0.0:
                msg = f"weight for {name!r} must be a number >= 0; got {weight!r}"
                raise ValueError(msg)


#: The no-op config every call site falls back to.
OFF = AuthorityRankConfig()


def resolve_authority_rank_config(
    settings: dict[str, Any] | None = None,
) -> AuthorityRankConfig:
    """Resolve ``mode`` env > settings > default; weights are fixed defaults.

    An unrecognised value degrades to ``"off"`` with a warning naming the
    variable or setting, never the value — the posture
    :func:`~trellis.retrieve.class_prior.resolve_class_prior_config` takes.
    """
    raw = os.environ.get(AUTHORITY_RANK_MODE_ENV, "").strip().lower()
    if raw:
        if raw in _VALID_MODES:
            return AuthorityRankConfig(mode=raw)  # type: ignore[arg-type]
        logger.warning(
            "authority_rank_mode_env_unrecognised",
            var=AUTHORITY_RANK_MODE_ENV,
            effective="off",
        )
        return OFF
    raw_value = (settings or {}).get(AUTHORITY_RANK_MODE_SETTING)
    value = raw_value.strip().lower() if isinstance(raw_value, str) else ""
    if value in _VALID_MODES:
        return AuthorityRankConfig(mode=value)  # type: ignore[arg-type]
    if value:
        logger.warning(
            "authority_rank_mode_settings_unrecognised",
            setting=AUTHORITY_RANK_MODE_SETTING,
        )
    return OFF


def _clean(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def signals_of(
    item_id: object,
    *,
    document_form: object,
    node_role: object,
    signal_quality: object,
) -> frozenset[str]:
    """The authority signals one item carries, from its recorded fields.

    Takes the fields as ``PACK_ASSEMBLED.injected_items[]`` records them
    (``document_form``, ``node_role``, ``signal_quality``), so serving and
    the replay read one predicate.
    """
    form = _clean(document_form)
    out: set[str] = set()
    if form == PRECEDENT_DOCUMENT_FORM and is_promoted_precedent_document(item_id):
        out.add(PRECEDENT_FORM_SIGNAL)
    if form == REFERENCE_FORM:
        out.add(REFERENCE_FORM_SIGNAL)
    if _clean(node_role) == "curated":
        out.add(CURATED_NODE_SIGNAL)
    if _clean(signal_quality) == "high":
        out.add(HIGH_SIGNAL_SIGNAL)
    return frozenset(out)


def item_signals(item: PackItem) -> frozenset[str]:
    """:func:`signals_of` over a live item, read the way the event records it."""
    meta = item.metadata or {}
    raw_tags = meta.get("content_tags")
    tags: Mapping[str, Any] = raw_tags if isinstance(raw_tags, Mapping) else {}
    return signals_of(
        item.item_id,
        document_form=document_form_of(meta),
        node_role=meta.get("node_role"),
        signal_quality=tags.get("signal_quality"),
    )


def multiplier_for(signals: frozenset[str], config: AuthorityRankConfig) -> float:
    """``1 + max(weight)`` over ``signals``; ``1.0`` when none apply."""
    weights = [config.weights.get(name, 0.0) for name in signals]
    return 1.0 + max(weights, default=0.0)


def apply_authority_rank(
    items: list[PackItem], config: AuthorityRankConfig = OFF
) -> list[PackItem]:
    """Boost items carrying an authority signal; ``items`` itself when off.

    Order is preserved — callers sort by ``relevance_score`` afterwards, as
    they already do after the content floor. Only an item whose multiplier
    is above ``1.0`` is copied; every other item is the same object.
    """
    if config.mode == "off":
        return items
    out: list[PackItem] = []
    for item in items:
        multiplier = multiplier_for(item_signals(item), config)
        if multiplier <= 1.0:
            out.append(item)
            continue
        breakdown = dict(item.score_breakdown)
        breakdown[AUTHORITY_BOOST_KEY] = multiplier
        out.append(
            item.model_copy(
                update={
                    "relevance_score": item.relevance_score * multiplier,
                    "score_breakdown": breakdown,
                }
            )
        )
    return out


def authority_rank_telemetry(
    config: AuthorityRankConfig, items: list[PackItem]
) -> dict[str, Any] | None:
    """The ``PACK_ASSEMBLED.authority_rank`` block, or ``None`` when off.

    ``None`` is the off answer so the caller adds no key at all.
    ``boosted_count`` counts the candidates the boost touched at the seam,
    served or not.
    """
    if config.mode == "off":
        return None
    return {
        "mode": config.mode,
        "weights": dict(sorted(config.weights.items())),
        "boosted_count": sum(
            1 for item in items if AUTHORITY_BOOST_KEY in item.score_breakdown
        ),
    }


__all__ = [
    "AUTHORITY_BOOST_KEY",
    "AUTHORITY_RANK_MODE_ENV",
    "AUTHORITY_RANK_MODE_SETTING",
    "CURATED_NODE_SIGNAL",
    "DEFAULT_WEIGHTS",
    "HIGH_SIGNAL_SIGNAL",
    "OFF",
    "PRECEDENT_DOCUMENT_FORM",
    "PRECEDENT_FORM_SIGNAL",
    "REFERENCE_FORM_SIGNAL",
    "AuthorityRankConfig",
    "AuthorityRankMode",
    "apply_authority_rank",
    "authority_rank_telemetry",
    "item_signals",
    "multiplier_for",
    "resolve_authority_rank_config",
    "signals_of",
]
