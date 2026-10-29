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
from trellis.retrieve.sensitivity import OverwriteRefusedError, refuse_overwrite
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
        raise HTTPException(status_code=409, detail=str(exc)) from exc
