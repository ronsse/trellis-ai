"""ApiKeyStore — abstract interface for scoped REST API credentials."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import Field

from trellis.core.base import TrellisModel, utc_now


class ApiKeyRecord(TrellisModel):
    """One scoped API credential as persisted by the store.

    Only the SHA-256 hex digest of the secret half of the token is
    stored — the full token (``trellis_ak_<key_id>.<secret>``) is shown
    exactly once at creation time and is unrecoverable afterwards.
    ``revoked_at`` doubles as the revocation flag: ``None`` means the
    key is live.
    """

    key_id: str
    name: str
    scopes: tuple[str, ...]
    secret_hash: str
    created_at: datetime = Field(default_factory=utc_now)
    revoked_at: datetime | None = None


class ApiKeyStore(ABC):
    """Persistence for scoped REST API credentials (operational plane).

    Records are append-then-revoke: ``create`` inserts a new key,
    ``revoke`` closes it by stamping ``revoked_at``. There is no update
    or delete — a compromised or stale key is revoked and a new one is
    minted, which keeps the audit trail intact.
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
    def close(self) -> None:
        """Cleanup."""
