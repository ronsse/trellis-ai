"""Tests for exception hierarchy."""

from trellis.errors import (
    IdempotencyError,
    MutationError,
    NotFoundError,
    PolicyViolationError,
    TrellisError,
    ValidationError,
)


def test_trellis_error_has_message_and_code():
    err = TrellisError("something broke", code="TEST_ERR")
    assert err.message == "something broke"
    assert err.code == "TEST_ERR"
    assert str(err) == "something broke"


def test_not_found_error_auto_formats_message():
    err = NotFoundError(entity_type="Node", entity_id="abc123")
    assert "Node" in err.message
    assert "abc123" in err.message
    assert err.code == "NOT_FOUND"


def test_idempotency_error_auto_formats_message():
    err = IdempotencyError(idempotency_key="key-42")
    assert "key-42" in err.message
    assert err.code == "DUPLICATE_COMMAND"


def test_validation_error_is_trellis_error():
    err = ValidationError("bad input", errors=["field x required"])
    assert isinstance(err, TrellisError)
    assert err.code == "VALIDATION_ERROR"
    assert err.errors == ["field x required"]


def test_policy_violation_is_mutation_error():
    err = PolicyViolationError("blocked", policy_id="pol-1")
    assert isinstance(err, MutationError)
    assert err.code == "POLICY_VIOLATION"
    assert err.policy_id == "pol-1"
