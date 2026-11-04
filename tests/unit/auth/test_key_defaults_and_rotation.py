"""Per-key defaults, key expiry and rotation: the store, handler and orchestration.

Everything runs on a ``tmp_path`` store with keys minted for the test; no token
is ever printed (failure messages only ever name key ids).
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
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
