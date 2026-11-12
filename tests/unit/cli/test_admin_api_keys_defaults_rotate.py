"""``trellis admin api-keys`` defaults on create, ``set-defaults`` and ``rotate``.

End to end through the root app on throwaway stores. Assertions read output
through ``_redacted`` so no failure message can print a minted token.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.auth import TOKEN_PREFIX, verify_token
from trellis.core.base import utc_now
from trellis.mutate.api_key_commands import build_update_command
from trellis.mutate.commands import (
    Command,
    CommandResult,
    CommandStatus,
    Operation,
)
from trellis.stores.base.event_log import EventType
from trellis_cli import admin_api_keys as cli_api_keys
from trellis_cli.exit_codes import EXIT_OK, EXIT_VALIDATION
from trellis_cli.main import app as root_app
from trellis_cli.stores import _get_registry

runner = CliRunner()

_ANY_TOKEN = re.compile(rf"{re.escape(TOKEN_PREFIX)}\S+")


def _invoke(args: list[str]):  # type: ignore[no-untyped-def]
    return runner.invoke(root_app, ["admin", *args])


def _redacted(output: str) -> str:
    return " ".join(_ANY_TOKEN.sub("<token>", plain(output)).split())


@pytest.fixture(autouse=True)
def _stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    assert _invoke(["init"]).exit_code == EXIT_OK


def _create(*extra: str, scopes: str = "read", name: str = "svc"):  # type: ignore[no-untyped-def]
    result = _invoke(
        [
            "api-keys",
            "create",
            "--name",
            name,
            "--scopes",
            scopes,
            "--format",
            "json",
            *extra,
        ]
    )
    assert result.exit_code == EXIT_OK, _redacted(result.output)
    return json.loads(result.stdout)


def _events(kind: EventType):  # type: ignore[no-untyped-def]
    return _get_registry().operational.event_log.get_events(event_type=kind, limit=100)


def _stored(key_id: str):  # type: ignore[no-untyped-def]
    record = _get_registry().operational.api_key_store.get(key_id)
    assert record is not None
    return record


class TestCreateWithDefaults:
    def test_the_default_options_are_stored_and_listed(self) -> None:
        body = _create(
            "--default-agent-id",
            "agent-7",
            "--default-domain",
            "platform",
            "--default-team",
            "core",
            "--default-session-id-prefix",
            "ci",
        )
        want = {
            "agent_id": "agent-7",
            "domain": "platform",
            "team": "core",
            "session_id_prefix": "ci",
        }
        assert body["defaults"] == want
        assert _stored(body["key_id"]).defaults.model_dump(exclude_none=True) == want
        listing = json.loads(_invoke(["api-keys", "list", "--format", "json"]).stdout)
        assert listing["keys"][0]["defaults"] == want

    def test_a_key_without_defaults_prints_no_defaults_field(self) -> None:
        assert "defaults" not in _create()

    def test_a_bad_default_exits_validation_and_mints_nothing(self) -> None:
        result = _invoke(
            [
                "api-keys",
                "create",
                "--name",
                "x",
                "--scopes",
                "read",
                "--default-domain",
                "bad\nvalue",
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION
        assert _get_registry().operational.api_key_store.list() == []


class TestSetDefaults:
    def test_it_replaces_the_block_and_audits_the_change(self) -> None:
        key_id = _create("--default-team", "core")["key_id"]
        result = _invoke(
            [
                "api-keys",
                "set-defaults",
                key_id,
                "--domain",
                "platform",
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_OK, _redacted(result.output)
        assert json.loads(result.stdout) == {
            "status": "updated",
            "key_id": key_id,
            "defaults": {"domain": "platform"},
        }
        # Replaced, not merged: the team set at create time is gone.
        assert _stored(key_id).defaults.model_dump(exclude_none=True) == {
            "domain": "platform"
        }
        events = _events(EventType.API_KEY_UPDATED)
        assert len(events) == 1
        assert events[0].payload["changed"] == ["defaults"]
        assert events[0].payload["updated_by"] == "cli:api-keys"

    def test_clear_removes_the_block(self) -> None:
        key_id = _create("--default-domain", "platform")["key_id"]
        result = _invoke(
            ["api-keys", "set-defaults", key_id, "--clear", "--format", "json"]
        )
        assert result.exit_code == EXIT_OK
        assert json.loads(result.stdout)["defaults"] is None
        assert _stored(key_id).defaults is None

    @pytest.mark.parametrize(
        "args",
        [
            [],
            ["--clear", "--domain", "x"],
            ["--domain", "bad\nvalue"],
            ["--session-id-prefix", "x" * 500],
        ],
    )
    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_bad_input_exits_validation_in_both_formats_and_writes_nothing(
        self, args: list[str], fmt: str
    ) -> None:
        key_id = _create("--default-domain", "keep")["key_id"]
        result = _invoke(["api-keys", "set-defaults", key_id, *args, "--format", fmt])
        assert result.exit_code == EXIT_VALIDATION
        assert _events(EventType.API_KEY_UPDATED) == []
        assert _stored(key_id).defaults.model_dump(exclude_none=True) == {
            "domain": "keep"
        }

    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_unknown_and_revoked_keys_exit_validation_in_both_formats(
        self, fmt: str
    ) -> None:
        key_id = _create()["key_id"]
        unknown = _invoke(
            [
                "api-keys",
                "set-defaults",
                "ffffffffffff",
                "--domain",
                "d",
                "--format",
                fmt,
            ]
        )
        assert unknown.exit_code == EXIT_VALIDATION
        assert _invoke(["api-keys", "revoke", key_id]).exit_code == EXIT_OK
        revoked = _invoke(
            ["api-keys", "set-defaults", key_id, "--domain", "d", "--format", fmt]
        )
        assert revoked.exit_code == EXIT_VALIDATION
        assert _events(EventType.API_KEY_UPDATED) == []


class TestRotate:
    def test_it_mints_a_matching_successor_and_revokes_the_old_key(self) -> None:
        old = _create(
            "--default-domain", "platform", scopes="read,restricted", name="svc"
        )
        result = _invoke(["api-keys", "rotate", old["key_id"], "--format", "json"])
        assert result.exit_code == EXIT_OK, _redacted(result.output)
        body = json.loads(result.stdout)
        assert body["status"] == "rotated"
        assert body["retired"] == "revoked"
        assert body["old_key_id"] == old["key_id"]
        assert body["token"].startswith(TOKEN_PREFIX)
        assert body["key_id"] != old["key_id"]
        assert body["scopes"] == ["read", "restricted"]
        assert body["defaults"] == {"domain": "platform"}
        assert body["name"] == "svc"
        store = _get_registry().operational.api_key_store
        assert verify_token(body["token"], store) is not None
        assert verify_token(old["token"], store) is None
        minted = [
            e
            for e in _events(EventType.API_KEY_MINTED)
            if e.payload["key_id"] == body["key_id"]
        ]
        assert minted[0].payload["rotated_from"] == old["key_id"]
        assert len(_events(EventType.API_KEY_REVOKED)) == 1

    def test_the_token_is_printed_once_and_never_stored_in_an_event(self) -> None:
        old = _create()
        result = _invoke(["api-keys", "rotate", old["key_id"], "--format", "json"])
        token = json.loads(result.stdout)["token"]
        log = json.dumps(
            [
                e.model_dump(mode="json")
                for e in _get_registry().operational.event_log.get_events(limit=1000)
            ]
        )
        assert token not in log
        assert token.split(".", 1)[1] not in log

    def test_grace_leaves_the_old_key_valid_until_the_window_closes(self) -> None:
        old = _create()
        result = _invoke(
            ["api-keys", "rotate", old["key_id"], "--grace", "24h", "--format", "json"]
        )
        assert result.exit_code == EXIT_OK, _redacted(result.output)
        body = json.loads(result.stdout)
        assert body["retired"] == "grace"
        assert body["old_expires_at"] is not None
        store = _get_registry().operational.api_key_store
        assert verify_token(old["token"], store) is not None
        assert _stored(old["key_id"]).revoked_at is None
        assert _events(EventType.API_KEY_REVOKED) == []
        assert len(_events(EventType.API_KEY_UPDATED)) == 1

    def test_the_name_option_renames_the_successor(self) -> None:
        old = _create(name="svc")
        body = json.loads(
            _invoke(
                [
                    "api-keys",
                    "rotate",
                    old["key_id"],
                    "--name",
                    "svc-2",
                    "--format",
                    "json",
                ]
            ).stdout
        )
        assert body["name"] == "svc-2"

    @pytest.mark.parametrize("fmt", ["json", "text"])
    @pytest.mark.parametrize("grace", ["0h", "91d", "soon"])
    def test_a_bad_grace_exits_validation_in_both_formats_before_minting(
        self, fmt: str, grace: str
    ) -> None:
        old = _create()
        result = _invoke(
            ["api-keys", "rotate", old["key_id"], "--grace", grace, "--format", fmt]
        )
        assert result.exit_code == EXIT_VALIDATION
        assert len(_get_registry().operational.api_key_store.list()) == 1

    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_unknown_and_revoked_keys_exit_validation_in_both_formats(
        self, fmt: str
    ) -> None:
        old = _create()
        assert (
            _invoke(["api-keys", "rotate", "ffffffffffff", "--format", fmt]).exit_code
            == EXIT_VALIDATION
        )
        assert _invoke(["api-keys", "revoke", old["key_id"]]).exit_code == EXIT_OK
        assert (
            _invoke(["api-keys", "rotate", old["key_id"], "--format", fmt]).exit_code
            == EXIT_VALIDATION
        )
        assert len(_get_registry().operational.api_key_store.list()) == 1

    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_a_failed_retire_keeps_both_keys_shows_the_token_and_exits_alike(
        self, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        old = _create()
        real = cli_api_keys._execute

        def fail_retire(command: Command) -> CommandResult:
            if command.operation == Operation.API_KEY_REVOKE:
                return CommandResult(
                    command_id=command.command_id,
                    status=CommandStatus.FAILED,
                    operation=command.operation,
                    message="store unavailable",
                )
            return real(command)

        monkeypatch.setattr(cli_api_keys, "_execute", fail_retire)
        result = _invoke(["api-keys", "rotate", old["key_id"], "--format", fmt])
        assert result.exit_code != EXIT_OK
        store = _get_registry().operational.api_key_store
        assert verify_token(old["token"], store) is not None
        assert len(store.list()) == 2
        if fmt == "json":
            body = json.loads(result.stdout)
            assert body["status"] == "partial"
            assert body["retired"] == "still_valid"
            assert verify_token(body["token"], store) is not None
        else:
            text = plain(result.output)
            assert TOKEN_PREFIX in text
            assert "NOT retired" in " ".join(text.split())
        # The exit code does not depend on the format.
        other = "text" if fmt == "json" else "json"
        again = _invoke(["api-keys", "rotate", old["key_id"], "--format", other])
        assert again.exit_code == result.exit_code


class TestListShowsExpiry:
    def _expire(self, key_id: str) -> None:
        past = utc_now() - timedelta(minutes=1)
        assert _get_registry().operational.api_key_store.update(key_id, expires_at=past)

    def test_json_rows_carry_expired_beside_revoked_and_the_exit_is_unchanged(
        self,
    ) -> None:
        live = _create(name="live")["key_id"]
        gone = _create(name="gone")["key_id"]
        self._expire(gone)
        for fmt in ("json", "text"):
            result = _invoke(["api-keys", "list", "--format", fmt])
            assert result.exit_code == EXIT_OK, _redacted(result.output)
        listed = json.loads(_invoke(["api-keys", "list", "--format", "json"]).stdout)
        rows = {r["key_id"]: r for r in listed["keys"]}
        assert rows[live]["expired"] is False
        assert rows[live]["revoked"] is False
        assert "expires_at" not in rows[live]
        assert rows[gone]["expired"] is True
        assert rows[gone]["revoked"] is False
        assert rows[gone]["expires_at"]

    def test_the_text_table_has_an_expired_column(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COLUMNS", "160")
        live = _create(name="live")["key_id"]
        gone = _create(name="gone")["key_id"]
        self._expire(gone)
        lines = plain(_invoke(["api-keys", "list", "--format", "text"]).output)
        lines = lines.splitlines()
        header = next(ln for ln in lines if "revoked" in ln)
        cols = [c.strip() for c in re.split("[│┃]", header)]
        assert cols.index("expired") == cols.index("revoked") + 1

        def cell(key_id: str, column: str) -> str:
            row = next(ln for ln in lines if key_id in ln)
            return [c.strip() for c in re.split("[│┃]", row)][cols.index(column)]

        assert cell(gone, "expired") == "yes"
        assert cell(gone, "revoked") == "no"
        assert cell(live, "expired") == "no"


class TestRotateRefusesEndingKeys:
    @pytest.mark.parametrize("fmt", ["json", "text"])
    def test_an_expired_key_is_refused_with_nothing_minted(self, fmt: str) -> None:
        key_id = _create(name="old")["key_id"]
        past = utc_now() - timedelta(minutes=1)
        store = _get_registry().operational.api_key_store
        assert store.update(key_id, expires_at=past)
        for grace in ([], ["--grace", "7d"]):
            result = _invoke(["api-keys", "rotate", key_id, "--format", fmt, *grace])
            assert result.exit_code == EXIT_VALIDATION, _redacted(result.output)
            assert TOKEN_PREFIX not in result.output
        assert len(store.list()) == 1
        assert store.get(key_id).expires_at == past

    def test_a_key_already_in_grace_is_refused_not_extended(self) -> None:
        key_id = _create(name="old")["key_id"]
        first = _invoke(
            ["api-keys", "rotate", key_id, "--grace", "1h", "--format", "json"]
        )
        assert first.exit_code == EXIT_OK, _redacted(first.output)
        store = _get_registry().operational.api_key_store
        window = store.get(key_id).expires_at
        again = _invoke(
            ["api-keys", "rotate", key_id, "--grace", "7d", "--format", "json"]
        )
        assert again.exit_code == EXIT_VALIDATION, _redacted(again.output)
        assert len(store.list()) == 2
        assert store.get(key_id).expires_at == window


class TestANaiveExpiryNeverReachesTheListing:
    def test_a_hand_built_naive_expiry_is_refused_and_list_still_works(self) -> None:
        key = _create()
        naive = datetime.fromisoformat("2999-01-01T00:00:00")
        refused = cli_api_keys._execute(
            build_update_command(
                key["key_id"], expires_at=naive, requested_by="test:cli"
            )
        )
        assert refused.status == CommandStatus.REJECTED
        assert _stored(key["key_id"]).expires_at is None
        for fmt in ("json", "text"):
            listed = _invoke(["api-keys", "list", "--format", fmt])
            assert listed.exit_code == EXIT_OK, _redacted(listed.output)
        rows = json.loads(_invoke(["api-keys", "list", "--format", "json"]).stdout)
        assert [r["key_id"] for r in rows["keys"]] == [key["key_id"]]
