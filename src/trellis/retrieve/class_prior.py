"""A default-off class-level serving prior, scoped to one document form.

**Why a class, not an item.** Per-item noise tagging
(:mod:`trellis.retrieve.noise`) is a judgment about one memory; this module
is a judgment about a whole imported document *form* — e.g. every chunk
:mod:`trellis.ingest_corpus.conversations` writes. Measured on the live
store (counts only, no corpus content): imported conversation documents
took about 11% of pack tokens on the keyword axis while being marked
helpful 0 of 83 times and unhelpful on 44% of rows in that slice, and only
1.2% of these documents have ever been marked helpful at all. Per-item
noise tagging cannot keep up — new conversation chunks arrive faster than
it tags them. Out-of-sample, withholding the whole class on the keyword
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
call :func:`apply_class_prior` once per strategy, immediately after
``_apply_collect_gates`` — the same seam noise/archived/demoted run at,
because the strategy set is open (pluggable ``SearchStrategy`` protocols)
and a rule that must hold for all of them cannot live inside any one of
them. ``strategy_name`` is passed explicitly for the same reason
``_apply_collect_gates`` takes it: ``_promote_strategy_source`` has not
stamped the item yet at this point in the pipeline.

**Modes.**

* ``"off"`` (default) — no-op.
* ``"demote"`` — multiplies ``relevance_score`` by :attr:`ClassPriorConfig.factor`
  (a configured value in ``(0, 1)``) before budget enforcement, so a
  matching item competes for its slot at a handicap instead of being
  removed. Nothing is dropped; :func:`~trellis.schemas.pack.RejectedItem`
  is never constructed for a demoted item.
* ``"withhold"`` — drops the item and records it as a
  :class:`~trellis.schemas.pack.RejectedItem` under
  :data:`CLASS_PRIOR_REJECTION_REASON`, a reason code distinct from
  ``noise``/``archived``/``demoted``/``superseded`` so
  :func:`~trellis.retrieve.withholding.summarize_withheld`'s first-reason
  attribution can tell this gate's drops apart from every other one.

**What "touched" means for served vs. withheld items.** A served item this
gate demoted gets ``metadata["class_prior"]`` stamped on it — mode, the
matched form, the axis and the factor applied — the same shape
``metadata["graph_selection"]`` uses, so a later measurement can split
served items by whether this rule touched them.
:class:`~trellis.schemas.pack.RejectedItem` carries no ``metadata`` field
(by design — see its docstring: one constructor, four copied fields), so a
withheld item's "which rule touched it" is its ``reason`` string, exactly
the mechanism every other collect-seam gate already uses for the same
purpose.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal, TypeGuard

import structlog

from trellis.schemas.document_metadata import document_form_of
from trellis.schemas.pack import PackItem, RejectedItem

logger = structlog.get_logger(__name__)

ClassPriorMode = Literal["off", "demote", "withhold"]

#: Distinct from every other collect-seam reason
#: (``noise``/``archived``/``demoted``/``superseded``/``dedup``/...) so the
#: withholding summary can attribute a drop to this rule specifically.
CLASS_PRIOR_REJECTION_REASON = "class_prior"

#: By-value duplicate of
#: ``trellis.ingest_corpus.conversations.CONVERSATION_DOCUMENT_FORM`` — see
#: the module docstring for why this is not an import.
CONVERSATION_DOCUMENT_FORM = "conversation"

CLASS_PRIOR_MODE_ENV = "TRELLIS_CLASS_PRIOR_MODE"
CLASS_PRIOR_FACTOR_ENV = "TRELLIS_CLASS_PRIOR_FACTOR"
#: Comma-separated document forms. Env-only: :class:`SettingSpec` has no
#: list-typed field, so this (and the axes knob below) cannot be a
#: settings-store entry the way ``mode``/``factor`` are — see
#: ``src/trellis/core/settings_registry.py``'s module docstring and the
#: ``class_prior_mode``/``class_prior_factor`` entries there.
CLASS_PRIOR_FORMS_ENV = "TRELLIS_CLASS_PRIOR_FORMS"
#: Comma-separated retrieval axis names (``keyword``, ``semantic``, ``graph``).
CLASS_PRIOR_AXES_ENV = "TRELLIS_CLASS_PRIOR_AXES"

#: Settings-store keys for the two scalar knobs (env > settings > default,
#: same precedence every other ``settings_live=True`` entry uses).
CLASS_PRIOR_MODE_SETTING = "class_prior_mode"
CLASS_PRIOR_FACTOR_SETTING = "class_prior_factor"

_VALID_MODES: frozenset[str] = frozenset({"off", "demote", "withhold"})
DEFAULT_FACTOR = 0.5
DEFAULT_FORMS: frozenset[str] = frozenset({CONVERSATION_DOCUMENT_FORM})
DEFAULT_AXES: frozenset[str] = frozenset({"keyword"})


def _valid_factor(value: object) -> TypeGuard[float]:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and 0.0 < value < 1.0
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
    factor: float = DEFAULT_FACTOR

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            msg = f"mode must be one of {sorted(_VALID_MODES)}; got {self.mode!r}"
            raise ValueError(msg)
        if not _valid_factor(self.factor):
            msg = f"factor must be in (0, 1); got {self.factor!r}"
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

    ``mode``/``factor`` are settings-store aware (``settings`` is the
    already-loaded ``name -> value`` mapping, e.g. from
    :func:`trellis.stores.settings_store.load_settings_values`); ``forms``/
    ``axes`` are env-only (see :data:`CLASS_PRIOR_FORMS_ENV`'s docstring)
    and fall straight to their defaults when unset.

    An unrecognised env value degrades to the shipped default with a
    logged warning — never a crash, never a silent switch to a *different*
    non-default behaviour — the same posture every other env-parsed knob
    in this package takes.
    """
    mode = _resolve_mode(settings)
    factor = _resolve_factor(settings)

    forms_raw = os.environ.get(CLASS_PRIOR_FORMS_ENV, "").strip()
    forms = _parse_csv(forms_raw) if forms_raw else DEFAULT_FORMS

    axes_raw = os.environ.get(CLASS_PRIOR_AXES_ENV, "").strip()
    axes = _parse_csv(axes_raw) if axes_raw else DEFAULT_AXES

    return ClassPriorConfig(mode=mode, forms=forms, axes=axes, factor=factor)


