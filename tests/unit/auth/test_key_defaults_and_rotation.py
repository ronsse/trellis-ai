"""Per-key defaults, key expiry and rotation: the store, handler and orchestration.

Everything runs on a ``tmp_path`` store with keys minted for the test; no token
is ever printed (failure messages only ever name key ids).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from trellis.auth import (
    build_key_defaults,
    fill_omitted,
    parse_grace,
    public_listing_row,
    verify_token,
)
from trellis.core.base import utc_now
from trellis.mutate import build_api_keys_executor
from trellis.mutate.api_key_commands import (
    build_mint_command,
    build_revoke_command,
    build_update_command,
    rotate_key,
)
from trellis.mutate.commands import CommandResult, CommandStatus
from trellis.stores.base.api_key import ApiKeyDefaults
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from collections.abc import Iterator

    from trellis.mutate.commands import Command

ACTOR = "test:keys"


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[StoreRegistry]:
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    yield reg
    reg.close()


def _mint(
    registry: StoreRegistry,
    scopes: str = "read",
    name: str = "k",
    **defaults: str,
) -> tuple[str, str]:
    block = build_key_defaults(**defaults) if defaults else None
    token, record, command = build_mint_command(
        name, scopes, requested_by=ACTOR, defaults=block
    )
    result = build_api_keys_executor(registry).execute(command)
    assert result.status == CommandStatus.SUCCESS, result.message
    return token, record.key_id


def _events(registry: StoreRegistry, kind: EventType) -> list[object]:
    return registry.operational.event_log.get_events(event_type=kind, limit=100)


class TestDefaultsBlock:
    def test_a_block_round_trips_through_the_store_and_the_listing(
        self, registry: StoreRegistry
    ) -> None:
        token, key_id = _mint(
            registry, agent_id="agent-7", domain="platform", team="core"
        )
        stored = registry.operational.api_key_store.get(key_id)
        assert stored is not None
        assert stored.defaults is not None
        assert stored.defaults.model_dump(exclude_none=True) == {
            "agent_id": "agent-7",
            "domain": "platform",
            "team": "core",
        }
        row = public_listing_row(stored)
        assert row["defaults"] == {
            "agent_id": "agent-7",
            "domain": "platform",
            "team": "core",
        }
        assert "secret_hash" not in row
        assert token not in str(row)

    def test_a_key_without_defaults_lists_no_defaults_key(
        self, registry: StoreRegistry
    ) -> None:
        _, key_id = _mint(registry)
        stored = registry.operational.api_key_store.get(key_id)
        assert stored is not None
        assert stored.defaults is None
        assert "defaults" not in public_listing_row(stored)
        assert "expires_at" not in public_listing_row(stored)

    def test_a_row_stored_before_the_columns_existed_loads_unchanged(
        self, tmp_path: Path
    ) -> None:
        reg = StoreRegistry(stores_dir=tmp_path / "stores")
        token, key_id = _mint(reg, "read,confidential", name="old")
        reg.close()
        db = tmp_path / "stores" / "api_keys.db"
        with sqlite3.connect(db) as conn:
            conn.execute("ALTER TABLE trellis_api_keys DROP COLUMN defaults")
            conn.execute("ALTER TABLE trellis_api_keys DROP COLUMN expires_at")
        reopened = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            store = reopened.operational.api_key_store
            record = store.get(key_id)
            assert record is not None
            assert record.defaults is None
            assert record.expires_at is None
            assert set(record.scopes) == {"read", "confidential"}
            assert verify_token(token, store) is not None
            # The migration added the columns back, so the row can take defaults.
            assert store.update(key_id, defaults=build_key_defaults(domain="platform"))
        finally:
            reopened.close()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("agent_id", "line\nbreak"),
            ("domain", "x" * 500),
            ("team", "\x1b[31mred"),
            ("session_id_prefix", "   "),
        ],
    )
    def test_a_bad_value_is_refused_before_anything_is_written(
        self, field: str, value: str
    ) -> None:
        with pytest.raises(ValueError, match=field):
            build_key_defaults(**{field: value})

    def test_no_field_builds_no_block(self) -> None:
        assert build_key_defaults() is None

    def test_a_default_fills_an_omitted_field_and_never_replaces_a_set_one(
        self,
    ) -> None:
        assert fill_omitted(None, "d") == "d"
        assert fill_omitted("", "d") == "d"
        assert fill_omitted("   ", "d") == "d"
        assert fill_omitted("mine", "d") == "mine"
        assert fill_omitted(None, None) is None


class TestExpiry:
    def test_an_expired_key_stops_verifying_and_a_future_one_still_does(
        self, registry: StoreRegistry
    ) -> None:
        token, key_id = _mint(registry)
        store = registry.operational.api_key_store
        assert store.update(key_id, expires_at=utc_now() + timedelta(hours=1))
        assert verify_token(token, store) is not None
        assert store.update(key_id, expires_at=utc_now() - timedelta(seconds=1))
        assert verify_token(token, store) is None
        # An expired key is not revived by clearing its expiry; a live one is.
        assert store.update(key_id, expires_at=None) is False
        assert verify_token(token, store) is None

    def test_a_live_keys_expiry_can_still_be_cleared(
        self, registry: StoreRegistry
    ) -> None:
        token, key_id = _mint(registry)
        store = registry.operational.api_key_store
        assert store.update(key_id, expires_at=utc_now() + timedelta(hours=1))
        assert store.update(key_id, expires_at=None)
        assert verify_token(token, store) is not None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("30m", timedelta(minutes=30)),
            ("24h", timedelta(hours=24)),
            ("7d", timedelta(days=7)),
            ("2w", timedelta(weeks=2)),
        ],
    )
    def test_grace_parses(self, text: str, expected: timedelta) -> None:
        assert parse_grace(text) == expected

    @pytest.mark.parametrize("text", ["", "0h", "abc", "10", "-1d", "91d", "1.5h"])
    def test_grace_refuses_anything_else(self, text: str) -> None:
        with pytest.raises(ValueError, match="race"):
            parse_grace(text)


class TestUpdateHandler:
    def _update(self, registry: StoreRegistry, command: Command) -> CommandResult:
        return build_api_keys_executor(registry).execute(command)

    def test_it_sets_and_clears_defaults_and_audits_each(
        self, registry: StoreRegistry
    ) -> None:
        _, key_id = _mint(registry)
        block = build_key_defaults(domain="platform", session_id_prefix="ci")
        ok = self._update(
            registry,
            build_update_command(key_id, defaults=block, requested_by=ACTOR),
        )
        assert ok.status == CommandStatus.SUCCESS
        store = registry.operational.api_key_store
        assert store.get(key_id).defaults == block  # type: ignore[union-attr]
        cleared = self._update(
            registry,
            build_update_command(key_id, defaults=None, requested_by=ACTOR),
        )
        assert cleared.status == CommandStatus.SUCCESS
        assert store.get(key_id).defaults is None  # type: ignore[union-attr]
        assert len(_events(registry, EventType.API_KEY_UPDATED)) == 2

    def test_an_update_leaves_the_credential_alone(
        self, registry: StoreRegistry
    ) -> None:
        token, key_id = _mint(registry, "read,restricted", name="keep")
        self._update(
            registry,
            build_update_command(
                key_id,
                defaults=build_key_defaults(team="core"),
                requested_by=ACTOR,
            ),
        )
        record = verify_token(token, registry.operational.api_key_store)
        assert record is not None
        assert record.name == "keep"
        assert set(record.scopes) == {"read", "restricted"}

    def test_it_refuses_an_unknown_a_revoked_and_an_empty_update(
        self, registry: StoreRegistry
    ) -> None:
        _, key_id = _mint(registry)
        block = build_key_defaults(domain="d")
        unknown = self._update(
            registry,
            build_update_command("ffffffffffff", defaults=block, requested_by=ACTOR),
        )
        assert unknown.status == CommandStatus.REJECTED
        assert unknown.metadata["rejection_reason"] == "unknown_key_id"
        empty = self._update(registry, build_update_command(key_id, requested_by=ACTOR))
        assert empty.metadata["rejection_reason"] == "nothing_to_update"
        self._update(registry, build_revoke_command(key_id, requested_by=ACTOR))
        revoked = self._update(
            registry,
            build_update_command(key_id, defaults=block, requested_by=ACTOR),
        )
        assert revoked.metadata["rejection_reason"] == "already_revoked"
        assert len(_events(registry, EventType.API_KEY_UPDATED)) == 0


class TestRotation:
    def _rotate(
        self,
        registry: StoreRegistry,
        key_id: str,
        execute=None,  # type: ignore[no-untyped-def]
        **kwargs: object,
    ):  # type: ignore[no-untyped-def]
        return rotate_key(
            registry.operational.api_key_store,
            execute or build_api_keys_executor(registry).execute,
            key_id,
            requested_by=ACTOR,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_the_successor_keeps_scopes_and_defaults_and_the_old_key_is_revoked(
        self, registry: StoreRegistry
    ) -> None:
        old_token, old_id = _mint(
            registry, "read,restricted", name="svc", domain="platform", team="core"
        )
        outcome = self._rotate(registry, old_id)
        assert outcome.retired == "revoked"
        assert outcome.token is not None
        assert outcome.record is not None
        assert outcome.failure is None
        store = registry.operational.api_key_store
        new = verify_token(outcome.token, store)
        assert new is not None
        assert new.key_id != old_id
        assert new.name == "svc"
        assert set(new.scopes) == {"read", "restricted"}
        assert new.defaults == store.get(old_id).defaults  # type: ignore[union-attr]
        assert verify_token(old_token, store) is None
        minted = _events(registry, EventType.API_KEY_MINTED)[-1]
        assert minted.payload["rotated_from"] == old_id  # type: ignore[attr-defined]

    def test_a_grace_period_leaves_the_old_key_valid_until_it_closes(
        self, registry: StoreRegistry
    ) -> None:
        old_token, old_id = _mint(registry)
        outcome = self._rotate(registry, old_id, grace=timedelta(hours=2))
        assert outcome.retired == "grace"
        assert outcome.old_expires_at is not None
        store = registry.operational.api_key_store
        assert verify_token(old_token, store) is not None
        assert store.get(old_id).revoked_at is None  # type: ignore[union-attr]
        assert store.update(old_id, expires_at=utc_now() - timedelta(seconds=1))
        assert verify_token(old_token, store) is None

    def test_a_crash_between_mint_and_retire_leaves_both_keys_valid(
        self, registry: StoreRegistry
    ) -> None:
        old_token, old_id = _mint(registry)
        real = build_api_keys_executor(registry).execute
        calls: list[Command] = []

        def crashing(command: Command) -> CommandResult:
            calls.append(command)
            if len(calls) == 2:
                msg = "simulated crash"
                raise RuntimeError(msg)
            return real(command)

        outcome = self._rotate(registry, old_id, execute=crashing)
        assert outcome.retired == "still_valid"
        assert outcome.failure is not None
        assert outcome.token is not None
        store = registry.operational.api_key_store
        assert verify_token(old_token, store) is not None
        assert verify_token(outcome.token, store) is not None

    def test_a_mint_refused_by_policy_leaves_only_the_old_key(
        self, registry: StoreRegistry
    ) -> None:
        old_token, old_id = _mint(registry)

        def refusing(command: Command) -> CommandResult:
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.REJECTED,
                operation=command.operation,
                message="denied",
                metadata={"rejection_reason": "policy_violation"},
            )

        outcome = self._rotate(registry, old_id, execute=refusing)
        assert outcome.retired == "none"
        assert outcome.token is None
        assert verify_token(old_token, registry.operational.api_key_store)
        assert len(registry.operational.api_key_store.list()) == 1

    def test_unknown_and_revoked_keys_are_refused_before_anything_is_minted(
        self, registry: StoreRegistry
    ) -> None:
        _, key_id = _mint(registry)
        unknown = self._rotate(registry, "ffffffffffff")
        assert unknown.refusal is not None
        assert unknown.refusal[0] == "unknown_key_id"
        build_api_keys_executor(registry).execute(
            build_revoke_command(key_id, requested_by=ACTOR)
        )
        revoked = self._rotate(registry, key_id)
        assert revoked.refusal is not None
        assert revoked.refusal[0] == "already_revoked"
        assert len(registry.operational.api_key_store.list()) == 1

    def test_the_key_a_request_is_made_with_cannot_rotate_itself_without_grace(
        self, registry: StoreRegistry
    ) -> None:
        _, key_id = _mint(registry, "admin")
        refused = self._rotate(registry, key_id, actor_key_id=key_id)
        assert refused.refusal is not None
        assert refused.refusal[0] == "cannot_revoke_own_key"
        assert len(registry.operational.api_key_store.list()) == 1
        graced = self._rotate(
            registry, key_id, actor_key_id=key_id, grace=timedelta(minutes=5)
        )
        assert graced.retired == "grace"
        assert len(registry.operational.api_key_store.list()) == 2


def _expire(registry: StoreRegistry, key_id: str) -> datetime:
    """Move a live key's expiry into the past, as a closed grace window would."""
    past = utc_now() - timedelta(minutes=1)
    assert registry.operational.api_key_store.update(key_id, expires_at=past)
    return past


