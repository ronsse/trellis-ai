"""The ``classification.set`` and ``classification.tag_regulatory`` handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as PydanticValidationError

from trellis.core.document_write import put_document
from trellis.core.vector_metadata import resolve_vector_store
from trellis.errors import PolicyViolationError, ValidationError
from trellis.mutate.commands import Command
from trellis.mutate.sensitivity_floor import (
    SENSITIVITY_FLOOR_POLICY_ID,
    refuse_written_label,
    visible_document,
)
from trellis.retrieve.sensitivity import OverwriteRefusedError, label_is_unreadable
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    REGULATORY_TAG_CLASSIFIER,
    SENSITIVITY_ORDER,
    DataClassification,
    is_public_label,
    is_tag_only_record,
    public_label_record,
    sensitivity_of,
)
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from trellis.schemas.classification import Sensitivity


def _relabelled_record(prior: object, sensitivity: str) -> dict[str, Any]:
    """A ``DataClassification`` record carrying ``sensitivity``, keeping what it can.

    The other fields of a readable prior record (regulatory tags, jurisdiction)
    survive a relabel if they validate; if any does not, the record is rebuilt
    from ``sensitivity`` alone. What is dropped is not kept. ``"caller"`` is
    recorded as a classifier because a person asked for this label.
    """
    kept = (
        {k: v for k, v in prior.items() if k in DataClassification.model_fields}
        if isinstance(prior, dict)
        else {}
    )
    try:
        record = DataClassification.model_validate({**kept, "sensitivity": sensitivity})
    except PydanticValidationError:
        record = DataClassification(sensitivity=sensitivity)  # type: ignore[arg-type]
    if "caller" not in record.classified_by:
        record = record.model_copy(
            update={"classified_by": [*record.classified_by, "caller"]}
        )
    return record.model_dump(mode="json")


def _public_record_discards(prior: object) -> list[str]:
    """What writing the exact public record would throw away from ``prior``.

    The names of the substantive fields an exact public record
    (:func:`~trellis.schemas.classification.public_label_record`) does not
    carry: regulatory tags, a jurisdiction, a classification version other than
    the current one, and any field the schema does not know. ``classified_by``
    is not counted: it is who made the call, and a person making this one is
    its new value. A prior that is not a record at all is replaced whole by
    every relabel, so it discards nothing worth naming.
    """
    if not isinstance(prior, dict):
        return []
    defaults = DataClassification()
    discarded = [
        name
        for name in ("regulatory_tags", "jurisdiction")
        if prior.get(name) not in (None, getattr(defaults, name))
    ]
    version = prior.get("classification_version")
    if version not in (None, defaults.classification_version):
        discarded.append("classification_version")
    discarded.extend(
        sorted(str(k) for k in prior if k not in DataClassification.model_fields)
    )
    return discarded


class ClassificationSetHandler:
    """Relabel one document's sensitivity, leaving its content untouched (#194).

    A metadata-only write: ``put_document(..., preserve_updated_at=True)``, so
    the row's clock is not bumped, and ``put_document`` mirrors
    ``data_classification`` onto the document's own vector row (it is in
    ``MIRRORED_METADATA_KEYS``), so a semantic hit stops carrying the old
    label. A chunk reads its parent's label (``effective_sensitivity``), so
    relabelling the parent relabels every chunk with no write to them.

    A document above ``clearance`` answers exactly as a missing one
    (``target_not_found``, via
    :func:`~trellis.mutate.sensitivity_floor.visible_document`), so a key that
    cannot read it cannot learn it exists by trying to relabel it.
    The new label must be within ``clearance`` too
    (:func:`~trellis.mutate.sensitivity_floor.refuse_written_label`), so a caller
    cannot push a document out of its own reach. Re-applying the stored label is
    a no-op, but only a record that states the level counts: a record that cannot
    be parsed reads ``restricted`` without saying so, and a relabel to
    ``restricted`` replaces it with a clean record (the same governed write as
    any other relabel, ``updated_at`` preserved).

    **``public`` is the exact record, and the handler says when it is not.** An
    ``external`` key reads a document only when its stored record is exactly
    :func:`~trellis.schemas.classification.public_label_record`, so a relabel
    to ``public`` writes that record. A document whose record merely reads
    ``public`` (a corpus file's bare frontmatter, a record another classifier
    wrote) is *rewritten*, never reported as unchanged: it is unserved until it
    is, and a success that changes nothing would leave it so. A record that
    carries tags or other fields the exact record would discard is refused
    (``classification_public_discards``) with the names of those fields rather
    than cleared silently. Only the stored record being the exact one is a
    no-op.
    """

    def __init__(
        self, registry: StoreRegistry, *, clearance: Sensitivity = "restricted"
    ) -> None:
        self._registry = registry
        self._clearance = clearance

    def handle(self, command: Command) -> tuple[str | None, str]:
        doc_id = command.args.get("doc_id")
        sensitivity = command.args.get("sensitivity")
        if not isinstance(doc_id, str) or not doc_id:
            msg = "classification.set doc_id must be a non-empty string"
            raise ValidationError(msg, errors=[msg], code="classification_set_shape")
        if not isinstance(sensitivity, str) or sensitivity not in SENSITIVITY_ORDER:
            msg = (
                "classification.set sensitivity must be one of "
                f"{', '.join(SENSITIVITY_ORDER)}"
            )
            raise ValidationError(msg, errors=[msg], code="classification_set_shape")

        document_store = self._registry.knowledge.document_store
        existing = visible_document(document_store, doc_id, clearance=self._clearance)
        if existing is None:
            msg = f"document {doc_id!r} not found"
            raise ValidationError(msg, errors=[msg], code="target_not_found")
        # Deferred: ``trellis.ingest_corpus`` pulls the sync stack.
        from trellis.ingest_corpus.models import is_chunk_doc_id  # noqa: PLC0415

        if is_chunk_doc_id(doc_id):
            msg = (
                f"document {doc_id!r} is a chunk and reads its parent's label; "
                "relabel the parent document instead"
            )
            raise ValidationError(msg, errors=[msg], code="classification_set_chunk")
        try:
            refuse_written_label(
                doc_id,
                sensitivity,  # type: ignore[arg-type]
                clearance=self._clearance,
            )
        except OverwriteRefusedError as exc:
            raise PolicyViolationError(
                exc.message, policy_id=SENSITIVITY_FLOOR_POLICY_ID
            ) from exc

        metadata: dict[str, Any] = dict(existing.get("metadata") or {})
        previous = sensitivity_of(metadata)
        prior_record = metadata.get(DATA_CLASSIFICATION_KEY)
        had_record = DATA_CLASSIFICATION_KEY in metadata
        if sensitivity == "public":
            if is_public_label(metadata):
                return doc_id, f"Classification unchanged: {doc_id} is {sensitivity}"
            discarded = _public_record_discards(prior_record)
            if discarded:
                msg = (
                    f"document {doc_id!r} carries {', '.join(discarded)} in its "
                    "classification record, which the exact public record an "
                    "external key reads does not hold; writing it would discard "
                    "them. Nothing was written."
                )
                raise ValidationError(
                    msg, errors=[msg], code="classification_public_discards"
                )
            metadata[DATA_CLASSIFICATION_KEY] = public_label_record()
        else:
            # A record that cannot be parsed reads ``restricted`` without anyone
            # having chosen it, so ``previous == sensitivity`` is not enough to
            # call a relabel to ``restricted`` a no-op: only a record that states
            # the level is. An operator who asks for ``restricted`` on such a row
            # means it, and gets a record that says so. A record a regulatory-tag
            # write created states ``internal`` without anyone having chosen it
            # either, so confirming ``internal`` on it records the person's call.
            states_it = (
                isinstance(prior_record, dict)
                and prior_record.get("sensitivity") == sensitivity
                and not is_tag_only_record(prior_record)
            )
            if states_it:
                return doc_id, f"Classification unchanged: {doc_id} is {sensitivity}"
            metadata[DATA_CLASSIFICATION_KEY] = _relabelled_record(
                prior_record, sensitivity
            )
        put_document(
            document_store,
            resolve_vector_store(self._registry),
            doc_id,
            str(existing.get("content") or ""),
            metadata,
            preserve_updated_at=True,
        )
        rewritten = _same_level_suffix(
            sensitivity, previous, prior_record, had_record=had_record
        )
        return (
            doc_id,
            f"Classification set: {doc_id} {previous} -> {sensitivity}{rewritten}",
        )


def _same_level_suffix(
    sensitivity: str, previous: str, prior_record: object, *, had_record: bool
) -> str:
    """What a relabel to the level a document already read actually rewrote.

    Only a relabel to public rewrites into the exact record; any other target
    keeps the prior fields, so that suffix would state something false there.
    Below public, a same-level write replaced either an unreadable record or a
    tag-only one (:func:`~trellis.schemas.classification.is_tag_only_record`).
    """
    if previous != sensitivity:
        return ""
    if sensitivity == "public":
        return " (record rewritten to the exact public record)"
    if not had_record:
        return ""
    if is_tag_only_record(prior_record):
        return " (tag-only record confirmed as a label)"
    return " (unreadable record replaced)"


#: Bounds on a regulatory tag set. The vocabulary is open (``pii``, ``phi``,
#: ``gdpr``, ``pci-dss``, whatever an owner uses), so these cap size and shape,
#: never the words.
MAX_REGULATORY_TAGS = 32


MAX_REGULATORY_TAG_LENGTH = 64


def _check_one_regulatory_tag(item: object) -> tuple[str, str | None]:
    """One tag stripped and lower-cased, or why it is not a tag."""
    if not isinstance(item, str):
        return "", "every tag must be a string"
    tag = item.strip().lower()
    problem: str | None = None
    if not tag:
        problem = "a tag may not be empty"
    elif len(tag) > MAX_REGULATORY_TAG_LENGTH:
        problem = f"a tag may not exceed {MAX_REGULATORY_TAG_LENGTH} characters"
    elif not tag.isprintable():
        problem = "a tag may not contain a non-printable character"
    return tag, problem


def _check_regulatory_tags(raw: object) -> tuple[list[str], str | None]:
    """``raw`` as sorted, de-duplicated lower-case tags, or why it is not a tag set.

    Each tag is stripped and lower-cased, so ``"PII"``, ``" pii "`` and ``"pii"``
    are one tag. The problem is a fixed sentence (it never quotes the caller's
    value), for a value that is not a list, a tag that is not a string, one that
    is empty, over :data:`MAX_REGULATORY_TAG_LENGTH`, or holding a non-printable
    character, and for more than :data:`MAX_REGULATORY_TAGS` distinct tags.
    """
    if not isinstance(raw, (list, tuple)):
        return [], "tags must be a list of strings"
    tags: set[str] = set()
    for item in raw:
        tag, problem = _check_one_regulatory_tag(item)
        if problem is not None:
            return [], problem
        tags.add(tag)
    if len(tags) > MAX_REGULATORY_TAGS:
        return [], f"a document may carry at most {MAX_REGULATORY_TAGS} regulatory tags"
    return sorted(tags), None


def normalize_regulatory_tags(raw: object) -> list[str]:
    """``raw`` as a sorted, de-duplicated list of lower-case tags.

    Raises ``ValueError`` (with a message safe to show a caller) for anything
    :func:`_check_regulatory_tags` reports as a problem.
    """
    tags, problem = _check_regulatory_tags(raw)
    if problem is not None:
        raise ValueError(problem)
    return tags


def _stored_tags(record: dict[str, Any]) -> list[str] | None:
    """The record's ``regulatory_tags`` when it is a list of strings, else ``None``."""
    stored = record.get("regulatory_tags")
    if isinstance(stored, list) and all(isinstance(t, str) for t in stored):
        return stored
    return None


