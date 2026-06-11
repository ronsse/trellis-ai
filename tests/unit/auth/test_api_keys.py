"""Tests for trellis.auth.api_keys — token mint/parse/verify + scopes."""

from __future__ import annotations

import pytest

from trellis.auth import (
    ALL_SCOPES,
    SCOPE_ADMIN,
    SCOPE_INGEST,
    SCOPE_MUTATE,
    SCOPE_READ,
    TOKEN_PREFIX,
    generate_api_key,
    hash_secret,
    parse_token,
    scopes_satisfy,
    verify_token,
)
from trellis.stores.sqlite.api_key import SQLiteApiKeyStore


@pytest.fixture
def store(tmp_path):
    s = SQLiteApiKeyStore(tmp_path / "api_keys.db")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# generate_api_key
# ---------------------------------------------------------------------------


class TestGenerateApiKey:
    def test_token_shape(self) -> None:
        token, record = generate_api_key("ci", [SCOPE_READ])
        assert token.startswith(TOKEN_PREFIX)
        body = token[len(TOKEN_PREFIX) :]
        key_id, _, secret = body.partition(".")
        assert key_id == record.key_id
        assert len(key_id) == 12
        assert all(c in "0123456789abcdef" for c in key_id)
        assert secret
        assert record.secret_hash == hash_secret(secret)
        assert record.scopes == (SCOPE_READ,)
        assert record.revoked_at is None

    def test_secret_never_on_record(self) -> None:
        token, record = generate_api_key("ci", [SCOPE_READ])
        secret = token.split(".", 1)[1]
        assert secret not in record.secret_hash
        assert token not in record.model_dump_json()

    def test_unknown_scope_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown scope"):
            generate_api_key("ci", ["write"])

    def test_empty_scopes_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one scope"):
            generate_api_key("ci", [])

    def test_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            generate_api_key("   ", [SCOPE_READ])

    def test_duplicate_scopes_deduped(self) -> None:
        _, record = generate_api_key("ci", [SCOPE_READ, SCOPE_READ, SCOPE_INGEST])
        assert record.scopes == (SCOPE_READ, SCOPE_INGEST)


# ---------------------------------------------------------------------------
# parse_token
# ---------------------------------------------------------------------------


class TestParseToken:
    def test_round_trip(self) -> None:
        token, record = generate_api_key("ci", [SCOPE_READ])
        parsed = parse_token(token)
        assert parsed is not None
        key_id, secret = parsed
        assert key_id == record.key_id
        assert hash_secret(secret) == record.secret_hash

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "garbage",
            "trellis_ak_",  # no body
            "trellis_ak_abcdef123456",  # no separator
            "trellis_ak_abcdef123456.",  # empty secret
            "trellis_ak_short.secret",  # key_id wrong length
            "trellis_ak_ABCDEF123456.secret",  # uppercase not hex per token_hex
            "Bearer trellis_ak_abcdef123456.secret",  # scheme not stripped
            "other_prefix_abcdef123456.secret",
        ],
    )
    def test_malformed_returns_none(self, bad: str) -> None:
        assert parse_token(bad) is None


# ---------------------------------------------------------------------------
# verify_token
# ---------------------------------------------------------------------------


class TestVerifyToken:
    def test_round_trip_against_store(self, store: SQLiteApiKeyStore) -> None:
        token, record = generate_api_key("ci", [SCOPE_READ, SCOPE_INGEST])
        store.create(record)
        verified = verify_token(token, store)
        assert verified is not None
        assert verified.key_id == record.key_id
        assert verified.scopes == (SCOPE_READ, SCOPE_INGEST)

    def test_wrong_secret_rejected(self, store: SQLiteApiKeyStore) -> None:
        token, record = generate_api_key("ci", [SCOPE_READ])
        store.create(record)
        tampered = f"{TOKEN_PREFIX}{record.key_id}.not-the-secret"
        assert verify_token(tampered, store) is None
        assert token != tampered

    def test_revoked_rejected(self, store: SQLiteApiKeyStore) -> None:
        token, record = generate_api_key("ci", [SCOPE_READ])
        store.create(record)
        assert store.revoke(record.key_id) is True
        assert verify_token(token, store) is None

    def test_unknown_key_id_rejected(self, store: SQLiteApiKeyStore) -> None:
        token, _record = generate_api_key("ci", [SCOPE_READ])
        # Never stored.
        assert verify_token(token, store) is None

    def test_malformed_rejected(self, store: SQLiteApiKeyStore) -> None:
        assert verify_token("garbage", store) is None


# ---------------------------------------------------------------------------
# scopes_satisfy
# ---------------------------------------------------------------------------


class TestScopesSatisfy:
    @pytest.mark.parametrize("required", sorted(ALL_SCOPES))
    def test_admin_implies_all(self, required: str) -> None:
        assert scopes_satisfy(frozenset({SCOPE_ADMIN}), required) is True

    @pytest.mark.parametrize(
        ("granted", "required", "expected"),
        [
            (frozenset({SCOPE_READ}), SCOPE_READ, True),
            (frozenset({SCOPE_READ}), SCOPE_INGEST, False),
            (frozenset({SCOPE_READ}), SCOPE_MUTATE, False),
            (frozenset({SCOPE_READ}), SCOPE_ADMIN, False),
            (frozenset({SCOPE_INGEST}), SCOPE_INGEST, True),
            (frozenset({SCOPE_INGEST}), SCOPE_READ, False),
            (frozenset({SCOPE_MUTATE}), SCOPE_MUTATE, True),
            (frozenset({SCOPE_MUTATE}), SCOPE_ADMIN, False),
            (frozenset({SCOPE_READ, SCOPE_MUTATE}), SCOPE_MUTATE, True),
            (frozenset(), SCOPE_READ, False),
        ],
    )
    def test_matrix(
        self, granted: frozenset[str], required: str, expected: bool
    ) -> None:
        assert scopes_satisfy(granted, required) is expected

    def test_unknown_required_scope_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown scope"):
            scopes_satisfy(frozenset({SCOPE_ADMIN}), "write")
