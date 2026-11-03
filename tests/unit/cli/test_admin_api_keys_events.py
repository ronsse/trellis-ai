"""``trellis admin api-keys`` emits an audit event for every mint and revoke.

The CLI is one of three surfaces that change a key; the event is emitted by the
governed handler, so it reads the same whichever surface did the work.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis.stores.base.event_log import EventType
from trellis_cli.exit_codes import EXIT_OK, EXIT_VALIDATION
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


def _create(scopes: str = "read", name: str = "ci"):  # type: ignore[no-untyped-def]
    return _invoke(
        ["api-keys", "create", "--name", name, "--scopes", scopes, "--format", "json"]
    )


def _events(event_type: EventType):  # type: ignore[no-untyped-def]
    return _get_registry().operational.event_log.get_events(
        event_type=event_type, limit=100
    )


def test_create_emits_api_key_minted_without_the_secret() -> None:
    result = _create("read,ingest", "ingest-bot")
    assert result.exit_code == EXIT_OK, result.output
    body = json.loads(result.stdout)
    events = _events(EventType.API_KEY_MINTED)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["key_id"] == body["key_id"]
    assert payload["name"] == "ingest-bot"
    assert payload["scopes"] == ["read", "ingest"]
    assert payload["minted_by"] == "cli:api-keys"
    log = json.dumps(
        [
            e.model_dump(mode="json")
            for e in _get_registry().operational.event_log.get_events(limit=1000)
        ]
    )
    record = _get_registry().operational.api_key_store.get(body["key_id"])
    assert record is not None
    assert body["token"] not in log
    assert body["token"].split(".", 1)[1] not in log
    assert record.secret_hash not in log


def test_revoke_emits_api_key_revoked() -> None:
    key_id = json.loads(_create().stdout)["key_id"]
    result = _invoke(["api-keys", "revoke", key_id, "--format", "json"])
    assert result.exit_code == EXIT_OK, result.output
    events = _events(EventType.API_KEY_REVOKED)
    assert len(events) == 1
    assert events[0].payload["key_id"] == key_id
    assert events[0].payload["revoked_by"] == "cli:api-keys"


def test_the_cli_can_revoke_any_key_it_has_no_principal_to_protect() -> None:
    key_id = json.loads(_create("admin", "root").stdout)["key_id"]
    assert _invoke(["api-keys", "revoke", key_id]).exit_code == EXIT_OK


def test_a_refused_scope_list_writes_no_key_and_no_event() -> None:
    result = _create("read,admin,external")
    assert result.exit_code == EXIT_VALIDATION
    assert json.loads(result.stdout)["error"] == "validation_error"
    assert _get_registry().operational.api_key_store.list() == []
    assert _events(EventType.API_KEY_MINTED) == []


def test_unknown_and_already_revoked_ids_exit_validation_and_emit_nothing() -> None:
    unknown = _invoke(["api-keys", "revoke", "ffffffffffff", "--format", "json"])
    assert unknown.exit_code == EXIT_VALIDATION
    assert json.loads(unknown.stdout)["error"] == "unknown_key_id"
    key_id = json.loads(_create().stdout)["key_id"]
    assert _invoke(["api-keys", "revoke", key_id]).exit_code == EXIT_OK
    again = _invoke(["api-keys", "revoke", key_id, "--format", "json"])
    assert again.exit_code == EXIT_VALIDATION
    assert json.loads(again.stdout)["error"] == "already_revoked"
    assert len(_events(EventType.API_KEY_REVOKED)) == 1
    # Both refusals are recorded by the executor's generic rejection event.
    assert len(_events(EventType.MUTATION_REJECTED)) == 2