class ClassificationTagRegulatoryHandler:
    """Set one document's ``regulatory_tags`` without changing who may read it.

    The tags are a label *about* the content (``pii``, ``phi``), not a limit on
    who reads it, so this write must never move a document in or out of any
    caller's reach. It writes one field of the stored
    :class:`~trellis.schemas.classification.DataClassification` record and
    carries every other key of that record forward untouched (``sensitivity``,
    ``jurisdiction``, ``classification_version``, ``classified_by``, an unknown
    key). It never claims a person made a sensitivity call: on a document with a
    record it leaves ``classified_by`` alone, and on one with none it writes
    ``classified_by=[REGULATORY_TAG_CLASSIFIER]``, which the label proposer reads
    as "no one has labelled this" (``classification.set`` still records
    ``"caller"`` when a person relabels).

    ``tags`` replaces the document's set (the other tags are not merged in), and
    ``clear`` with an empty ``tags`` removes it. Re-applying the stored set is a
    no-op that writes nothing.

    **What is refused, and why.** An ``external`` key is served a document only
    when its record is *exactly* the one
    :func:`~trellis.schemas.classification.public_label_record` returns, so a tag
    on a ``public`` document would withhold it from that key. Such a document is
    refused (``regulatory_tags_public``), whether or not its record is the exact
    one. A record that is not an object, or whose ``sensitivity``
    names no level, reads ``restricted`` without anyone having chosen it, and
    this write will not paper over it (``regulatory_tags_corrupt_record``);
    repair it with ``classification.set``. Every other label (``internal``,
    ``confidential``, ``restricted``, and an unlabelled document, which reads
    ``internal``) keeps the level it had, so its clearance is the same before
    and after. A document with no record gets one saying ``internal``, the level
    it already read. A chunk reads its parent's label, so it is refused too.

    A metadata-only write (``put_document(..., preserve_updated_at=True)``): the
    content and the row's clock are untouched, and ``put_document`` re-mirrors
    ``data_classification`` onto the document's vector row. A document above
    ``clearance`` answers as a missing one, as it does for ``classification.set``.

    **What it does not promise.** Which documents a key can read does not move.
    Like every metadata-only write (a relabel included), re-writing the row can
    reorder documents whose ranks tie exactly (SQLite FTS deletes and inserts the
    row, the vector store replaces it, and the ranking queries have no tiebreak),
    so a pack sitting at a per-strategy fetch boundary can serve the other tied
    item.
    """

    def __init__(
        self, registry: StoreRegistry, *, clearance: Sensitivity = "restricted"
    ) -> None:
        self._registry = registry
        self._clearance = clearance

    def _parse(self, command: Command) -> tuple[str, list[str], bool]:
        doc_id = command.args.get("doc_id")
        clear = command.args.get("clear", False)
        if not isinstance(doc_id, str) or not doc_id:
            msg = "classification.tag_regulatory doc_id must be a non-empty string"
            raise ValidationError(msg, errors=[msg], code="regulatory_tags_shape")
        if not isinstance(clear, bool):
            msg = "classification.tag_regulatory clear must be a boolean"
            raise ValidationError(msg, errors=[msg], code="regulatory_tags_shape")
        tags, problem = _check_regulatory_tags(command.args.get("tags"))
        if problem is not None:
            msg = f"classification.tag_regulatory {problem}"
            raise ValidationError(msg, errors=[msg], code="regulatory_tags_shape")
        if clear and tags:
            msg = "classification.tag_regulatory takes tags or clear, not both"
            raise ValidationError(msg, errors=[msg], code="regulatory_tags_shape")
        if not clear and not tags:
            msg = "classification.tag_regulatory needs at least one tag, or clear"
            raise ValidationError(msg, errors=[msg], code="regulatory_tags_shape")
        return doc_id, tags, clear

    def handle(self, command: Command) -> tuple[str | None, str]:
        doc_id, tags, _clear = self._parse(command)

        document_store = self._registry.knowledge.document_store
        existing = visible_document(document_store, doc_id, clearance=self._clearance)
        if existing is None:
            msg = f"document {doc_id!r} not found"
            raise ValidationError(msg, errors=[msg], code="target_not_found")
        # Deferred: ``trellis.ingest_corpus`` pulls the sync stack.
        from trellis.ingest_corpus.models import is_chunk_doc_id  # noqa: PLC0415

        if is_chunk_doc_id(doc_id):
            msg = (
                f"document {doc_id!r} is a chunk and reads its parent's label; "
                "tag the parent document instead"
            )
            raise ValidationError(msg, errors=[msg], code="regulatory_tags_chunk")

        stored_metadata = existing.get("metadata")
        if label_is_unreadable(stored_metadata):
            msg = (
                f"document {doc_id!r} has metadata that is not an object, so its "
                "label cannot be read. Nothing was written."
            )
            raise ValidationError(
                msg, errors=[msg], code="regulatory_tags_corrupt_record"
            )
        metadata: dict[str, Any] = dict(stored_metadata or {})

        if DATA_CLASSIFICATION_KEY not in metadata:
            if not tags:
                return doc_id, f"Regulatory tags unchanged: {doc_id} has none"
            previous: list[str] = []
            record: dict[str, Any] = DataClassification(
                sensitivity="internal",
                regulatory_tags=tags,
                classified_by=[REGULATORY_TAG_CLASSIFIER],
            ).model_dump(mode="json")
        else:
            prior = metadata[DATA_CLASSIFICATION_KEY]
            level = prior.get("sensitivity") if isinstance(prior, dict) else None
            if not isinstance(prior, dict) or level not in SENSITIVITY_ORDER:
                msg = (
                    f"document {doc_id!r} has a classification record that names "
                    "no sensitivity level, so it reads as restricted without "
                    "anyone having chosen that. Nothing was written; repair it "
                    "with classification.set first."
                )
                raise ValidationError(
                    msg, errors=[msg], code="regulatory_tags_corrupt_record"
                )
            if level == "public":
                msg = (
                    f"document {doc_id!r} is labelled public. An external key "
                    "reads a public document only while its classification "
                    "record is exactly the one a public label writes, so a "
                    "regulatory tag would withhold it from that key. Nothing "
                    "was written."
                )
                raise ValidationError(msg, errors=[msg], code="regulatory_tags_public")
            stored = _stored_tags(prior)
            if stored == tags:
                return doc_id, (
                    f"Regulatory tags unchanged: {doc_id} is {_tag_list(tags)}"
                )
            previous = stored or []
            record = {**prior, "regulatory_tags": tags}

        metadata[DATA_CLASSIFICATION_KEY] = record
        put_document(
            document_store,
            resolve_vector_store(self._registry),
            doc_id,
            str(existing.get("content") or ""),
            metadata,
            preserve_updated_at=True,
        )
        return doc_id, (
            f"Regulatory tags set: {doc_id} {_tag_list(previous)} -> {_tag_list(tags)}"
        )


def _tag_list(tags: list[str]) -> str:
    """``tags`` for a message: the names, or ``none`` for an empty set."""
    return ", ".join(tags) if tags else "none"
