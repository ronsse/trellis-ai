"""ApiKeyStore — abstract interface for scoped REST API credentials."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import Field

from trellis.core.base import TrellisModel, utc_now


class Unset:
    """Type of :data:`UNSET`, the "leave this column alone" marker.

    :meth:`ApiKeyStore.update` cannot use ``None`` for that, because ``None``
    already means "clear the column".
    """

    def __repr__(self) -> str:
        return "UNSET"


UNSET = Unset()


class ApiKeyDefaults(TrellisModel):
    """Server-side defaults a key fills into a call that omitted the field.

    Every field is optional and ``None`` means "this key sets no default for
    it". A key with no defaults (every key minted before this block existed) is
    stored with no block at all. ``agent_id`` / ``domain`` / ``session_id_prefix``
    only ever fill a field the call left out; ``team`` is *pinned* (the key's
    team is the team, whatever a caller claims). ``session_id_prefix`` is a
    prefix, not a session id: one fixed id per key would put every conversation
    the key serves into one dedup bucket.
    """

    agent_id: str | None = None
    domain: str | None = None
    team: str | None = None
    session_id_prefix: str | None = None

    def is_empty(self) -> bool:
        """Whether the block sets nothing (and so is stored as no block)."""
        return not any((self.agent_id, self.domain, self.team, self.session_id_prefix))


class ApiKeyRecord(TrellisModel):
    """One scoped API credential as persisted by the store.

    Only the SHA-256 hex digest of the secret half of the token is
    stored — the full token (``trellis_ak_<key_id>.<secret>``) is shown
    exactly once at creation time and is unrecoverable afterwards.
    ``revoked_at`` doubles as the revocation flag: ``None`` means the
    key is live. ``expires_at`` is a second, softer end: a key whose
    ``expires_at`` has passed no longer verifies, and ``None`` (every key
    minted without a grace period) never expires. ``defaults`` is ``None``
    for a key that sets none.
    """

    key_id: str
    name: str
    scopes: tuple[str, ...]
    secret_hash: str
    created_at: datetime = Field(default_factory=utc_now)
    revoked_at: datetime | None = None
    defaults: ApiKeyDefaults | None = None
    expires_at: datetime | None = None


class ApiKeyStore(ABC):
    """Persistence for scoped REST API credentials (operational plane).

    Records are append-then-revoke: ``create`` inserts a new key,
    ``revoke`` closes it by stamping ``revoked_at``. The credential itself
    (name, scopes, secret hash) is never rewritten and there is no delete:
    a compromised or stale key is revoked and a new one is minted, which
    keeps the audit trail intact. Two non-credential columns may change
    through :meth:`update`: the ``defaults`` block and ``expires_at``.
    """

    @abstractmethod
    def create(self, record: ApiKeyRecord) -> ApiKeyRecord:
        """Persist a new API key record.  Returns the stored copy.

        Raises :class:`~trellis.errors.StoreError` when ``key_id``
        already exists — key ids are minted from
        ``secrets.token_hex`` so a collision indicates caller misuse,
        not bad luck.
        """

    @abstractmethod
    def get(self, key_id: str) -> ApiKeyRecord | None:
        """Fetch a record by ``key_id``, or ``None`` when absent."""

    @abstractmethod
    def list(self) -> list[ApiKeyRecord]:
        """Return every record (live and revoked), newest first."""

    @abstractmethod
    def revoke(self, key_id: str) -> bool:
        """Stamp ``revoked_at`` on a live key.

        Returns ``False`` (and logs loudly) when the key is unknown or
        already revoked — callers surface that as an operator error
        rather than treating revocation as idempotent, per the
        loud-on-misuse directive.
        """

    @abstractmethod
    def update(
        self,
        key_id: str,
        *,
        defaults: ApiKeyDefaults | Unset | None = UNSET,
        expires_at: datetime | Unset | None = UNSET,
    ) -> bool:
        """Rewrite ``defaults`` and/or ``expires_at`` on a live key.

        A field left at :data:`UNSET` is untouched; ``None`` clears it. Returns
        ``False`` when the key is unknown or revoked. Never touches the scopes,
        the name or the secret hash.
        """

    @abstractmethod
    def close(self) -> None:
        """Cleanup."""
