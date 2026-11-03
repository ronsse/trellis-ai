"""``trellis admin api-keys`` is the recovery path: it survives a dead event log.

A corrupt ``events.db`` must not keep a compromised key live, so ``create`` and
``revoke`` succeed and warn. A corrupt ``policies.json`` is the one case that
blocks them: Stage 2 fails closed, and the message names the file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import trellis_cli.stores as cli_stores
from trellis.mutate.policy_source import resolve_policy_path
from trellis.stores.base.api_key import ApiKeyRecord
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.main import app as root_app
from trellis_cli.stores import _get_registry

runner = CliRunner()


def _invoke(args: list[str]):  # type: ignore[no-untyped-def]
    return runner.invoke(root_app, ["admin", *args])


@pytest.fixture(autouse=True)
def _stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    assert _invoke(["init"]).exit_code == EXIT_OK


def _create(fmt: str, name: str = "ci"):  # type: ignore[no-untyped-def]
    return _invoke(
        ["api-keys", "create", "--name", name, "--scopes", "read", "--format", fmt]
    )


def _corrupt_event_log(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _get_registry()
    stores = registry.stores_dir
    registry.close()
    files = [f for f in stores.glob("event*") if f.is_file()]
    assert files, list(stores.iterdir())
    for f in files:
        f.write_bytes(b"this is not a database " * 400)
    monkeypatch.setattr(cli_stores, "_registry", None)


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_create_and_revoke_succeed_with_a_corrupt_event_log(
    monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    first = json.loads(_create("json", "victim").output)
    _corrupt_event_log(monkeypatch)

    made = _create(fmt)
    assert made.exit_code == EXIT_OK, made.output
    assert "audit_event_not_recorded" in made.output
    if fmt == "json":
        body = json.loads(made.output)
        assert body["token"]
        assert any("audit_event_not_recorded" in w for w in body["warnings"])

    revoked = _invoke(["api-keys", "revoke", first["key_id"], "--format", fmt])
    assert revoked.exit_code == EXIT_OK, revoked.output
    assert "audit_event_not_recorded" in revoked.output
    record = _get_registry().operational.api_key_store.get(first["key_id"])
    assert record is not None
    assert record.revoked_at is not None


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_a_corrupt_policy_file_blocks_the_cli_and_names_the_file(fmt: str) -> None:
    key_id = json.loads(_create("json", "victim").output)["key_id"]
    resolve_policy_path(_get_registry().stores_dir).write_text("{ not json")
    for args in (
        ["api-keys", "revoke", key_id, "--format", fmt],
        ["api-keys", "create", "--name", "n", "--scopes", "read", "--format", fmt],
    ):
        result = _invoke(args)
        assert result.exit_code == EXIT_STORE, result.output
        assert "policies.json" in result.output
    record = _get_registry().operational.api_key_store.get(key_id)
    assert record is not None
    assert record.revoked_at is None


def test_the_refusal_text_is_the_text_it_always_was() -> None:
    result = _invoke(["api-keys", "revoke", "ffffffffffff"])
    assert result.exit_code == EXIT_VALIDATION
    assert "Cannot revoke 'ffffffffffff': unknown key id." in result.output
    assert "Cannot change" not in result.output


@pytest.mark.parametrize("name", ["a" * 121, "bad\nname", "bad\x1b[2Jname"])
def test_an_unsuitable_name_is_refused_by_the_cli(name: str) -> None:
    result = _create("json", name)
    assert result.exit_code == EXIT_VALIDATION, result.output
    assert _get_registry().operational.api_key_store.list() == []


def test_a_stored_name_with_control_characters_prints_safely() -> None:
    _get_registry().operational.api_key_store.create(
        ApiKeyRecord(
            key_id="aabbccddeeff",
            name="a\x1b[bold]b",
            scopes=("read",),
            secret_hash="0" * 64,
        )
    )
    result = _invoke(["api-keys", "list"])
    assert result.exit_code == EXIT_OK
    assert "\x1b" not in result.output
    assert "a\\x1b[bold]b" in result.output
