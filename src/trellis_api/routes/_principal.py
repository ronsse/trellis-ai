"""The one place a REST route turns an authenticated principal into an
audit-trail ``requested_by``.

Lifted from ``routes/mutations.py``'s ``_resolve_requested_by`` (#889),
which got this right for ``/commands/batch`` but left every other
body-supplied ``requested_by`` route to trust the caller: a mutate-scoped
key could ``POST /precedents`` with ``requested_by="auto:learning_candidate"``
and forge the auto-promote origin that ``_was_auto_promoted`` trusts. Every
route that lets a request body claim a ``requested_by`` must resolve it
through :func:`resolve_requested_by` instead of passing the claim straight
to ``Command(...)`` -- see ``tests/unit/api/test_requested_by_principal_rule.py``.
"""

from __future__ import annotations

from trellis.core.elision import elide_text
from trellis.mutate.handlers import _contains_nul_or_lone_surrogate
from trellis_api.auth import AuthContext

#: ``requested_by_claim`` is attacker-controlled free text, so it is bounded
#: the same way a pack excerpt is -- long enough to be useful on an audit
#: event, short enough that one claim cannot inflate the event without
#: limit.
_CLAIM_MAX_CHARS = 200

_REPLACEMENT_CHAR = "�"


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
    scrubbed = "".join(
        _REPLACEMENT_CHAR if _contains_nul_or_lone_surrogate(ch) else ch for ch in claim
    )
    return elide_text(scrubbed, _CLAIM_MAX_CHARS)


def resolve_requested_by(
    ctx: AuthContext, claimed: str, *, claim_was_set: bool = True
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

    *claim_was_set* says whether the body actually supplied
    ``requested_by`` (``"requested_by" in req.model_fields_set``) rather
    than falling back to the request model's own default. A caller
    passes ``False`` for a defaulted value, and the second element of
    the return is then ``None`` even with a principal present -- a DTO
    default such as ``"api:promote"`` is not something the caller
    claimed, and recording it as ``requested_by_claim`` would blame the
    body for a value it never sent.

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
