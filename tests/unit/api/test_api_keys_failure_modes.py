"""API-key routes: failure behaviour, caller identity, names and audit content.

A key operation that took effect answers success even when its audit event
cannot be written; a server fault is a 5xx without exception text; an anonymous
caller can neither mint nor revoke in any auth mode; names are bounded.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

import trellis_api.app as app_module
from trellis.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    generate_api_key,
    parse_scope_list,
    verify_token,
)
from trellis.auth.api_keys import api_key_name_refusal
from trellis.mutate import CommandResult, CommandStatus, build_api_keys_executor
from trellis.mutate.api_key_commands import build_mint_command, build_revoke_command
from trellis.mutate.commands import Command, Operation
from trellis.mutate.executor import AUDIT_EMIT_FAILED_MARKER, MutationExecutor
from trellis.mutate.policy_source import resolve_policy_path
from trellis.stores.base.api_key import ApiKeyRecord
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app
from trellis_api.routes.api_keys import (
    IDENTITY_REFUSAL_HEADER,
    IDENTITY_REFUSAL_VALUE,
)

from ...flaky_registry import FlakyRegistry

BASE = "/api/v1/api-keys"


@pytest.fixture
def registry(tmp_path: Path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture(autouse=True)
def _auth_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    return TestClient(create_app())


def _seed(registry: StoreRegistry, scopes: list[str], name: str) -> tuple[str, str]:
    token, record = generate_api_key(name, scopes)
    registry.operational.api_key_store.create(record)
    return token, record.key_id


def _h(token: str) -> dict[str, str]:
    return {"X-API-Key": token}


@pytest.fixture
def admin(registry: StoreRegistry) -> tuple[str, str]:
    return _seed(registry, [SCOPE_ADMIN], "root")


def _count(registry: StoreRegistry) -> int:
    return len(registry.operational.api_key_store.list())


def _dead_event_log(registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        msg = "disk I/O error SECRET-DRIVER-TEXT"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(registry.operational.event_log, "emit", boom)


class TestAnAuditFailureNeverChangesTheOutcome:
    def test_a_mint_still_hands_back_a_working_token(
        self,
        client: TestClient,
        registry: StoreRegistry,
        admin: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _dead_event_log(registry, monkeypatch)
        resp = client.post(
            BASE, json={"name": "ci", "scopes": "read"}, headers=_h(admin[0])
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert any(AUDIT_EMIT_FAILED_MARKER in w for w in body["warnings"])
        assert "SECRET-DRIVER-TEXT" not in resp.text
        stored = registry.operational.api_key_store.get(body["key_id"])
        assert stored is not None
        assert verify_token(body["token"], registry.operational.api_key_store)

    def test_a_revoke_reports_success_and_the_warning(
        self,
        client: TestClient,
        registry: StoreRegistry,
        admin: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, victim = _seed(registry, [SCOPE_READ], "victim")
        _dead_event_log(registry, monkeypatch)
        resp = client.delete(f"{BASE}/{victim}", headers=_h(admin[0]))
        assert resp.status_code == 200, resp.text
        assert any(AUDIT_EMIT_FAILED_MARKER in w for w in resp.json()["warnings"])
        record = registry.operational.api_key_store.get(victim)
        assert record is not None
        assert record.revoked_at is not None

    def test_an_audit_emit_raising_import_error_is_still_only_a_warning(
        self,
        client: TestClient,
        registry: StoreRegistry,
        admin: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real = registry.operational.event_log.emit

        def selective(event_type: Any, *args: Any, **kwargs: Any) -> Any:
            if event_type == EventType.API_KEY_MINTED:
                msg = "no module named SECRET-DRIVER-TEXT"
                raise ImportError(msg)
            return real(event_type, *args, **kwargs)

        monkeypatch.setattr(registry.operational.event_log, "emit", selective)
        with capture_logs() as logs:
            resp = client.post(
                BASE, json={"name": "ci", "scopes": "read"}, headers=_h(admin[0])
            )
        assert resp.status_code == 201, resp.text
        assert any(AUDIT_EMIT_FAILED_MARKER in w for w in resp.json()["warnings"])
        assert "SECRET-DRIVER-TEXT" not in resp.text
        assert all("SECRET-DRIVER-TEXT" not in str(v) for e in logs for v in e.values())

    def test_an_unopenable_event_log_is_a_server_fault_not_a_tolerated_one(
        self,
        registry: StoreRegistry,
        admin: tuple[str, str],
    ) -> None:
        """Only the CLI tolerates a failed open; REST and MCP must not."""
        app_module._registry = FlakyRegistry(registry, fail_first=10**6)
        before = _count(registry)
        resp = TestClient(create_app(), raise_server_exceptions=False).post(
            BASE, json={"name": "ci", "scopes": "read"}, headers=_h(admin[0])
        )
        assert resp.status_code == 500, resp.text
        assert "SECRET-DRIVER-TEXT" not in resp.text
        assert _count(registry) == before

    def test_a_warn_verdict_policy_reaches_the_response(
        self,
        client: TestClient,
        registry: StoreRegistry,
        admin: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real = MutationExecutor.execute

        def warned(self: MutationExecutor, command: Command) -> CommandResult:
            result = real(self, command)
            return result.model_copy(update={"warnings": ["policy_warning: heads up"]})

        monkeypatch.setattr(MutationExecutor, "execute", warned)
        resp = client.post(
            BASE, json={"name": "ci", "scopes": "read"}, headers=_h(admin[0])
        )
        assert resp.status_code == 201
        assert resp.json()["warnings"] == ["policy_warning: heads up"]

    def test_a_server_fault_is_5xx_without_exception_text(
        self,
        client: TestClient,
        registry: StoreRegistry,
        admin: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(_record: Any) -> None:
            msg = "boom-detail select * from keys"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(registry.operational.api_key_store, "create", boom)
        before = _count(registry)
        resp = client.post(
            BASE, json={"name": "ci", "scopes": "read"}, headers=_h(admin[0])
        )
        assert resp.status_code == 500, resp.text
        assert "boom-detail" not in resp.text
        assert _count(registry) == before

    def test_a_duplicate_result_is_not_a_success(
        self,
        client: TestClient,
        admin: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def dup(self: MutationExecutor, command: Command) -> CommandResult:
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.DUPLICATE,
                operation=command.operation,
            )

        monkeypatch.setattr(MutationExecutor, "execute", dup)
        resp = client.post(
            BASE, json={"name": "ci", "scopes": "read"}, headers=_h(admin[0])
        )
        assert resp.status_code == 409
        assert "token" not in resp.json()

    def test_a_corrupt_policy_file_fails_closed_and_names_the_file(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        resolve_policy_path(registry.stores_dir).write_text("{ not json")
        before = _count(registry)
        resp = client.post(
            BASE, json={"name": "ci", "scopes": "read"}, headers=_h(admin[0])
        )
        assert resp.status_code == 409, resp.text
        assert "policies.json" in resp.text
        assert _count(registry) == before


class TestMintAndRevokeNeedAnIdentifiedAdmin:
    @pytest.mark.parametrize("mode", ["off", "optional"])
    def test_an_anonymous_caller_cannot_mint(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", mode)
        resp = client.post(BASE, json={"name": "x", "scopes": "admin"})
        assert resp.status_code == 403, resp.text
        assert "trellis admin api-keys" in resp.text
        assert _count(registry) == 0

    def test_under_off_a_read_key_cannot_mint_or_revoke(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token, _ = _seed(registry, [SCOPE_READ], "reader")
        _, victim = _seed(registry, [SCOPE_READ], "victim")
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        assert (
            client.post(
                BASE, json={"name": "x", "scopes": "admin"}, headers=_h(token)
            ).status_code
            == 403
        )
        resp = client.delete(f"{BASE}/{victim}", headers=_h(token))
        assert resp.status_code == 403
        record = registry.operational.api_key_store.get(victim)
        assert record is not None
        assert record.revoked_at is None

    def test_under_optional_an_admin_key_still_mints(
        self,
        client: TestClient,
        admin: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "optional")
        resp = client.post(
            BASE, json={"name": "ci", "scopes": "read"}, headers=_h(admin[0])
        )
        assert resp.status_code == 201

    def test_the_shared_secret_mints_and_the_event_names_it(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_KEY", "shared-secret-value")
        resp = client.post(
            BASE,
            json={"name": "ci", "scopes": "read"},
            headers=_h("shared-secret-value"),
        )
        assert resp.status_code == 201, resp.text
        events = registry.operational.event_log.get_events(
            event_type=EventType.API_KEY_MINTED
        )
        assert events[0].payload["actor_key_name"] == "shared-secret"
        assert events[0].payload["minted_by"].startswith("api:")

    def test_a_stored_admin_cannot_revoke_itself(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        token, key_id = admin
        assert client.delete(f"{BASE}/{key_id}", headers=_h(token)).status_code == 409
        assert client.delete(f"{BASE}/{key_id}").status_code == 401
        record = registry.operational.api_key_store.get(key_id)
        assert record is not None
        assert record.revoked_at is None

    def test_the_shared_secret_can_revoke_the_only_stored_admin(
        self,
        client: TestClient,
        registry: StoreRegistry,
        admin: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pins the documented gap: self-revoke is the whole last-admin guard.

        The secret has no key id, so the self-revoke check cannot match it, and
        a deployment with one stored admin and ``TRELLIS_API_KEY`` set can reach
        zero live admin keys over REST. The docs say so; this keeps them honest.
        """
        monkeypatch.setenv("TRELLIS_API_KEY", "shared-secret-value")
        _, key_id = admin
        resp = client.delete(f"{BASE}/{key_id}", headers=_h("shared-secret-value"))
        assert resp.status_code == 200, resp.text
        live_admins = [
            k
            for k in registry.operational.api_key_store.list()
            if k.revoked_at is None and SCOPE_ADMIN in k.scopes
        ]
        assert live_admins == []

    def test_a_scope_refusal_does_not_carry_the_identity_marker(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        token, _ = _seed(registry, [SCOPE_READ], "reader")
        resp = client.post(
            BASE, json={"name": "x", "scopes": "read"}, headers=_h(token)
        )
        assert resp.status_code == 403
        assert IDENTITY_REFUSAL_HEADER not in resp.headers

    def test_the_identity_refusal_is_marked_and_logged_without_a_secret(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "optional")
        with capture_logs() as logs:
            resp = client.post(BASE, json={"name": "x", "scopes": "read"})
        assert resp.status_code == 403
        assert resp.headers[IDENTITY_REFUSAL_HEADER] == IDENTITY_REFUSAL_VALUE
        refusals = [
            e for e in logs if e["event"] == "api_key_write_refused_unidentified"
        ]
        assert len(refusals) == 1
        assert refusals[0]["log_level"] == "warning"
        assert refusals[0]["mode"] == "optional"
        assert "present an admin API key" in resp.json()["detail"]

    @pytest.mark.parametrize("presented", ["stored", "secret", "none"])
    def test_under_off_the_refusal_points_only_at_the_cli(
        self,
        client: TestClient,
        registry: StoreRegistry,
        admin: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
        presented: str,
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        monkeypatch.setenv("TRELLIS_API_KEY", "shared-secret-value")
        headers = {
            "stored": _h(admin[0]),
            "secret": _h("shared-secret-value"),
            "none": {},
        }[presented]
        resp = client.post(BASE, json={"name": "x", "scopes": "read"}, headers=headers)
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert "trellis admin api-keys" in detail
        assert "present an admin API key" not in detail
        assert resp.headers[IDENTITY_REFUSAL_HEADER] == IDENTITY_REFUSAL_VALUE
        assert _count(registry) == 1


class TestNames:
    @pytest.mark.parametrize(
        "name",
        [
            "a" * 121,
            "a" * 1_000_000,
            "bad\x00name",
            "bad\nname",
            "bad\x1b[2Jname",
            "bad\u2028name",
            "bad\u2029name",
        ],
        ids=["121", "million", "nul", "newline", "esc", "line-sep", "para-sep"],
    )
    def test_an_unsuitable_name_is_refused(
        self,
        client: TestClient,
        registry: StoreRegistry,
        admin: tuple[str, str],
        name: str,
    ) -> None:
        resp = client.post(
            BASE, json={"name": name, "scopes": "read"}, headers=_h(admin[0])
        )
        assert resp.status_code == 400, resp.text[:200]
        assert _count(registry) == 1

    @pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
    def test_the_line_and_paragraph_separators_are_refused_by_the_rule(
        self, separator: str
    ) -> None:
        assert api_key_name_refusal(f"a{separator}b") is not None
        assert api_key_name_refusal("a b") is None

    def test_the_limit_is_inclusive(
        self, client: TestClient, admin: tuple[str, str]
    ) -> None:
        resp = client.post(
            BASE, json={"name": "a" * 120, "scopes": "read"}, headers=_h(admin[0])
        )
        assert resp.status_code == 201

    def test_a_name_stored_before_the_rule_still_lists(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        registry.operational.api_key_store.create(
            ApiKeyRecord(
                key_id="aabbccddeeff",
                name="old\x1b[2Jname",
                scopes=("read",),
                secret_hash="0" * 64,
            )
        )
        resp = client.get(BASE, headers=_h(admin[0]))
        assert resp.status_code == 200
        assert "aabbccddeeff" in [k["key_id"] for k in resp.json()["keys"]]


class TestMintContract:
    def test_scopes_are_case_sensitive(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        with pytest.raises(ValueError, match="Unknown"):
            parse_scope_list("ADMIN")
        before = _count(registry)
        resp = client.post(
            BASE, json={"name": "x", "scopes": "ADMIN"}, headers=_h(admin[0])
        )
        assert resp.status_code == 400
        assert _count(registry) == before

    def test_the_handler_rechecks_what_a_hand_built_command_carries(
        self, registry: StoreRegistry
    ) -> None:
        executor = build_api_keys_executor(registry)
        for record in (
            ApiKeyRecord(
                key_id="000000000001", name="n", scopes=("bogus",), secret_hash="0" * 64
            ),
            ApiKeyRecord(
                key_id="000000000002",
                name="n",
                scopes=("read", "external", "admin"),
                secret_hash="0" * 64,
            ),
            ApiKeyRecord(
                key_id="000000000003",
                name="x" * 500,
                scopes=("read",),
                secret_hash="0" * 64,
            ),
        ):
            result = executor.execute(
                Command(
                    operation=Operation.API_KEY_MINT,
                    args={"record": record},
                    target_id=record.key_id,
                    target_type="api_key",
                    requested_by="test",
                )
            )
            assert result.status == CommandStatus.REJECTED
        assert _count(registry) == 0

    def test_two_identical_mints_make_two_keys(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        tokens = []
        for _ in range(2):
            resp = client.post(
                BASE, json={"name": "same", "scopes": "read"}, headers=_h(admin[0])
            )
            assert resp.status_code == 201, resp.text
            tokens.append(resp.json()["token"])
        assert _count(registry) == 3
        assert all(verify_token(t, registry.operational.api_key_store) for t in tokens)
        _, _, command = build_mint_command("same", "read", requested_by="t")
        assert command.idempotency_key is None

    def test_no_event_holds_a_secret_or_a_hash(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        minted = client.post(
            BASE, json={"name": "ci", "scopes": "read"}, headers=_h(admin[0])
        ).json()
        key_id = minted["key_id"]
        digest = registry.operational.api_key_store.get(key_id).secret_hash  # type: ignore[union-attr]
        secret = minted["token"].rsplit(".", 1)[1]
        assert (
            client.delete(f"{BASE}/{key_id}", headers=_h(admin[0])).status_code == 200
        )
        log = json.dumps(
            [
                e.model_dump(mode="json")
                for e in registry.operational.event_log.get_events(limit=1000)
            ]
        )
        assert digest not in log
        assert secret not in log
        revoked = registry.operational.event_log.get_events(
            event_type=EventType.API_KEY_REVOKED
        )[0]
        assert set(revoked.payload) == {
            "key_id",
            "name",
            "scopes",
            "revoked_by",
            "actor_key_id",
            "actor_key_name",
        }
        assert build_revoke_command(key_id, requested_by="t").idempotency_key is None
