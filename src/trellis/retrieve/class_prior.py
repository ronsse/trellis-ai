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
successor yet). The strategy a pooled item is judged under is read off
the item's own ``strategy_source``, stamped in the collection loop
(``PackBuilder._stamp_collecting_strategy``) because the sectioned path
never runs the metadata promotion and a directly-constructed ``PackItem``
(most tests) carries no ``metadata["source_strategy"]`` either. It is not
a side table keyed on ``id(item)``: that outlives the object it names, and
the pool is rebuilt from copies between collection and this gate.

**Parents by default; chunks only by opt-in.** The gate matches the
item's own ``metadata["document_form"]``, which
:mod:`trellis.ingest_corpus.conversations` stamps only on the parent
document it writes, never on the chunks derived from it. So by default
(``scope="parents"``, :data:`CLASS_PRIOR_SCOPE_ENV`) it withholds
conversation parent documents and never touches their chunks. That was
measured, not assumed: the comment beside the chunk metadata dict in
``sync.py`` records a stamped parent cited helpful at 0.005 against 0.082
for a chunk, so chunks were the stronger half.

``scope="parents_and_chunks"`` (#986) also withholds a chunk whose
*parent* carries a configured form. The chunk's form is read, not
stamped: the parent's own ``document_form`` is looked up through the
build's :class:`~trellis.retrieve.parent_verdicts.ParentVerdictCache`, by
the very call (:meth:`~trellis.retrieve.parent_verdicts.ParentVerdictCache.resolve`)
the archived and noise gates make. So the two rules agree on what a chunk
is: only an item whose own id is a chunk id (``<parent>#chunk-N``)
inherits through a parent, and a plain item that merely names one in
``metadata["parent_doc_id"]`` borrows nothing. The parent id is the item's
``parent_doc_id`` (written by ``sync.py`` on every chunk), else the chunk
id's own prefix, because a semantic-axis item is built from an embed-time
vector snapshot that may predate that key
(:func:`~trellis.retrieve.concentration.parent_id_of`). That cache has
already fetched every distinct chunk parent in the pool for the archived and
noise gates, so the lookup adds no document-store read for a chunk
(``tests/unit/retrieve/test_class_prior_chunks.py`` counts them). The
resolver is *injected* (``parent_form_of``), so :func:`apply_class_prior`
stays pure over :class:`~trellis.schemas.pack.PackItem`. The item's own
form wins: the parent is consulted only for an item that carries none.

Stamping the form onto each chunk at write time was the other honest
design and was refused. It would not reach a chunk already in a store
without a backfill, and the vector row's metadata snapshot is frozen at
embed time, so it would need a mirror key and a re-sync first. Worse,
stamping the existing ``document_form`` key would silently turn the
*parents-only* default into parents-and-chunks for every re-synced chunk,
because this gate matches an item's own form. A form-derived stamp under a
second key avoids that, but still leaves unstamped history. Reading the
parent works for those chunks today and cannot disagree with the parent.

An unresolvable parent (a non-chunk item, a parent row the store no
longer has, a parent with no form, a store error) is no verdict: the item
is served, the same default-pass posture the archived and noise gates take.

**Modes.**

* ``"off"`` (default) — no-op.
* ``"withhold"`` — withholds the item from the configured axes
  (:attr:`ClassPriorConfig.axes`) and records it as a
  :class:`~trellis.schemas.pack.RejectedItem` under
  :data:`CLASS_PRIOR_REJECTION_REASON`, a reason code distinct from
  ``noise``/``archived``/``demoted``/``superseded`` so
  :func:`~trellis.retrieve.withholding.summarize_withheld`'s first-reason
  attribution can tell this gate's drops apart from every other one. An
  unconfigured axis that found the same document may still serve it.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from trellis.retrieve.semantic_seeds import ENTITY_SUMMARY_DOCUMENT_FORM
from trellis.schemas.document_metadata import document_form_of
from trellis.schemas.pack import PackItem, RejectedItem

logger = structlog.get_logger(__name__)

ClassPriorMode = Literal["off", "withhold"]
ClassPriorScope = Literal["parents", "parents_and_chunks"]

#: Resolves the document form of an item's *parent* — ``None`` when the item
#: has no resolvable parent or the parent carries no form. Injected into
#: :func:`apply_class_prior` so this module stays pure over ``PackItem`` objects;
#: the pack builder supplies one backed by its per-build parent cache.
ParentFormResolver = Callable[[PackItem], str | None]

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
#: ``parents`` (the default) or ``parents_and_chunks``. Env-only like the two
#: knobs above, and case-insensitive. An unrecognised value degrades to
#: ``parents`` with a warning — the shipped behaviour, never the wider one.
CLASS_PRIOR_SCOPE_ENV = "TRELLIS_CLASS_PRIOR_SCOPE"

#: Settings-store key for the one scalar knob (env > settings > default,
#: same precedence every other ``settings_live=True`` entry uses).
CLASS_PRIOR_MODE_SETTING = "class_prior_mode"

_VALID_MODES: frozenset[str] = frozenset({"off", "withhold"})
PARENTS_SCOPE: ClassPriorScope = "parents"
PARENTS_AND_CHUNKS_SCOPE: ClassPriorScope = "parents_and_chunks"
_VALID_SCOPES: frozenset[str] = frozenset({PARENTS_SCOPE, PARENTS_AND_CHUNKS_SCOPE})
DEFAULT_SCOPE: ClassPriorScope = PARENTS_SCOPE
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
    not something this module does on its own. ``scope`` defaults to
    ``"parents"``: chunks are withheld only when it is set to
    ``"parents_and_chunks"`` (see the module docstring).
    """

    mode: ClassPriorMode = "off"
    forms: frozenset[str] = field(default_factory=lambda: DEFAULT_FORMS)
    axes: frozenset[str] = field(default_factory=lambda: DEFAULT_AXES)
    scope: ClassPriorScope = DEFAULT_SCOPE

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            msg = f"mode must be one of {sorted(_VALID_MODES)}; got {self.mode!r}"
            raise ValueError(msg)
        if self.scope not in _VALID_SCOPES:
            msg = f"scope must be one of {sorted(_VALID_SCOPES)}; got {self.scope!r}"
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
    ``axes``/``scope`` are env-only (see :data:`CLASS_PRIOR_FORMS_ENV`'s
    docstring) and fall straight to their defaults when unset.

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

    return ClassPriorConfig(mode=mode, forms=forms, axes=axes, scope=_resolve_scope())


def _resolve_scope() -> ClassPriorScope:
    raw = os.environ.get(CLASS_PRIOR_SCOPE_ENV, "").strip().lower()
    if not raw:
        return DEFAULT_SCOPE
    if raw in _VALID_SCOPES:
        return raw  # type: ignore[return-value]
    # Name the variable, never the value it held, as the mode knob does.
    logger.warning(
        "class_prior_scope_env_unrecognised",
        var=CLASS_PRIOR_SCOPE_ENV,
        effective=DEFAULT_SCOPE,
    )
    return DEFAULT_SCOPE


def _warn_unknown_axes(axes: frozenset[str]) -> None:
    unknown = sorted(axes - KNOWN_AXES)
    if unknown:
        logger.warning(
            "class_prior_axes_env_unrecognised",
            var=CLASS_PRIOR_AXES_ENV,
            unknown_count=len(unknown),
        )


def _warn_unknown_forms(forms: frozenset[str]) -> None:
    unknown = sorted(forms - KNOWN_FORMS)
    if unknown:
        logger.warning(
            "class_prior_forms_env_unrecognised",
            var=CLASS_PRIOR_FORMS_ENV,
            unknown_count=len(unknown),
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
        else:
            # Name the variable, never the value it held — see the
            # module's callers for why an unrecognised config value does
            # not belong in a log line.
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
    if value and value in _VALID_MODES:
        return value  # type: ignore[return-value]
    if value:
        if value == "demote":
            logger.warning(
                "class_prior_mode_demote_removed", setting=CLASS_PRIOR_MODE_SETTING
            )
        else:
            # Name the setting, never the value it held -- same posture
            # the env branch above takes for an unrecognised value.
            logger.warning(
                "class_prior_mode_settings_unrecognised",
                setting=CLASS_PRIOR_MODE_SETTING,
            )
    return "off"


def effective_form(
    item: PackItem,
    *,
    config: ClassPriorConfig,
    parent_form_of: ParentFormResolver | None = None,
) -> str | None:
    """The document form this gate judges ``item`` under.

    The item's own ``document_form`` when it carries one. Otherwise, and only
    when ``config.scope`` is ``"parents_and_chunks"`` and a resolver was
    supplied, its parent's form. With the default scope the resolver is never
    called, so the parents-only path is untouched.
    """
    own = document_form_of(item.metadata)
    if own is not None:
        return own
    if config.scope != PARENTS_AND_CHUNKS_SCOPE or parent_form_of is None:
        return None
    return parent_form_of(item)


def apply_class_prior(
    items: list[PackItem],
    *,
    config: ClassPriorConfig,
    strategy_name: str,
    parent_form_of: ParentFormResolver | None = None,
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

    ``parent_form_of`` is consulted only under ``scope="parents_and_chunks"``,
    for an item that carries no form of its own (see :func:`effective_form`).
    """
    if config.mode == "off" or strategy_name not in config.axes or not config.forms:
        return items, []

    kept: list[PackItem] = []
    rejected: list[RejectedItem] = []
    for item in items:
        form = effective_form(item, config=config, parent_form_of=parent_form_of)
        if form is None or form not in config.forms:
            kept.append(item)
            continue
        rejected.append(
            RejectedItem.from_pack_item(
                item, CLASS_PRIOR_REJECTION_REASON, strategy_source=strategy_name
            )
        )
    return kept, rejected


def class_prior_telemetry(config: ClassPriorConfig) -> dict[str, Any]:
    """The compact, additive ``PACK_ASSEMBLED.class_prior`` payload.

    ``{"mode": ..., "forms": [...], "axes": [...]}`` — sorted lists so the
    payload is deterministic regardless of the frozenset's iteration
    order. Emitted on every flat and sectioned ``PACK_ASSEMBLED`` row,
    including when ``mode == "off"``, so a pack event can say whether
    this gate was on without a reader having to re-derive it from env/
    settings state at read time (which may have since changed).

    ``"scope"`` is added only when it is not the default, so a row's
    absence of the key means ``"parents"`` — which is also what every row
    written before the knob existed means — and a default-configured
    deployment's payload is unchanged.
    """
    payload: dict[str, Any] = {
        "mode": config.mode,
        "forms": sorted(config.forms),
        "axes": sorted(config.axes),
    }
    if config.scope != DEFAULT_SCOPE:
        payload["scope"] = config.scope
    return payload


__all__ = [
    "CLASS_PRIOR_AXES_ENV",
    "CLASS_PRIOR_FORMS_ENV",
    "CLASS_PRIOR_MODE_ENV",
    "CLASS_PRIOR_MODE_SETTING",
    "CLASS_PRIOR_REJECTION_REASON",
    "CLASS_PRIOR_SCOPE_ENV",
    "CONVERSATION_DOCUMENT_FORM",
    "DEFAULT_AXES",
    "DEFAULT_FORMS",
    "DEFAULT_SCOPE",
    "KNOWN_AXES",
    "OFF",
    "PARENTS_AND_CHUNKS_SCOPE",
    "PARENTS_SCOPE",
    "ClassPriorConfig",
    "ClassPriorMode",
    "ClassPriorScope",
    "ParentFormResolver",
    "apply_class_prior",
    "class_prior_telemetry",
    "effective_form",
    "resolve_class_prior_config",
]
