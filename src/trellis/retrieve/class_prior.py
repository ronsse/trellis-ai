"""A default-off class-level serving prior, scoped to one document form.

**Why a class, not an item.** Per-item noise tagging
(:mod:`trellis.retrieve.noise`) is a judgment about one memory; this module
is a judgment about a whole imported document *form* — e.g. every parent
document :mod:`trellis.ingest_corpus.conversations` writes. Measured on the
live store (counts only, no corpus content): imported conversation documents
took about 11% of pack tokens on the keyword axis while being marked
helpful 0 of 83 times and unhelpful on 44% of rows in that slice, and only
1.2% of these documents have ever been marked helpful at all. Per-item
noise tagging cannot keep up — new conversation parent documents arrive
faster than it tags them. Out-of-sample, withholding the whole class on the keyword
axis removed 29-33% of later unhelpful marks and lost 0-0.2% of helpful
ones; item-level demotion alone removed under 1%. Other namespaces
(captures, bare memories) are helpful 8-25% of the time, so this prior
must stay scoped to the document forms and axes it is configured for and
must never become a store-wide rule.

**Shipped default is off.** Every deployment serves this class exactly as
before until an operator (or a settings-store override) turns it on. The
``off`` branch is a pure no-op — it returns the input list unchanged
(the same object, not a copy) — so a pack and its telemetry are
byte-identical with and without this module in the call chain
(``tests/unit/retrieve/test_class_prior.py``).

**Matching, not typing.** Items are matched by
``metadata["document_form"]``, read through
:func:`trellis.schemas.document_metadata.document_form_of` — the same
reader :mod:`trellis.retrieve.semantic_seeds` and
:mod:`trellis.retrieve.evaluate` already use, and the same key
:mod:`trellis.ingest_corpus.conversations` writes. Never an id-prefix
check: an id shape is an implementation detail of one importer, and a
reader that depends on it breaks the moment that importer's id scheme
changes without anyone touching this file.

``CONVERSATION_DOCUMENT_FORM`` below is a **by-value duplicate** of
:data:`trellis.ingest_corpus.conversations.CONVERSATION_DOCUMENT_FORM`,
not an import of it: :mod:`trellis.ingest_corpus` imports back into
:mod:`trellis.retrieve` (see the circular-import note in
:mod:`trellis.retrieve.concentration`), so importing the writer's constant
from the reader's side would risk a cycle. :mod:`trellis.retrieve.lifecycle`
sets the precedent for this — ``SUPERSEDED_STATE``/``DEMOTED_STATE`` are
reused by value across the same writer/reader boundary.

**The collect seam.** :meth:`~trellis.retrieve.pack_builder.PackBuilder.build`
and :meth:`~trellis.retrieve.pack_builder.PackBuilder.build_sectioned` both
call :func:`apply_class_prior` once per strategy represented in the merged
candidate pool, via ``PackBuilder._apply_class_prior_pool`` — because the
strategy set is open (pluggable ``SearchStrategy`` protocols) and a rule
that must hold for all of them cannot live inside any one of them. Unlike
the other collect-seam gates, this one runs *after*
:func:`~trellis.retrieve.lifecycle.partition_superseded`, not immediately
after ``_apply_collect_gates``: applying it per strategy, before the pool
is merged and supersession resolved, let a withheld successor un-supersede
its own loser (the loser looked kept, since nothing had rejected the
successor yet). ``strategy_name`` is still passed explicitly rather than
read off the promoted ``strategy_source`` field, because the sectioned
path never runs that promotion and a directly-constructed ``PackItem``
(most tests) carries no ``metadata["source_strategy"]`` either.

**Parents only, by design.** The gate matches ``metadata["document_form"]``,
which :mod:`trellis.ingest_corpus.conversations` stamps only on the parent
document it writes, never on the chunks derived from it — and this module
does not resolve a chunk's form through ``parent_doc_id`` either. Withholds
conversation parent documents; their chunks carry no document form and are
never touched. The reason is measured, not assumed: the comment beside the
chunk metadata dict in ``sync.py`` records a stamped parent cited helpful
at 0.005 against 0.082 for a chunk, so parents are the weak half and chunks
are exactly what this gate must leave alone.

**Modes.**

* ``"off"`` (default) — no-op.
* ``"withhold"`` — drops the item and records it as a
  :class:`~trellis.schemas.pack.RejectedItem` under
  :data:`CLASS_PRIOR_REJECTION_REASON`, a reason code distinct from
  ``noise``/``archived``/``demoted``/``superseded`` so
  :func:`~trellis.retrieve.withholding.summarize_withheld`'s first-reason
  attribution can tell this gate's drops apart from every other one.

There was a third mode, ``"demote"``, that rescaled ``relevance_score``
instead of dropping the item. It was removed: production always wires the
RRF reranker (:mod:`trellis.retrieve.rerankers.rrf`), which rebuilds each
item's score from its *within-axis rank*, not from the collect-seam score
this module could rescale — so a reorder applied here was usually undone
before the pack was built, and a mode that silently fails to do what it
documents is worse than no mode. A demote mode would need a seam *after*
reranking, which this module does not have access to; that is why there
isn't one.

**What "touched" means for a withheld item.** There is no ``metadata``
stamp for this gate — a demoted item used to carry one, but demote is
gone. :class:`~trellis.schemas.pack.RejectedItem` carries no ``metadata``
field (by design — see its docstring: one constructor, four copied
fields), so a withheld item's "which rule touched it" is its ``reason``
string, exactly the mechanism every other collect-seam gate already uses
for the same purpose.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from trellis.retrieve.semantic_seeds import ENTITY_SUMMARY_DOCUMENT_FORM
from trellis.schemas.document_metadata import document_form_of
from trellis.schemas.pack import PackItem, RejectedItem

logger = structlog.get_logger(__name__)

ClassPriorMode = Literal["off", "withhold"]

#: Distinct from every other collect-seam reason
#: (``noise``/``archived``/``demoted``/``superseded``/``dedup``/...) so the
#: withholding summary can attribute a drop to this rule specifically.
CLASS_PRIOR_REJECTION_REASON = "class_prior"

#: By-value duplicate of
#: ``trellis.ingest_corpus.conversations.CONVERSATION_DOCUMENT_FORM`` — see
#: the module docstring for why this is not an import.
CONVERSATION_DOCUMENT_FORM = "conversation"

CLASS_PRIOR_MODE_ENV = "TRELLIS_CLASS_PRIOR_MODE"
#: Comma-separated document forms. Env-only: :class:`SettingSpec` has no
#: list-typed field, so this (and the axes knob below) cannot be a
#: settings-store entry the way ``mode`` is — see
#: ``src/trellis/core/settings_registry.py``'s module docstring and the
#: ``class_prior_mode`` entry there.
#: Case-insensitive, the same as the axes knob below: each name is
#: lower-cased before matching, with a warning (not a refusal — document
#: forms are open vocabulary) when a normalised name is outside
#: :data:`KNOWN_FORMS`.
CLASS_PRIOR_FORMS_ENV = "TRELLIS_CLASS_PRIOR_FORMS"
#: Comma-separated retrieval axis names (``keyword``, ``semantic``, ``graph``).
#: Case-insensitive: each name is lower-cased before matching, with a
#: warning (not a refusal — the strategy set is open) when a normalised
#: name is outside :data:`KNOWN_AXES`.
CLASS_PRIOR_AXES_ENV = "TRELLIS_CLASS_PRIOR_AXES"

#: Settings-store key for the one scalar knob (env > settings > default,
#: same precedence every other ``settings_live=True`` entry uses).
CLASS_PRIOR_MODE_SETTING = "class_prior_mode"

_VALID_MODES: frozenset[str] = frozenset({"off", "withhold"})
DEFAULT_FORMS: frozenset[str] = frozenset({CONVERSATION_DOCUMENT_FORM})
DEFAULT_AXES: frozenset[str] = frozenset({"keyword"})

#: The built-in strategies' well-known names, used only to decide whether
#: to log a warning on an axis name that matches none of them — the
#: strategy set itself stays open (see the module docstring), so this is
#: advisory, never a closed enum :func:`apply_class_prior` enforces.
KNOWN_AXES: frozenset[str] = frozenset({"keyword", "semantic", "graph"})

#: The document forms ingest handlers in this repo are known to stamp,
#: used only to decide whether to log a warning on a form that matches
#: none of them — ``document_form`` is open vocabulary (see
#: ``trellis.schemas.document_metadata``), so this is advisory, never a
#: closed enum :func:`apply_class_prior` enforces, the same posture
#: :data:`KNOWN_AXES` takes for strategy names.
KNOWN_FORMS: frozenset[str] = frozenset(
    {CONVERSATION_DOCUMENT_FORM, ENTITY_SUMMARY_DOCUMENT_FORM}
)


@dataclass(frozen=True)
class ClassPriorConfig:
    """Config for :func:`apply_class_prior`. Shipped default is a no-op.

    ``forms``/``axes`` default to exactly the slice the measurement in the
    module docstring covers: imported conversation documents, keyword axis
    only. Widening either is a scope decision for whoever enables this,
    not something this module does on its own.
    """

    mode: ClassPriorMode = "off"
    forms: frozenset[str] = field(default_factory=lambda: DEFAULT_FORMS)
    axes: frozenset[str] = field(default_factory=lambda: DEFAULT_AXES)

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            msg = f"mode must be one of {sorted(_VALID_MODES)}; got {self.mode!r}"
            raise ValueError(msg)


#: The no-op config every call site falls back to. A module-level constant
#: rather than ``ClassPriorConfig()`` re-constructed per call so an
#: identity check (not used today, but available to a caller) is cheap.
OFF = ClassPriorConfig()


def _parse_csv(raw: str) -> frozenset[str]:
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def resolve_class_prior_config(
    settings: dict[str, Any] | None = None,
) -> ClassPriorConfig:
    """Resolve env > settings > default for every field, mirroring the
    precedence :func:`trellis.retrieve.builder_factory._graph_seeding_enabled`
    and :func:`trellis.retrieve.pack_builder._filter_legacy_advisories_enabled`
    already use.

    ``mode`` is settings-store aware (``settings`` is the already-loaded
    ``name -> value`` mapping, e.g. from
    :func:`trellis.stores.settings_store.load_settings_values`); ``forms``/
    ``axes`` are env-only (see :data:`CLASS_PRIOR_FORMS_ENV`'s docstring)
    and fall straight to their defaults when unset.

    An unrecognised env value degrades to the shipped default with a
    logged warning — never a crash, never a silent switch to a *different*
    non-default behaviour — the same posture every other env-parsed knob
    in this package takes.
    """
    mode = _resolve_mode(settings)

    forms_raw = os.environ.get(CLASS_PRIOR_FORMS_ENV, "").strip()
    if forms_raw:
        forms = frozenset(name.lower() for name in _parse_csv(forms_raw))
        _warn_unknown_forms(forms)
    else:
        forms = DEFAULT_FORMS

    axes_raw = os.environ.get(CLASS_PRIOR_AXES_ENV, "").strip()
    if axes_raw:
        axes = frozenset(name.lower() for name in _parse_csv(axes_raw))
        _warn_unknown_axes(axes)
    else:
        axes = DEFAULT_AXES

    return ClassPriorConfig(mode=mode, forms=forms, axes=axes)


def _warn_unknown_axes(axes: frozenset[str]) -> None:
    unknown = sorted(axes - KNOWN_AXES)
    if unknown:
        logger.warning(
            "class_prior_axes_env_unrecognised",
            var=CLASS_PRIOR_AXES_ENV,
            names=unknown,
        )


def _warn_unknown_forms(forms: frozenset[str]) -> None:
    unknown = sorted(forms - KNOWN_FORMS)
    if unknown:
        logger.warning(
            "class_prior_forms_env_unrecognised",
            var=CLASS_PRIOR_FORMS_ENV,
            names=unknown,
        )


def _resolve_mode(settings: dict[str, Any] | None) -> ClassPriorMode:
    raw = os.environ.get(CLASS_PRIOR_MODE_ENV, "").strip().lower()
    if raw:
        if raw in _VALID_MODES:
            return raw  # type: ignore[return-value]
        if raw == "demote":
            # Named demote mode removed entirely (see the module
            # docstring); never resolve to withhold for it.
            logger.warning("class_prior_mode_demote_removed", var=CLASS_PRIOR_MODE_ENV)
            return "off"
        # Name the variable, never the value it held — see the module's
        # callers for why an unrecognised config value does not belong in
        # a log line.
        logger.warning(
            "class_prior_mode_env_unrecognised",
            var=CLASS_PRIOR_MODE_ENV,
            effective="off",
        )
        return "off"
    raw_value = (settings or {}).get(CLASS_PRIOR_MODE_SETTING)
    # Normalised the same way as the env var above (strip + lowercase) so
    # a stored "WITHHOLD" or " demote " is read the same as its env
    # counterpart. This is read-side robustness only — the governed PUT
    # path that writes this setting still refuses a non-canonical
    # spelling with 400; normalising here never papers over that refusal.
    value = raw_value.strip().lower() if isinstance(raw_value, str) else ""
    if value:
        if value in _VALID_MODES:
            return value  # type: ignore[return-value]
        if value == "demote":
            logger.warning(
                "class_prior_mode_demote_removed", setting=CLASS_PRIOR_MODE_SETTING
            )
            return "off"
    return "off"


def apply_class_prior(
    items: list[PackItem],
    *,
    config: ClassPriorConfig,
    strategy_name: str,
) -> tuple[list[PackItem], list[RejectedItem]]:
    """Apply ``config`` to ``items`` collected from ``strategy_name``.

    Returns ``(kept, rejected)`` — ``kept`` in input order; ``rejected``
    holding a :class:`RejectedItem` per withheld item. ``items`` itself is
    never mutated.

    ``config.mode == "off"``, or ``strategy_name`` outside
    ``config.axes``, or an empty ``config.forms``, each short-circuit to
    returning ``items`` **unchanged** — same list, same item objects — so
    that branch is a true no-op rather than a copy that merely compares
    equal.
    """
    if config.mode == "off" or strategy_name not in config.axes or not config.forms:
        return items, []

    kept: list[PackItem] = []
    rejected: list[RejectedItem] = []
    for item in items:
        form = document_form_of(item.metadata)
        if form is None or form not in config.forms:
            kept.append(item)
            continue
        rejected.append(
            RejectedItem.from_pack_item(
                item, CLASS_PRIOR_REJECTION_REASON, strategy_source=strategy_name
            )
        )
    return kept, rejected


__all__ = [
    "CLASS_PRIOR_AXES_ENV",
    "CLASS_PRIOR_FORMS_ENV",
    "CLASS_PRIOR_MODE_ENV",
    "CLASS_PRIOR_MODE_SETTING",
    "CLASS_PRIOR_REJECTION_REASON",
    "CONVERSATION_DOCUMENT_FORM",
    "DEFAULT_AXES",
    "DEFAULT_FORMS",
    "KNOWN_AXES",
    "OFF",
    "ClassPriorConfig",
    "ClassPriorMode",
    "apply_class_prior",
    "resolve_class_prior_config",
]
