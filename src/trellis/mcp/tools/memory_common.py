"""Helpers ``save_memory`` and ``save_knowledge`` share.

Sensitivity stamping, pointer-key refusal and readable-document matching.
"""

from __future__ import annotations

from typing import Any

from trellis.mcp.auth import current_clearance
from trellis.mcp.boundary import _raise_invalid_params
from trellis.mutate.evidence_pointer_keys import pointer_key_refusal
from trellis.retrieve.embed_ingest_hook import EMBED_INPUT_CHAR_CAP
from trellis.retrieve.reference import (
    REFERENCE_FORM,
    SUMMARY_KEY,
    URI_KEY,
    summary_refusal,
)
from trellis.retrieve.sensitivity import (
    DocumentClearance,
    SensitivityLabelError,
    stamp_sensitivity,
)
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    sensitivity_of,
    sensitivity_rank,
)
from trellis.schemas.document_metadata import document_form_of
from trellis.stores.registry import StoreRegistry

# ---------------------------------------------------------------------------
# Macro Tool 4: save_memory
# ---------------------------------------------------------------------------


def _stamp_sensitivity(
    metadata: dict[str, Any] | None, sensitivity: str | None, *, tool: str
) -> dict[str, Any]:
    """The metadata to store, carrying the caller's ``sensitivity`` label (#194).

    The rule is :func:`trellis.retrieve.sensitivity.stamp_sensitivity`, shared
    with the REST producers; this wrapper only maps its refusal to this
    surface's invalid-params error.
    """
    try:
        return stamp_sensitivity(metadata, sensitivity, current_clearance())
    except SensitivityLabelError as exc:
        _raise_invalid_params(exc.message, data={**exc.data, "tool": tool})


def _refuse_memory_pointer_keys(metadata: dict[str, Any] | None) -> None:
    """Refuse ``save_memory`` metadata that would withhold or redirect another item.

    The pack builder withholds items by id, so a caller's ``lifecycle`` or
    ``evidence_ref`` reaches an item other than the document being saved
    (:mod:`trellis.mutate.evidence_pointer_keys`). The strict form, as on
    ``POST /documents``: either key is refused whatever its value. It judges
    only the metadata the caller sent, so the stamps ``supersedes=`` and the
    reconcile verdicts write through their own commands are untouched.
    Refused before anything is read or written, with the tool's ordinary
    pre-flight ``INVALID_PARAMS``.
    """
    if not metadata:
        return
    refusal = pointer_key_refusal(metadata, strict=True)
    if refusal is not None:
        _raise_invalid_params(
            refusal.message,
            data={"field": f"metadata.{refusal.key}", "tool": "save_memory"},
        )


def _with_reference_fields(
    metadata: dict[str, Any] | None, summary: str | None, uri: str | None
) -> dict[str, Any] | None:
    """``metadata`` with ``save_memory``'s ``summary`` and ``uri`` written into it.

    Both land flat under those keys. A parameter and a metadata key of the same
    name that disagree are refused, and so is a summary the write boundary would
    refuse, here and before anything is read, so an exact-duplicate hit cannot
    hide a bad one. A ``summary`` with no ``document_form`` anywhere in the
    metadata stamps ``document_form: "reference"``, the form that makes the
    summary what is embedded and shown. Neither parameter given: the metadata is
    returned as it came.
    """
    if summary is None and uri is None:
        return metadata
    bag = dict(metadata or {})
    for key, value in ((SUMMARY_KEY, summary), (URI_KEY, uri)):
        if value is None:
            continue
        if key in bag and bag[key] != value:
            _raise_invalid_params(
                f"{key} was given as a parameter and in metadata, and they differ",
                data={"field": key, "tool": "save_memory"},
            )
        bag[key] = value
    if summary is not None and document_form_of(bag) is None:
        bag["document_form"] = REFERENCE_FORM
    refusal = summary_refusal(bag, limit=EMBED_INPUT_CHAR_CAP)
    if refusal is not None:
        _raise_invalid_params(
            refusal, data={"field": SUMMARY_KEY, "tool": "save_memory"}
        )
    return bag


def _match_readable(registry: StoreRegistry, doc_id: str) -> bool:
    """Whether the caller may read document ``doc_id`` (a missing one is fine)."""
    store = registry.knowledge.document_store
    doc = store.get(doc_id)
    if doc is None:
        return True
    return DocumentClearance(store, current_clearance()).permits(
        doc_id, doc.get("metadata")
    )


def _readable_document_exists(registry: StoreRegistry, doc_id: str) -> bool:
    """Whether ``doc_id`` exists and the caller may read it."""
    store = registry.knowledge.document_store
    doc = store.get(doc_id)
    return doc is not None and DocumentClearance(store, current_clearance()).permits(
        doc_id, doc.get("metadata")
    )


def _refuse_label_below_request(
    registry: StoreRegistry, doc_id: str, metadata: dict[str, Any]
) -> None:
    """Refuse a labelled save that an existing, lower-labelled match would swallow.

    Dedup returns the existing document and stores nothing, so the content
    would stay served at the old label. Relabelling is a separate operation.
    An unlabelled save is never refused: it asked for no label.
    """
    if DATA_CLASSIFICATION_KEY not in metadata:
        return
    doc = registry.knowledge.document_store.get(doc_id)
    if doc is None:
        return
    requested = sensitivity_of(metadata)
    if sensitivity_rank(sensitivity_of(doc.get("metadata"))) < sensitivity_rank(
        requested
    ):
        _raise_invalid_params(
            f"an identical or near-identical document already exists at a lower "
            f"sensitivity than {requested!r}; nothing was written and its label "
            "is unchanged (relabelling is not supported here)",
            data={"field": "sensitivity"},
        )


def _append_note(reply: str, note: str | None) -> str:
    """``reply`` with a skipped-step ``note`` on its own line, if there is one."""
    return f"{reply}\n{note}" if note else reply
