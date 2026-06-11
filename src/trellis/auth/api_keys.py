"""API-key token generation, parsing, scopes, and verification.

Token format::

    trellis_ak_<key_id>.<secret>

``key_id`` is 12 hex chars (``secrets.token_hex(6)``) and is the
lookup key in the :class:`~trellis.stores.base.api_key.ApiKeyStore`.
``secret`` is a ``secrets.token_urlsafe(32)`` string whose SHA-256 hex
digest is the only thing persisted — the full token is returned once
from :func:`generate_api_key` and is unrecoverable afterwards.

Scopes are a small closed set (``read`` / ``ingest`` / ``mutate`` /
``admin``); ``admin`` implies every other scope. The REST layer maps
each router to one required scope — see ``trellis_api.app``.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING

import structlog

from trellis.stores.base.api_key import ApiKeyRecord

if TYPE_CHECKING:
    from collections.abc import Iterable

    from trellis.stores.base.api_key import ApiKeyStore

logger = structlog.get_logger(__name__)

#: Read-only retrieval surfaces (search, packs, entity reads).
SCOPE_READ = "read"
#: Trace / evidence / vector ingestion surfaces.
SCOPE_INGEST = "ingest"
#: Governed mutations (commands, curate, extract drafts).
SCOPE_MUTATE = "mutate"
#: Administrative surfaces (stats, policies, maintenance). Implies all
#: other scopes — see :func:`scopes_satisfy`.
SCOPE_ADMIN = "admin"

ALL_SCOPES: frozenset[str] = frozenset(
    {SCOPE_READ, SCOPE_INGEST, SCOPE_MUTATE, SCOPE_ADMIN}
)

#: Leading marker on every Trellis API token. Lets log scrubbers and
#: secret scanners recognise the credential shape.
TOKEN_PREFIX = "trellis_ak_"  # noqa: S105 — public prefix, not a secret

_KEY_ID_HEX_CHARS = 12
_HEX_DIGITS = frozenset("0123456789abcdef")


def scopes_satisfy(granted: frozenset[str], required: str) -> bool:
    """Return ``True`` when ``granted`` covers the ``required`` scope.

    ``admin`` implies every other scope. Raises :class:`ValueError`
    when ``required`` is not a known scope — a typo'd scope name at a
    call site must fail loudly, not silently deny (or allow) requests.
    """
    if required not in ALL_SCOPES:
        msg = f"Unknown scope {required!r}; known scopes: {sorted(ALL_SCOPES)}"
        raise ValueError(msg)
    return required in granted or SCOPE_ADMIN in granted


def hash_secret(secret: str) -> str:
    """SHA-256 hex digest of the secret half of a token."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_api_key(name: str, scopes: Iterable[str]) -> tuple[str, ApiKeyRecord]:
    """Mint a new API key.

    Returns ``(token, record)``. The token is shown to the caller once
    and never persisted; the record carries only ``sha256(secret)``.
    Raises :class:`ValueError` on an empty name, empty scope set, or
    any scope outside :data:`ALL_SCOPES` — loud on misuse, no silent
    scope-dropping.
    """
    clean_name = name.strip()
    if not clean_name:
        msg = "API key name must be a non-empty string"
        raise ValueError(msg)
    scope_tuple = tuple(dict.fromkeys(scopes))  # de-dupe, keep order
    if not scope_tuple:
        msg = "API key must carry at least one scope"
        raise ValueError(msg)
    unknown = [s for s in scope_tuple if s not in ALL_SCOPES]
    if unknown:
        msg = f"Unknown scope(s) {unknown!r}; known scopes: {sorted(ALL_SCOPES)}"
        raise ValueError(msg)

    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    token = f"{TOKEN_PREFIX}{key_id}.{secret}"
    record = ApiKeyRecord(
        key_id=key_id,
        name=clean_name,
        scopes=scope_tuple,
        secret_hash=hash_secret(secret),
    )
    logger.info(
        "api_key.generated",
        key_id=key_id,
        name=clean_name,
        scopes=list(scope_tuple),
    )
    return token, record


def parse_token(token: str) -> tuple[str, str] | None:
    """Split a presented token into ``(key_id, secret)``.

    Returns ``None`` on any malformed shape (wrong prefix, missing
    separator, bad key_id length/charset, empty secret) — the caller
    decides how loud to be about it.
    """
    if not token.startswith(TOKEN_PREFIX):
        return None
    body = token[len(TOKEN_PREFIX) :]
    key_id, sep, secret = body.partition(".")
    if not sep or not secret:
        return None
    if len(key_id) != _KEY_ID_HEX_CHARS or not set(key_id) <= _HEX_DIGITS:
        return None
    return key_id, secret


def verify_token(token: str, store: ApiKeyStore) -> ApiKeyRecord | None:
    """Verify a presented token against the store.

    Returns the live :class:`ApiKeyRecord` on success, ``None`` on any
    failure. The failure *category* (malformed / unknown / revoked /
    mismatch) is emitted as a structured server-side log only — the
    HTTP layer must answer with an undifferentiated 401 so callers
    cannot probe which key ids exist or are revoked.
    """
    parsed = parse_token(token)
    if parsed is None:
        logger.warning("api_key.verify_failed", reason="malformed")
        return None
    key_id, secret = parsed
    record = store.get(key_id)
    if record is None:
        logger.warning("api_key.verify_failed", reason="unknown", key_id=key_id)
        return None
    if record.revoked_at is not None:
        logger.warning("api_key.verify_failed", reason="revoked", key_id=key_id)
        return None
    if not secrets.compare_digest(hash_secret(secret), record.secret_hash):
        logger.warning("api_key.verify_failed", reason="mismatch", key_id=key_id)
        return None
    return record
