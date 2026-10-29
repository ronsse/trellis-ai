"""The one place a REST route turns an authenticated principal into an
audit-trail ``requested_by``.

Every route that lets a request body claim a ``requested_by`` -- promote,
demote, reinstate, bulk ingest, extract/drafts and ``/commands/batch`` --
resolves it through :func:`resolve_requested_by` instead of passing the
claim straight to ``Command(...)``: with an authenticated principal (a
real key, or the legacy shared secret) the audit trail's ``requested_by``
is always derived from that principal, never from the body, and the
body's own claim survives only as a separate, clearly-labelled
``requested_by_claim``. ``tests/unit/api/test_requested_by_principal_rule.py``
statically enforces that every such route call site resolves through this
module rather than reintroducing a body passthrough.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from trellis.auth import clearance_of
from trellis.core.elision import elide_text
from trellis.core.text_safety import replace_nul_and_lone_surrogates
from trellis.retrieve.sensitivity import (
    DocumentClearance,
    OverwriteRefusedError,
    SensitivityLabelError,
    refuse_overwrite,
    stamp_sensitivity,
)
from trellis_api.auth import AuthContext

if TYPE_CHECKING:
    from collections.abc import Mapping

    from trellis.schemas.classification import Sensitivity
    from trellis.stores.base.document import DocumentStore

#: ``requested_by_claim`` is attacker-controlled free text, so it is bounded
#: the same way a pack excerpt is -- long enough to be useful on an audit
#: event, short enough that one claim cannot inflate the event without
#: limit.
_CLAIM_MAX_CHARS = 200


def _scrub_claim(claim: str) -> str:
    """Make *claim* safe to carry in ``Command.metadata`` verbatim.

    Unlike ``requested_by`` itself (always ``api:<principal>`` once a key
    is present), nothing validates ``requested_by_claim`` before it
    reaches here -- it is exactly what the body sent. A NUL byte or an
    unpaired UTF-16 surrogate makes it unwritable to a Postgres ``jsonb``
    column, the same defect :func:`trellis.mutate.handlers._find_unsafe_jsonb_string`
    and :mod:`trellis_api.routes.classify_rules` guard against, so each
    offending character is replaced with U+FFFD -- preserving the
    claim's length and position rather than dropping or rejecting it,
    since this is an audit trail annotating a request, not a value the
    caller's request depends on succeeding. Length is capped *after*
    scrubbing, via :func:`elide_text`, so a cut can never land on a
    surrogate pair that arrived already merged into one code point and
    manufacture a fresh lone surrogate at the boundary.
    """
    return elide_text(replace_nul_and_lone_surrogates(claim), _CLAIM_MAX_CHARS)


def resolve_requested_by(
    ctx: AuthContext, claimed: str, *, claim_was_set: bool
) -> tuple[str, str | None]:
    """The audit ``requested_by`` for this request, and the body's own claim.

    When a principal is authenticated (a real key, or the legacy shared
    secret), the audit label is derived from it -- ``api:<key name or
    id>`` -- never from the caller-chosen *claimed* body value, so a
    careless or hostile body cannot forge who the audit trail blames.
    With no principal (auth mode ``off``, or ``optional`` with no
    credential presented) there is nothing to derive from, so current
    behaviour is unchanged: *claimed* passes through verbatim and the
    second element is ``None``.

    *claim_was_set* is required, not defaulted -- every caller must say
    whether the body actually supplied ``requested_by``
    (``"requested_by" in req.model_fields_set``, or the route's own
    equivalent for a field that falls back to something other than the
    request model's own default) rather than silently inheriting a
    "the caller claimed it" assumption. Pass ``False`` for a defaulted
    value, and the second element of the return is then ``None`` even
    with a principal present -- a DTO default such as ``"api:promote"``
    is not something the caller claimed, and recording it as
    ``requested_by_claim`` would blame the body for a value it never
    sent.

    When a claim is recorded, it is bounded and scrubbed first (see
    :func:`_scrub_claim`) -- the claim rides into ``Command.metadata``
    on every caller's audit event, so this is the one place to do it
    rather than each route remembering to.
    """
    principal = ctx.key_id or ctx.name
    if principal is None:
        return claimed, None
    if not claim_was_set:
        return f"api:{principal}", None
    return f"api:{principal}", _scrub_claim(claimed)


def request_clearance(ctx: AuthContext) -> Sensitivity:
    """The highest document sensitivity this REST caller may read (#194).

    From the principal's scopes. Auth mode ``off`` (and the legacy shared
    secret) grant ``ALL_SCOPES``, so a trusted-local deployment reads
    everything; a key without ``confidential``/``restricted`` reads up to
    ``internal``.
    """
    return clearance_of(ctx.scopes)


def refuse_document_overwrite(
    document_store: DocumentStore,
    doc_id: str,
    *,
    ctx: AuthContext,
    metadata: Mapping[str, Any] | None,
) -> None:
    """Raise 409 when re-putting ``doc_id`` would destroy or downgrade a label (#194).

    The caller's clearance comes from ``ctx``; see
    :func:`trellis.retrieve.sensitivity.refuse_overwrite` for the rule.
    """
    try:
        refuse_overwrite(
            document_store,
            doc_id,
            clearance=request_clearance(ctx),
            metadata=metadata,
        )
    except OverwriteRefusedError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc


def refuse_vector_write(
    document_store: DocumentStore,
    item_id: str,
    *,
    ctx: AuthContext,
) -> None:
    """Raise 409 when a vector write would touch a document above clearance (#1029).

    A vector row mirrors its document's label and carries the excerpt a
    semantic hit shows, so replacing the row of a document the key may not read
    is a write to that document's retrieval, whether or not it stores any text.
    ``item_id`` is judged as every read surface judges a document id: a chunk id
    by the higher of its own label and its parent's, whether or not its own row
    exists, and an unreadable parent as above clearance. An id that names no
    document at all is no verdict (the route stores vectors for ids that are not
    documents). The route has no "missing document" answer to give, so the
    refusal is the overwrite refusal (409) and, like it, tells a key that the id
    exists.
    """
    clearance = request_clearance(ctx)
    if clearance == "restricted":
        return
    existing = document_store.get(item_id)
    if not DocumentClearance(document_store, clearance).permits(
        item_id, existing.get("metadata") if existing is not None else None
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"document {item_id!r} is above this credential's clearance; "
                "nothing was written"
            ),
        )


def stamp_request_sensitivity(
    metadata: Mapping[str, Any] | None,
    sensitivity: str | None,
    *,
    ctx: AuthContext,
) -> dict[str, Any]:
    """The metadata to store, carrying the caller's ``sensitivity`` label (#194).

    The rule is :func:`trellis.retrieve.sensitivity.stamp_sensitivity`, the one
    the MCP save tools apply. A label above the caller's clearance answers 403
    (the key lacks the scope); an unknown label, or a raw ``data_classification``
    key in ``metadata``, answers 422. Nothing is written in either case.
    """
    try:
        return stamp_sensitivity(metadata, sensitivity, request_clearance(ctx))
    except SensitivityLabelError as exc:
        raise HTTPException(
            status_code=403 if exc.above_clearance else 422, detail=exc.message
        ) from exc
