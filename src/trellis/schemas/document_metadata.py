"""Document metadata — a validated core over the flat metadata dict.

``DocumentStore.put(doc_id, text, metadata=...)`` takes a free dict, but that
dict is not free-form in practice: retrieval filters, the tagging pipeline, the
chunker and the effectiveness join all key off specific names in it. This module
names the keys the system actually depends on and type-checks them, following
the :class:`~trellis.schemas.classification.ContentTags` precedent — a validated
core plus an explicit ``custom`` bag — rather than inventing a second
convention.

**The stored shape does not change.** ``custom`` is a *modelling* device, not a
storage one: :meth:`DocumentMetadata.to_metadata` re-flattens it, so a document
round-tripped through this model is stored with exactly the keys it arrived
with. That is deliberate — every metadata filter in the store layer is a
``json_extract(metadata_json, '$.<key>')`` against a *top-level* key
(``stores/sqlite/document.py``, ``stores/sqlite/vector.py``), so nesting
caller-supplied keys would silently break live queries. Keys are not tidied
either — whitespace stripping is off on this model, because pydantic would
apply it to ``custom``'s keys and rename them.

There is exactly one intentional exception, described next: a flat
``content_type`` carrying a foreign value is renamed, and dropped outright when
an explicit ``document_form`` already claims that dimension.

Reconciling the ``content_type`` drift
--------------------------------------

Two vocabularies were sharing one name:

* ``content_tags.content_type`` — the closed :data:`ContentType` facet
  (pattern / decision / error-resolution / …): *what shape of information is
  this*.
* a **flat** ``metadata["content_type"]`` carrying ``"conversation"``
  (:mod:`trellis.ingest_corpus.conversations`) or ``"entity_summary"`` (the
  eval-corpus loaders, read by :mod:`trellis.retrieve.semantic_seeds`) —
  values outside that vocabulary that describe *where the document came from
  and what form it takes*, not what shape of information it holds.

The second is renamed to :attr:`DocumentMetadata.document_form` (option (c) of
the three considered: not removed, because the values are load-bearing —
``semantic_seeds`` filters graph seeds on ``entity_summary``; not kept as an
alias on the facet key, because that is the ambiguity itself). ``document_form``
is an **open** vocabulary, matching the project's "entity/edge types are any
string" rule: an ingest path knows its own provenance, and the core cannot
enumerate every future one.

This agrees with the decision already recorded in
``trellis.retrieve.pack_builder._attribution_fields``, which refuses to read the
flat key as a learning-observation category *because* it holds a foreign
taxonomy. That refusal stays correct; this module removes the foreign taxonomy
from the key instead of teaching more readers to work around it.

Already-stored documents
------------------------

Nothing is migrated. Documents written before this change carry
``content_type: "conversation"`` and are read exactly as before:
:func:`document_form_of` accepts the legacy flat key whenever its value is
outside the :data:`ContentType` vocabulary, and the flat-key fallback in
``retrieve/tier_mapping.py`` is untouched. A legacy document is normalised
opportunistically — the next time the ingest seam re-writes it
(:func:`trellis.ingest_corpus.sync._apply_record`), its foreign flat
``content_type`` becomes ``document_form``.

Because the seam *rewrites* the key, every reader of the flat key has to be
taught the new one in the same change, or the rename is a silent behaviour
change dressed up as a refactor. ``retrieve/evaluate._item_content_type`` —
the one live reader whose fallback actually fires — falls through to
:func:`document_form_of`, so a document contributes the same
``BreadthScorer`` category before and after it is rewritten.

A backfill migration, if one is ever wanted, would be: scan
``list_documents()``, and for every document whose flat ``content_type`` is not
in :data:`CONTENT_TYPE_VALUES`, re-``put`` it with
``DocumentMetadata.from_mapping(meta).to_metadata()`` (idempotent, and a no-op
for every other document). It would also have to touch the *vector* store's
metadata copy, which is written independently by the embed hook and is what
``semantic_seeds`` actually queries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeGuard

import structlog
from pydantic import ConfigDict, Field, ValidationError

from trellis.core.base import TrellisModel
from trellis.schemas.classification import CONTENT_TYPE_VALUES, ContentType

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = structlog.get_logger(__name__)


class DocumentMetadata(TrellisModel):
    """The keys a stored document's metadata dict is depended on for.

    Every field is optional — metadata is descriptive, and a document with an
    empty dict is legitimate. ``custom`` holds everything else (YAML
    frontmatter, a reader's own fields, operator ``--tag k=v`` pairs) and
    round-trips verbatim.

    Construct from a stored dict with :meth:`from_mapping`, never with
    ``DocumentMetadata(**metadata)`` — the base model is ``extra="forbid"``, so
    the direct call raises on the first unrecognised key, which is precisely
    what ``custom`` exists to absorb.
    """

    #: ``str_strip_whitespace`` is off here, against the ``TrellisModel``
    #: default: pydantic applies it to the *keys* of ``custom`` as well as to
    #: values, so a frontmatter key with incidental whitespace would be renamed
    #: in storage — and every store-level filter is a ``json_extract`` against
    #: the exact key. This model preserves the stored dict; it does not tidy it.
    model_config = ConfigDict(str_strip_whitespace=False)

    #: Human-readable document title (frontmatter ``title``, conversation name).
    title: str | None = None
    #: Corpus namespace the document was ingested under; the
    #: :class:`~trellis.classify.classifiers.source_system.SourceSystemClassifier`
    #: and the pack attribution fields both key off it.
    source_system: str | None = None
    #: Human-readable source locator (a relpath, a conversation title).
    source_path: str | None = None
    #: Provenance / format of the document — ``"conversation"``,
    #: ``"entity_summary"``, whatever a future reader stamps. Open vocabulary,
    #: and deliberately *not* the :data:`ContentType` facet; see the module
    #: docstring. Read it through :func:`document_form_of`, which also accepts
    #: the pre-reconciliation flat ``content_type`` key.
    document_form: str | None = None
    #: Flat mirror of the ``content_tags.content_type`` facet. **Deprecated** —
    #: it exists so documents that legitimately carry an in-vocabulary value
    #: keep validating and keep being read by the flat-key fallbacks in
    #: ``retrieve/evaluate.py`` / ``retrieve/tier_mapping.py``. New writers set
    #: ``content_tags.content_type``; a value *outside* the vocabulary is not a
    #: content type at all and is routed to :attr:`document_form` by
    #: :meth:`from_mapping`.
    content_type: ContentType | None = None
    #: JSON dump of :class:`~trellis.schemas.classification.ContentTags`, as
    #: written by :func:`trellis.classify.ingest.classify_for_ingest`. Left as a
    #: dict rather than the model: the tagging pipeline owns its own validation,
    #: and a hand-edited tag set must not make a document unreadable.
    content_tags: dict[str, Any] | None = None
    #: Importance score from :func:`trellis.classify.importance.compute_importance`.
    auto_importance: float | None = None
    #: Operator-set domain (``--domain``/``--tag``). Scalar or list — the CLI
    #: writes a scalar, and ``retrieve/evaluate.py`` reads both.
    domain: list[str] | str | None = None
    #: Chunk bookkeeping written by :mod:`trellis.ingest_corpus.sync`.
    parent_doc_id: str | None = None
    chunk_index: int | None = None
    chunk_count: int | None = None
    #: ``[start, end]`` offsets of a chunk into its parent's content.
    char_span: list[int] | None = None
    #: Everything else, preserved verbatim and re-flattened by
    #: :meth:`to_metadata`. A core field that is set wins over a ``custom`` key
    #: of the same name.
    custom: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_mapping(cls, metadata: Mapping[str, Any] | None) -> DocumentMetadata:
        """Split a stored metadata dict into the validated core and ``custom``.

        Lenient by construction. Ingest must not fail on a caller's odd
        frontmatter — the metadata is not the payload, and refusing a document
        because someone wrote ``title: 2026`` in a note would be a worse bug
        than the one this model prevents. So an unknown key lands in ``custom``,
        and a *known* key whose value does not validate is demoted to ``custom``
        too (logged, not dropped): it still round-trips through
        :meth:`to_metadata` unchanged, it just does not get a typed accessor.

        The one value this does rewrite is a flat ``content_type`` outside the
        :data:`ContentType` vocabulary, which becomes :attr:`document_form` —
        the drift reconciliation described in the module docstring. That value
        is also the one thing this method can *drop*: when the mapping already
        carries an explicit ``document_form``, the foreign ``content_type``
        loses and is discarded with a warning (see
        :func:`_reconcile_content_type`).

        Never raises for a JSON-shaped mapping. Non-string keys are coerced with
        ``str()``, which is what JSON serialisation into the store would do to
        them anyway.
        """
        raw = dict(metadata or {})
        core: dict[str, Any] = {}
        custom: dict[str, Any] = {}
        for key, value in raw.items():
            if key in _CORE_FIELD_NAMES:
                core[key] = value
            else:
                custom[str(key)] = value

        _reconcile_content_type(core)

        while True:
            try:
                return cls(**core, custom=custom)
            except ValidationError as exc:
                demoted = [
                    str(error["loc"][0])
                    for error in exc.errors()
                    if error["loc"] and str(error["loc"][0]) in core
                ]
                if not demoted:
                    raise
                for key in demoted:
                    custom[key] = core.pop(key)
                logger.debug("document_metadata_demoted_to_custom", keys=demoted)

    def to_metadata(self) -> dict[str, Any]:
        """Render back to the flat dict the document store persists.

        ``custom`` is emitted first so a set core field wins over a ``custom``
        key of the same name (only reachable by hand-construction, or by a value
        :meth:`from_mapping` demoted — where the core field is ``None`` and the
        original value therefore survives).

        A core field is emitted when it holds a value *or* when it was
        explicitly set — a stored ``content_tags: null`` has to come back as
        ``content_tags: null``, because ``sync._apply_record`` gates
        classify-on-write on the key being *present*, and silently dropping it
        would classify a document that was deliberately left untagged.
        """
        flat: dict[str, Any] = dict(self.custom)
        for name in _CORE_FIELD_NAMES:
            value = getattr(self, name)
            if value is not None or name in self.model_fields_set:
                flat[name] = value
        return flat


#: Core field names in declaration order, excluding the ``custom`` bag itself.
_CORE_FIELD_NAMES: tuple[str, ...] = tuple(
    name for name in DocumentMetadata.model_fields if name != "custom"
)


def _is_foreign_content_type(value: Any) -> TypeGuard[str]:
    """True when a flat ``content_type`` value is not a :data:`ContentType`.

    The single rule behind both halves of the reconciliation — the write-side
    rename in :func:`_reconcile_content_type` and the read-side legacy
    acceptance in :func:`document_form_of`. They must never disagree about
    which values are content-type facets and which are document forms, so they
    ask the same question here rather than each re-implementing it.
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return bool(stripped) and stripped not in CONTENT_TYPE_VALUES


def _reconcile_content_type(core: dict[str, Any]) -> None:
    """Route an out-of-vocabulary flat ``content_type`` to ``document_form``.

    Mutates *core* in place. A value inside :data:`CONTENT_TYPE_VALUES` is left
    alone (it is a legitimate, if deprecated, flat mirror of the facet). When
    ``document_form`` already carries a value the foreign one is dropped with a
    warning — both keys describe the same dimension, the explicit one is the
    writer's intent, and inventing a third key to park the loser in would just
    move the drift. This is the one value the model does not preserve; the
    warning is the record of it, and it fires whatever type the incumbent
    ``document_form`` has (a non-string one is demoted to ``custom`` by
    :meth:`DocumentMetadata.from_mapping` and still round-trips).
    """
    flat = core.get("content_type")
    if not _is_foreign_content_type(flat):
        return
    core.pop("content_type")
    existing = core.get("document_form")
    if existing is None or (isinstance(existing, str) and not existing.strip()):
        core["document_form"] = flat
        return
    if existing != flat:
        logger.warning(
            "document_metadata_conflicting_form",
            document_form=existing,
            dropped_content_type=flat,
        )


def document_form_of(metadata: Mapping[str, Any] | None) -> str | None:
    """Read a document's provenance/format, accepting the legacy flat key.

    Documents written before the reconciliation carry the value under
    ``content_type``; documents written after carry it under ``document_form``.
    Both are read here so no consumer has to know which era a document is from.
    The legacy read is guarded by the :data:`ContentType` vocabulary, so an
    in-vocabulary flat value — a real content-type facet mirror — is never
    mistaken for a document form.
    """
    meta = metadata or {}
    form = meta.get("document_form")
    if isinstance(form, str) and form.strip():
        return form.strip()
    legacy = meta.get("content_type")
    return legacy.strip() if _is_foreign_content_type(legacy) else None


__all__ = ["DocumentMetadata", "document_form_of"]
