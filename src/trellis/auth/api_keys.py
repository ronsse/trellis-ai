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

Two clearance scopes (``confidential`` / ``restricted``) raise the sensitivity
ceiling above ``internal``, and one (``external``) lowers it to ``public``: an
``external`` key reads only documents someone labelled ``public``, and no
surface that cannot carry a label (see :func:`clearance_of`).
"""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING, Any

import structlog

from trellis.schemas.classification import (
    Sensitivity,
)
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

#: Clearance scopes (#194): not a surface, a ceiling on the sensitivity a
#: caller may read or label. ``restricted`` implies ``confidential``; a key with
#: neither reads documents up to ``internal``. ``admin`` holds both.
SCOPE_CONFIDENTIAL = "confidential"
SCOPE_RESTRICTED = "restricted"

#: The one scope that lowers the ceiling rather than raising it: a key that
#: carries it reads documents labelled ``public`` and nothing else. It narrows
#: ``read``, so a key is minted ``read,external`` (:func:`external_scope_refusal`).
#: It is deliberately *not* in :data:`ALL_SCOPES`: that set is what auth mode
#: ``off``, the legacy shared secret and a no-credential ``optional`` request
#: are granted, and a trusted-local caller must never read as ``external``.
SCOPE_EXTERNAL = "external"

ALL_SCOPES: frozenset[str] = frozenset(
    {
        SCOPE_READ,
        SCOPE_INGEST,
        SCOPE_MUTATE,
        SCOPE_ADMIN,
        SCOPE_CONFIDENTIAL,
        SCOPE_RESTRICTED,
    }
)

#: Every scope a stored key may carry: :data:`ALL_SCOPES` plus ``external``.
#: What a mint validates against; :data:`ALL_SCOPES` stays the grant-everything
#: set.
KEY_SCOPES: frozenset[str] = ALL_SCOPES | {SCOPE_EXTERNAL}

#: Leading marker on every Trellis API token. Lets log scrubbers and
#: secret scanners recognise the credential shape.
TOKEN_PREFIX = "trellis_ak_"  # noqa: S105 — public prefix, not a secret

_KEY_ID_HEX_CHARS = 12
_HEX_DIGITS = frozenset("0123456789abcdef")


def holds_external(granted: Iterable[str]) -> bool:
    """Whether ``granted`` carries ``external``, however it is spelled.

    The one place the narrowing scope is recognised. It ignores case and
    surrounding whitespace, so a stored scope ``External`` (a hand-edited record,
    an import) is still read as ``external``: the lower clearance is the safe
    failure for a scope that is merely mis-spelled. Only ``external`` is
    normalised this way; every other scope is matched exactly, as before.
    """
    return any(
        isinstance(scope, str) and scope.strip().lower() == SCOPE_EXTERNAL
        for scope in granted
    )


def scopes_satisfy(granted: frozenset[str], required: str) -> bool:
    """Return ``True`` when ``granted`` covers the ``required`` scope.

    ``admin`` implies every other scope. Raises :class:`ValueError`
    when ``required`` is not a known scope — a typo'd scope name at a
    call site must fail loudly, not silently deny (or allow) requests.

    A credential that holds ``external`` satisfies ``read`` and nothing else,
    whatever else it holds: ``external`` grants no write, and a stored key that
    somehow carries it beside ``admin`` or ``ingest`` is still a read-only,
    public-clearance key (:func:`external_scope_refusal` stops a mint, this
    covers a record that is already stored).
    """
    if required not in ALL_SCOPES:
        msg = f"Unknown scope {required!r}; known scopes: {sorted(ALL_SCOPES)}"
        raise ValueError(msg)
    if holds_external(granted):
        return required == SCOPE_READ and SCOPE_READ in granted
    if required == SCOPE_CONFIDENTIAL and SCOPE_RESTRICTED in granted:
        return True
    return required in granted or SCOPE_ADMIN in granted


def clearance_of(scopes: Iterable[str]) -> Sensitivity:
    """The highest document sensitivity a credential's ``scopes`` may read.

    ``external`` gives ``public``, and wins over every other scope the key
    holds: the lower clearance applies, whatever the key also carries. Otherwise
    ``admin`` or ``restricted`` gives ``restricted``; ``confidential`` gives
    ``confidential``; anything else gives ``internal``, so a caller without
    ``external`` never sees less than unlabelled content (a document with no
    label reads ``internal``). A ``public`` caller is not shown an unlabelled
    document, and is shown nothing that cannot carry a label at all.
    """
    granted = frozenset(scopes)
    if holds_external(granted):
        return "public"
    if SCOPE_ADMIN in granted or SCOPE_RESTRICTED in granted:
        return "restricted"
    if SCOPE_CONFIDENTIAL in granted:
        return "confidential"
    return "internal"


def external_scope_refusal(scopes: Iterable[str]) -> str | None:
    """Why ``scopes`` may not be minted, when they carry ``external``; else ``None``.

    ``external`` narrows ``read``: the one legal spelling is ``read,external``.
    It cannot sit beside ``confidential`` / ``restricted`` / ``admin`` (a ceiling
    that is both public and higher is not a ceiling) or ``ingest`` / ``mutate``
    (``external`` grants no write), and without ``read`` the key could call
    nothing. The reason is text Trellis wrote, safe to show.
    """
    granted = frozenset(scopes)
    if SCOPE_EXTERNAL not in granted:
        return None
    extra = sorted(granted - {SCOPE_READ, SCOPE_EXTERNAL})
    if extra:
        return (
            f"scope 'external' cannot be combined with {extra}: it caps the key at "
            "documents labelled public and grants no write, so mint it as "
            "'read,external' and nothing else"
        )
    if SCOPE_READ not in granted:
        return (
            "scope 'external' narrows 'read' and grants nothing by itself: "
            "mint it as 'read,external'"
        )
    return None


#: Shown beside a freshly minted token on every surface that mints one.
SHOWN_ONCE_WARNING = (
    "Store this token now - it is shown once and cannot be recovered. "
    "Only a hash of the secret is persisted."
)


def parse_scope_list(scopes: str | Iterable[str]) -> list[str]:
    """Split a comma-separated scope list and validate it; loud on unknowns.

    The one parse every mint surface (``trellis admin api-keys create``, the
    REST route) calls before :func:`generate_api_key`, so a scope that is
    unknown or empty is refused the same way on each. ``external`` beside
    anything but ``read`` is refused by :func:`generate_api_key` itself.
    """
    raw = scopes.split(",") if isinstance(scopes, str) else list(scopes)
    parsed = [s.strip() for s in raw if isinstance(s, str) and s.strip()]
    unknown = [s for s in parsed if s not in KEY_SCOPES]
    if unknown or not parsed:
        msg = (
            f"Unknown or empty scope(s): {unknown or '(none given)'}; "
            f"valid scopes: {sorted(KEY_SCOPES)}"
        )
        raise ValueError(msg)
    return parsed


#: Scope combinations an operator mints most often, in the order a picker shows
#: them. A convenience over :data:`KEY_SCOPES`, not a second vocabulary: every
#: entry passes :func:`generate_api_key` (``tests/unit/api/test_api_keys_routes.py``).
KEY_PRESETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Read only", (SCOPE_READ,)),
    ("Read, public documents only (external)", (SCOPE_READ, SCOPE_EXTERNAL)),
    ("Read and confidential", (SCOPE_READ, SCOPE_CONFIDENTIAL)),
    ("Read and restricted", (SCOPE_READ, SCOPE_RESTRICTED)),
    ("Read and write (ingest, mutate)", (SCOPE_READ, SCOPE_INGEST, SCOPE_MUTATE)),
    ("Admin (implies every other scope)", (SCOPE_ADMIN,)),
)


def api_key_public_row(record: ApiKeyRecord) -> dict[str, Any]:
    """The listable shape of a key: never the secret hash, never any of the secret."""
    return {
        "key_id": record.key_id,
        "name": record.name,
        "scopes": list(record.scopes),
        "created_at": record.created_at.isoformat(),
        "revoked": record.revoked_at is not None,
        "revoked_at": (record.revoked_at.isoformat() if record.revoked_at else None),
    }


def hash_secret(secret: str) -> str:
    """SHA-256 hex digest of the secret half of a token."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_api_key(name: str, scopes: Iterable[str]) -> tuple[str, ApiKeyRecord]:
    """Mint a new API key.

    Returns ``(token, record)``. The token is shown to the caller once
    and never persisted; the record carries only ``sha256(secret)``.
    Raises :class:`ValueError` on an empty name, empty scope set, or
    any scope outside :data:`KEY_SCOPES`, or ``external`` beside anything but
    ``read`` (:func:`external_scope_refusal`) — loud on misuse, no silent
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
    unknown = [s for s in scope_tuple if s not in KEY_SCOPES]
    if unknown:
        msg = f"Unknown scope(s) {unknown!r}; known scopes: {sorted(KEY_SCOPES)}"
        raise ValueError(msg)
    refusal = external_scope_refusal(scope_tuple)
    if refusal is not None:
        raise ValueError(refusal)

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
