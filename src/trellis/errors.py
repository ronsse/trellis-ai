"""Exception hierarchy for Trellis."""

from __future__ import annotations


class TrellisError(Exception):
    """Base exception for all Trellis errors."""

    def __init__(self, message: str, *, code: str = "TRELLIS_ERROR") -> None:
        self.message = message
        self.code = code
        super().__init__(message)


class ValidationError(TrellisError):
    """Raised when input validation fails."""

    def __init__(self, message: str, *, errors: list[str] | None = None) -> None:
        self.errors: list[str] = errors or []
        super().__init__(message, code="VALIDATION_ERROR")


class StoreError(TrellisError):
    """Raised when a storage operation fails."""

    def __init__(self, message: str, *, store: str | None = None) -> None:
        self.store = store
        super().__init__(message, code="STORE_ERROR")


class NotFoundError(StoreError):
    """Raised when an entity is not found."""

    def __init__(self, *, entity_type: str, entity_id: str) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} not found: {entity_id}")
        self.code = "NOT_FOUND"


class MutationError(TrellisError):
    """Raised when a mutation operation fails."""

    def __init__(self, message: str, *, command_id: str | None = None) -> None:
        self.command_id = command_id
        super().__init__(message, code="MUTATION_ERROR")


class PolicyViolationError(MutationError):
    """Raised when a policy is violated."""

    def __init__(self, message: str, *, policy_id: str) -> None:
        self.policy_id = policy_id
        super().__init__(message)
        self.code = "POLICY_VIOLATION"


class ApprovalRequiredError(MutationError):
    """Raised when an approval is needed."""

    def __init__(self, message: str, *, approval_id: str) -> None:
        self.approval_id = approval_id
        super().__init__(message)
        self.code = "APPROVAL_REQUIRED"


class IdempotencyError(MutationError):
    """Raised when a duplicate command is detected."""

    def __init__(self, *, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(f"Duplicate command: {idempotency_key}")
        self.code = "DUPLICATE_COMMAND"