def _resolve_mode(settings: dict[str, Any] | None) -> ClassPriorMode:
    raw = os.environ.get(CLASS_PRIOR_MODE_ENV, "").strip().lower()
    if raw:
        if raw in _VALID_MODES:
            return raw  # type: ignore[return-value]
        logger.warning(
            "class_prior_mode_env_unrecognised",
            var=CLASS_PRIOR_MODE_ENV,
            value=raw,
            effective="off",
        )
        return "off"
    value = (settings or {}).get(CLASS_PRIOR_MODE_SETTING)
    if isinstance(value, str) and value in _VALID_MODES:
        return value  # type: ignore[return-value]
    return "off"


def _resolve_factor(settings: dict[str, Any] | None) -> float:
    raw = os.environ.get(CLASS_PRIOR_FACTOR_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = float("nan")
        if _valid_factor(value):
            return value
        logger.warning(
            "class_prior_factor_env_unrecognised",
            var=CLASS_PRIOR_FACTOR_ENV,
            value=raw,
            effective=DEFAULT_FACTOR,
        )
        return DEFAULT_FACTOR
    stored = (settings or {}).get(CLASS_PRIOR_FACTOR_SETTING)
    if stored is not None and not _valid_factor(stored):
        # Reachable only if something stored a value validate_value's own
        # settings-layer bounds let through but this module's stricter
        # open-interval contract refuses (see settings_registry.py's
        # ``class_prior_factor`` entry: there is no ``maximum_exclusive``
        # field, so 1.0 itself passes the stored-value check there).
        logger.warning(
            "class_prior_factor_setting_out_of_range",
            value=stored,
            effective=DEFAULT_FACTOR,
        )
    return stored if _valid_factor(stored) else DEFAULT_FACTOR


def apply_class_prior(
    items: list[PackItem],
    *,
    config: ClassPriorConfig,
    strategy_name: str,
) -> tuple[list[PackItem], list[RejectedItem]]:
    """Apply ``config`` to ``items`` collected from ``strategy_name``.

    Returns ``(kept, rejected)`` — ``kept`` in input order, demoted items
    replaced by a re-scored copy; ``rejected`` holding a
    :class:`RejectedItem` per withheld item. ``items`` itself is never
    mutated.

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
        if config.mode == "withhold":
            rejected.append(
                RejectedItem.from_pack_item(
                    item, CLASS_PRIOR_REJECTION_REASON, strategy_source=strategy_name
                )
            )
            continue
        # demote
        stamped_metadata = dict(item.metadata)
        stamped_metadata["class_prior"] = {
            "mode": "demote",
            "form": form,
            "axis": strategy_name,
            "factor": config.factor,
        }
        kept.append(
            item.model_copy(
                update={
                    "relevance_score": item.relevance_score * config.factor,
                    "metadata": stamped_metadata,
                }
            )
        )
    return kept, rejected


__all__ = [
    "CLASS_PRIOR_AXES_ENV",
    "CLASS_PRIOR_FACTOR_ENV",
    "CLASS_PRIOR_FACTOR_SETTING",
    "CLASS_PRIOR_FORMS_ENV",
    "CLASS_PRIOR_MODE_ENV",
    "CLASS_PRIOR_MODE_SETTING",
    "CLASS_PRIOR_REJECTION_REASON",
    "CONVERSATION_DOCUMENT_FORM",
    "DEFAULT_AXES",
    "DEFAULT_FACTOR",
    "DEFAULT_FORMS",
    "OFF",
    "ClassPriorConfig",
    "ClassPriorMode",
    "apply_class_prior",
    "resolve_class_prior_config",
]
