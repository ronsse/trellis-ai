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

from trellis_api.auth import AuthContext


def resolve_requested_by(ctx: AuthContext, claimed: str) -> tuple[str, str | None]:
    """The audit ``requested_by`` for this request, and the body's own claim.

    When a principal is authenticated (a real key, or the legacy shared
    secret), the audit label is derived from it -- ``api:<key name or
    id>`` -- never from the caller-chosen *claimed* body value, so a
    careless or hostile body cannot forge who the audit trail blames.
    With no principal (auth mode ``off``, or ``optional`` with no
    credential presented) there is nothing to derive from, so current
    behaviour is unchanged: *claimed* passes through verbatim and the
    second element is ``None``.
    """
    principal = ctx.key_id or ctx.name
    if principal is None:
        return claimed, None
    return f"api:{principal}", claimed