class TestAnExpiredKeyIsNeverRevived:
    def _rotate(self, registry: StoreRegistry, key_id: str, **kwargs: object):  # type: ignore[no-untyped-def]
        return rotate_key(
            registry.operational.api_key_store,
            build_api_keys_executor(registry).execute,
            key_id,
            requested_by=ACTOR,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_rotating_an_expired_key_with_a_grace_period_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        old_token, old_id = _mint(registry)
        past = _expire(registry, old_id)
        outcome = self._rotate(registry, old_id, grace=timedelta(days=30))
        assert outcome.refusal is not None
        assert outcome.refusal[0] == "already_expired"
        assert outcome.token is None
        store = registry.operational.api_key_store
        assert len(store.list()) == 1
        assert store.get(old_id).expires_at == past  # type: ignore[union-attr]
        assert verify_token(old_token, store) is None

    def test_rotating_an_expired_key_without_one_is_refused_and_mints_nothing(
        self, registry: StoreRegistry
    ) -> None:
        _, old_id = _mint(registry)
        _expire(registry, old_id)
        outcome = self._rotate(registry, old_id)
        assert outcome.refusal is not None
        assert outcome.refusal[0] == "already_expired"
        assert len(registry.operational.api_key_store.list()) == 1
        assert (
            _events(registry, EventType.API_KEY_MINTED)[-1].payload["key_id"] == old_id
        )  # type: ignore[attr-defined]

    def test_a_key_already_in_grace_is_refused_not_extended(
        self, registry: StoreRegistry
    ) -> None:
        old_token, old_id = _mint(registry)
        first = self._rotate(registry, old_id, grace=timedelta(hours=1))
        assert first.retired == "grace"
        store = registry.operational.api_key_store
        window = store.get(old_id).expires_at  # type: ignore[union-attr]
        again = self._rotate(registry, old_id, grace=timedelta(days=60))
        assert again.refusal is not None
        assert again.refusal[0] == "already_in_grace"
        assert "extend" in again.refusal[1]
        assert len(store.list()) == 2
        assert store.get(old_id).expires_at == window  # type: ignore[union-attr]
        assert verify_token(old_token, store) is not None

    def test_a_key_in_grace_rotated_without_a_grace_period_is_refused_too(
        self, registry: StoreRegistry
    ) -> None:
        old_token, old_id = _mint(registry)
        self._rotate(registry, old_id, grace=timedelta(hours=1))
        store = registry.operational.api_key_store
        window = store.get(old_id).expires_at  # type: ignore[union-attr]
        again = self._rotate(registry, old_id)
        assert again.refusal is not None
        assert again.refusal[0] == "already_in_grace"
        assert again.token is None
        assert len(store.list()) == 2
        assert store.get(old_id).expires_at == window  # type: ignore[union-attr]
        assert verify_token(old_token, store) is not None

    def test_a_key_not_yet_expiring_still_rotates_with_a_grace_period(
        self, registry: StoreRegistry
    ) -> None:
        _, old_id = _mint(registry)
        outcome = self._rotate(registry, old_id, grace=timedelta(hours=1))
        assert outcome.refusal is None
        assert outcome.retired == "grace"

    def test_the_store_will_not_clear_or_move_an_expired_keys_expiry(
        self, registry: StoreRegistry
    ) -> None:
        token, key_id = _mint(registry)
        past = _expire(registry, key_id)
        store = registry.operational.api_key_store
        assert store.update(key_id, expires_at=None) is False
        assert store.update(key_id, expires_at=utc_now() + timedelta(days=9)) is False
        assert store.get(key_id).expires_at == past  # type: ignore[union-attr]
        assert verify_token(token, store) is None
        # The block is not the expiry: rewriting it on an expired key still works.
        assert store.update(key_id, defaults=build_key_defaults(domain="d"))

    def test_the_update_command_says_why_an_expired_key_stays_expired(
        self, registry: StoreRegistry
    ) -> None:
        _, key_id = _mint(registry)
        _expire(registry, key_id)
        result = build_api_keys_executor(registry).execute(
            build_update_command(key_id, expires_at=None, requested_by=ACTOR)
        )
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "key_expired"
        assert "never revived" in result.message
        assert _events(registry, EventType.API_KEY_UPDATED) == []


class TestListingCarriesTheExpiry:
    def test_the_row_has_expires_at_and_an_expired_flag(
        self, registry: StoreRegistry
    ) -> None:
        _, key_id = _mint(registry)
        store = registry.operational.api_key_store
        plain_row = public_listing_row(store.get(key_id))  # type: ignore[arg-type]
        assert plain_row["expired"] is False
        assert "expires_at" not in plain_row
        window = utc_now() + timedelta(hours=1)
        assert store.update(key_id, expires_at=window)
        live = public_listing_row(store.get(key_id))  # type: ignore[arg-type]
        assert live["expires_at"] == window.isoformat()
        assert live["expired"] is False
        past = _expire(registry, key_id)
        gone = public_listing_row(store.get(key_id))  # type: ignore[arg-type]
        assert gone["expires_at"] == past.isoformat()
        assert gone["expired"] is True


class TestAuditPayloadsHoldNoSecret:
    """Exact key sets, so a new field cannot slip a hash or token into the log."""

    def test_the_update_mint_and_revoke_events_have_exactly_these_fields(
        self, registry: StoreRegistry
    ) -> None:
        old_token, old_id = _mint(registry)
        store = registry.operational.api_key_store
        secret_hash = store.get(old_id).secret_hash  # type: ignore[union-attr]
        executor = build_api_keys_executor(registry)
        executor.execute(
            build_update_command(
                old_id, defaults=build_key_defaults(team="t"), requested_by=ACTOR
            )
        )
        rotated = rotate_key(
            store, executor.execute, old_id, requested_by=ACTOR, actor_key_id="a1"
        )
        assert rotated.retired == "revoked"
        actor = {"actor_key_id", "actor_key_name"}
        want = {
            EventType.API_KEY_UPDATED: {
                "key_id",
                "name",
                "scopes",
                "changed",
                "defaults",
                "expires_at",
                "updated_by",
                *actor,
            },
            EventType.API_KEY_MINTED: {
                "key_id",
                "name",
                "scopes",
                "minted_by",
                "rotated_from",
                *actor,
            },
            EventType.API_KEY_REVOKED: {
                "key_id",
                "name",
                "scopes",
                "revoked_by",
                *actor,
            },
        }
        for kind, fields in want.items():
            (event,) = _events(registry, kind)[-1:]
            payload = event.payload  # type: ignore[attr-defined]
            assert set(payload) == fields, kind
            blob = str(payload)
            assert secret_hash not in blob
            assert old_token.split(".")[-1] not in blob


class _StaleProbe:
    """A cursor whose first ``table_info`` probe reports no columns at all.

    That is what a second opener sees when the first one adds the columns
    between its probe and its ``ALTER``.
    """

    lied = False

    def __init__(self, real: sqlite3.Cursor) -> None:
        self._real = real

    def execute(self, sql: str, *args: object) -> object:
        if sql.startswith("PRAGMA table_info") and not _StaleProbe.lied:
            _StaleProbe.lied = True
            return iter(())
        return self._real.execute(sql, *args)

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class _StaleConn:
    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def cursor(self) -> _StaleProbe:
        return _StaleProbe(self._real.cursor())

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


def test_a_concurrent_opener_that_added_the_columns_first_is_tolerated(
    tmp_path: Path,
) -> None:
    from trellis.stores.sqlite.api_key import SQLiteApiKeyStore

    db = tmp_path / "keys.db"
    SQLiteApiKeyStore(db).close()  # the "other opener": columns now exist

    class Racing(SQLiteApiKeyStore):
        @property
        def _conn(self) -> sqlite3.Connection:  # type: ignore[override]
            return _StaleConn(self._get_conn())  # type: ignore[return-value]

    _StaleProbe.lied = False
    store = Racing(db)  # the ALTER hits "duplicate column name" and is swallowed
    assert _StaleProbe.lied
    store.close()
    cols = {
        r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(trellis_api_keys)")
    }
    assert {"defaults", "expires_at"} <= cols


class _AlterFailsCursor:
    def __init__(self, real: sqlite3.Cursor, message: str) -> None:
        self._real = real
        self._message = message

    def execute(self, sql: str, *args: object) -> object:
        if sql.startswith("ALTER TABLE"):
            raise sqlite3.OperationalError(self._message)
        return self._real.execute(sql, *args)

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class _AlterFailsConn:
    def __init__(self, real: sqlite3.Connection, message: str) -> None:
        self._real = real
        self._message = message

    def cursor(self) -> _AlterFailsCursor:
        return _AlterFailsCursor(self._real.cursor(), self._message)

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


@pytest.mark.parametrize(
    "message",
    [
        "disk I/O error",
        # The right words, but the column is still absent: not a lost race.
        "duplicate column name: defaults",
    ],
)
def test_a_failed_migration_that_is_not_a_lost_race_still_raises(
    tmp_path: Path, message: str
) -> None:
    from trellis.stores.sqlite.api_key import SQLiteApiKeyStore

    db = tmp_path / "keys.db"
    SQLiteApiKeyStore(db).close()
    with sqlite3.connect(db) as raw:  # an upgrade-from-before database
        raw.execute("ALTER TABLE trellis_api_keys DROP COLUMN defaults")
        raw.execute("ALTER TABLE trellis_api_keys DROP COLUMN expires_at")

    class Failing(SQLiteApiKeyStore):
        @property
        def _conn(self) -> sqlite3.Connection:  # type: ignore[override]
            return _AlterFailsConn(self._get_conn(), message)  # type: ignore[return-value]

    with pytest.raises(sqlite3.OperationalError, match=message):
        Failing(db)


class TestAnUpdateRefusesANaiveExpiry:
    def test_a_naive_expiry_is_refused_and_nothing_is_written(
        self, registry: StoreRegistry
    ) -> None:
        token, key_id = _mint(registry)
        naive = datetime.fromisoformat("2999-01-01T00:00:00")
        result = build_api_keys_executor(registry).execute(
            build_update_command(key_id, expires_at=naive, requested_by=ACTOR)
        )
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "invalid_expiry"
        store = registry.operational.api_key_store
        assert store.get(key_id).expires_at is None  # type: ignore[union-attr]
        assert verify_token(token, store) is not None
        assert _events(registry, EventType.API_KEY_UPDATED) == []

    def test_an_aware_expiry_at_any_offset_is_still_accepted(
        self, registry: StoreRegistry
    ) -> None:
        _, key_id = _mint(registry)
        later = (utc_now() + timedelta(hours=1)).astimezone(
            timezone(timedelta(hours=-5))
        )
        result = build_api_keys_executor(registry).execute(
            build_update_command(key_id, expires_at=later, requested_by=ACTOR)
        )
        assert result.status == CommandStatus.SUCCESS


class TestAMintRechecksItsDefaults:
    @pytest.mark.parametrize(
        "bad",
        [
            ApiKeyDefaults(agent_id="a\x1bb", domain="  "),
            ApiKeyDefaults(agent_id="ok", domain="  "),
            ApiKeyDefaults(agent_id="a\x1bb"),
            ApiKeyDefaults(team="t" * 121),
        ],
    )
    def test_a_hand_built_mint_with_a_bad_default_stores_nothing(
        self, registry: StoreRegistry, bad: ApiKeyDefaults
    ) -> None:
        _, _, command = build_mint_command(
            "k", "read", requested_by=ACTOR, defaults=bad
        )
        result = build_api_keys_executor(registry).execute(command)
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "invalid_defaults"
        assert registry.operational.api_key_store.list() == []
        assert _events(registry, EventType.API_KEY_MINTED) == []

    def test_a_blank_only_block_in_a_hand_built_record_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        # ``build_mint_command`` drops a block that sets nothing, so a blank
        # reaches the handler only on a record the caller assembled itself.
        _, record, command = build_mint_command("k", "read", requested_by=ACTOR)
        blank = record.model_copy(update={"defaults": ApiKeyDefaults(domain="  ")})
        command = command.model_copy(update={"args": {"record": blank}})
        result = build_api_keys_executor(registry).execute(command)
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "invalid_defaults"
        assert registry.operational.api_key_store.list() == []

    def test_mint_and_update_refuse_a_bad_block_with_the_same_words(
        self, registry: StoreRegistry
    ) -> None:
        bad = ApiKeyDefaults(agent_id="a\x1bb", domain="  ")
        execute = build_api_keys_executor(registry).execute
        _, key_id = _mint(registry)
        _, _, mint = build_mint_command("k2", "read", requested_by=ACTOR, defaults=bad)
        minted = execute(mint)
        updated = execute(
            build_update_command(key_id, defaults=bad, requested_by=ACTOR)
        )
        assert minted.message == updated.message
        assert minted.metadata["rejection_reason"] == "invalid_defaults"
        assert updated.metadata["rejection_reason"] == "invalid_defaults"

    def test_a_clean_block_still_mints(self, registry: StoreRegistry) -> None:
        _, key_id = _mint(registry, domain="platform", agent_id="svc")
        stored = registry.operational.api_key_store.get(key_id)
        assert stored is not None
        assert stored.defaults == ApiKeyDefaults(domain="platform", agent_id="svc")


class TestTwoRotationsOfOneKey:
    """Both read the key as live; only the first may write its grace window."""

    def test_the_second_rotations_retire_is_refused_and_the_window_is_unchanged(
        self, registry: StoreRegistry
    ) -> None:
        old_token, old_id = _mint(registry)
        store = registry.operational.api_key_store
        before_either = store.get(old_id)
        execute = build_api_keys_executor(registry).execute

        first = rotate_key(
            store, execute, old_id, grace=timedelta(hours=2), requested_by=ACTOR
        )
        assert first.retired == "grace"
        window = store.get(old_id).expires_at  # type: ignore[union-attr]
        assert window == first.old_expires_at

        class _ReadBeforeTheFirstWrote:
            """A preflight read taken before the first rotation retired the key."""

            def get(self, key_id: str) -> object:
                return before_either

        second = rotate_key(
            _ReadBeforeTheFirstWrote(),  # type: ignore[arg-type]
            execute,
            old_id,
            grace=timedelta(days=30),
            requested_by=ACTOR,
        )

        # The existing partial-rotation outcome: the successor is kept and its
        # token shown, the old key is reported as not retired by this rotation.
        assert second.retired == "still_valid"
        assert second.refusal is None
        assert second.failure is not None
        assert second.failure.status == CommandStatus.REJECTED
        assert second.failure.metadata["rejection_reason"] == "already_retired"
        assert second.token is not None
        assert first.token is not None
        assert verify_token(second.token, store) is not None
        assert verify_token(first.token, store) is not None
        # The grace window is the first rotation's, untouched.
        assert store.get(old_id).expires_at == window  # type: ignore[union-attr]
        assert verify_token(old_token, store) is not None
        assert len(_events(registry, EventType.API_KEY_UPDATED)) == 1
        assert len(_events(registry, EventType.API_KEY_MINTED)) == 3

    def test_the_second_rotation_after_a_revoke_reports_the_key_revoked(
        self, registry: StoreRegistry
    ) -> None:
        _, old_id = _mint(registry)
        store = registry.operational.api_key_store
        before_either = store.get(old_id)
        execute = build_api_keys_executor(registry).execute
        assert rotate_key(store, execute, old_id, requested_by=ACTOR).retired == (
            "revoked"
        )

        class _ReadBeforeTheFirstWrote:
            def get(self, key_id: str) -> object:
                return before_either

        second = rotate_key(
            _ReadBeforeTheFirstWrote(),  # type: ignore[arg-type]
            execute,
            old_id,
            grace=timedelta(hours=1),
            requested_by=ACTOR,
        )
        assert second.retired == "still_valid"
        assert second.failure is not None
        assert second.failure.metadata["rejection_reason"] == "already_revoked"
        assert store.get(old_id).expires_at is None  # type: ignore[union-attr]

    def test_the_conditional_is_only_a_rotations_not_every_updates(
        self, registry: StoreRegistry
    ) -> None:
        """An operator's own ``update`` may still move a live key's expiry."""
        _, key_id = _mint(registry)
        store = registry.operational.api_key_store
        execute = build_api_keys_executor(registry).execute
        soon = utc_now() + timedelta(hours=1)
        later = utc_now() + timedelta(days=3)
        assert execute(
            build_update_command(key_id, expires_at=soon, requested_by=ACTOR)
        ).status == (CommandStatus.SUCCESS)
        assert execute(
            build_update_command(key_id, expires_at=later, requested_by=ACTOR)
        ).status == (CommandStatus.SUCCESS)
        assert store.get(key_id).expires_at == later  # type: ignore[union-attr]

    def test_the_flag_without_an_expiry_to_set_is_a_malformed_command(
        self, registry: StoreRegistry
    ) -> None:
        _, key_id = _mint(registry)
        result = build_api_keys_executor(registry).execute(
            build_update_command(
                key_id,
                defaults=build_key_defaults(domain="d"),
                only_if_no_expiry=True,
                requested_by=ACTOR,
            )
        )
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "invalid_update"
