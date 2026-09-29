"""Tests for ``trellis admin api-keys create|list|revoke``.

Exercises the subcommands end-to-end via :class:`typer.testing.CliRunner`
so registration, output formats, the token-shown-once contract, and
exit-code routing are all under contract. Each test isolates its own
``TRELLIS_CONFIG_DIR`` + ``TRELLIS_DATA_DIR`` (the autouse
``_reset_cli_registry`` fixture drops the cached registry between tests).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from tests.cli_output import assert_coloured, force_colour, plain

if TYPE_CHECKING:
    import pytest

from trellis.auth import TOKEN_PREFIX
from trellis_cli import admin_api_keys as cli_api_keys
from trellis_cli.exit_codes import EXIT_OK, EXIT_VALIDATION
from trellis_cli.main import app as root_app

# Invoke via the root app so the ``@app.callback`` routes structlog
# output to stderr — same convention as test_admin_proposals.py.
runner = CliRunner()


def _invoke(args: list[str]):  # type: ignore[no-untyped-def]
    return runner.invoke(root_app, ["admin", *args])


def _init_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    init_result = _invoke(["init"])
    assert init_result.exit_code == EXIT_OK, init_result.output


# The shape of a token line, never a value: ``trellis_ak_`` +
# ``token_hex(6)`` + ``.`` + ``token_urlsafe(32)``.
_TOKEN_LINE = re.compile(
    rf"token:\s+{re.escape(TOKEN_PREFIX)}[0-9a-f]{{12}}\.[A-Za-z0-9_-]{{43}}"
)
_ANY_TOKEN = re.compile(rf"{re.escape(TOKEN_PREFIX)}\S+")


def _redacted(output: str) -> str:
    """``output`` stripped, whitespace-normalised, and carrying no token.

    Assertions read this rather than ``stdout`` so that no failure message
    can print a minted token.
    """
    return " ".join(_ANY_TOKEN.sub("<token>", plain(output)).split())


def _create_key(scopes: str = "read", name: str = "ci") -> dict:
    result = _invoke(
        ["api-keys", "create", "--name", name, "--scopes", scopes, "--format", "json"]
    )
    assert result.exit_code == EXIT_OK, result.output
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_json_prints_token_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        payload = _create_key("read,ingest", name="ci-bot")
        assert payload["token"].startswith(TOKEN_PREFIX)
        assert payload["name"] == "ci-bot"
        assert payload["scopes"] == ["read", "ingest"]
        assert payload["revoked"] is False
        assert "warning" in payload
        assert "secret_hash" not in payload

    def test_create_text_mentions_shown_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["api-keys", "create", "--name", "ci", "--scopes", "read"])
        assert result.exit_code == EXIT_OK, result.output
        assert TOKEN_PREFIX in result.output
        assert "shown once" in result.output

    def test_unknown_scope_is_loud_validation_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "api-keys",
                "create",
                "--name",
                "ci",
                "--scopes",
                "read,write",
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["error"] == "validation_error"
        assert "write" in payload["message"]

    def test_empty_scopes_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["api-keys", "create", "--name", "ci", "--scopes", " , "])
        assert result.exit_code == EXIT_VALIDATION, result.output

    def test_create_text_renders_the_name_verbatim_and_prints_the_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The key is stored before the text arm renders a word of it.

        So a name Rich reads as markup must not abort that arm: ``[/bot]``
        raised ``MarkupError`` after ``store.create``, which exited 1 with
        the key stored and its token -- shown once, by contract -- never
        printed. ``[bold]`` alone would not raise; Rich would eat it.
        """
        _init_stores(tmp_path, monkeypatch)
        force_colour(monkeypatch, cli_api_keys)
        name = "[bold]ci[/bot]"
        result = _invoke(["api-keys", "create", "--name", name, "--scopes", "read"])
        text = _redacted(result.stdout)
        has_token_line = _TOKEN_LINE.search(plain(result.stdout)) is not None
        assert result.exit_code == EXIT_OK, text
        assert_coloured(result.stdout)
        assert f"({name})" in text
        assert has_token_line, text

    def test_scope_error_renders_the_scope_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rejected scope is caller text inside a ``[red]`` message.

        Unescaped, the validation error itself raised ``MarkupError``, so the
        exit was 1 ("unexpected; file a bug") instead of 2.
        """
        _init_stores(tmp_path, monkeypatch)
        force_colour(monkeypatch, cli_api_keys)
        result = _invoke(
            ["api-keys", "create", "--name", "ci", "--scopes", "[bold]r[/x]"]
        )
        assert result.exit_code == EXIT_VALIDATION, plain(result.output)
        rendered = " ".join(assert_coloured(result.stdout).split())
        assert "['[bold]r[/x]']" in rendered


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_list_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["api-keys", "list", "--format", "json"])
        assert result.exit_code == EXIT_OK, result.output
        payload = json.loads(result.stdout)
        assert payload == {"keys": [], "count": 0}

    def test_list_shows_keys_but_never_hash_or_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        created = _create_key("admin", name="root-key")
        result = _invoke(["api-keys", "list", "--format", "json"])
        assert result.exit_code == EXIT_OK, result.output
        payload = json.loads(result.stdout)
        assert payload["count"] == 1
        row = payload["keys"][0]
        assert row["key_id"] == created["key_id"]
        assert row["name"] == "root-key"
        assert row["scopes"] == ["admin"]
        assert row["revoked"] is False
        assert "secret_hash" not in row
        assert "token" not in row
        assert created["token"] not in result.output

    def test_list_text_renders_a_stored_name_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A name ``create --format json`` stored reaches the table as written."""
        _init_stores(tmp_path, monkeypatch)
        _create_key(name="[b]k[/x]")
        monkeypatch.setenv("COLUMNS", "200")  # the table must not fold the cell
        force_colour(monkeypatch, cli_api_keys)
        result = _invoke(["api-keys", "list"])
        text = _redacted(result.stdout)
        assert result.exit_code == EXIT_OK, text
        assert_coloured(result.stdout)
        assert "[b]k[/x]" in text


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


class TestRevoke:
    def test_revoke_live_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        created = _create_key()
        result = _invoke(["api-keys", "revoke", created["key_id"], "--format", "json"])
        assert result.exit_code == EXIT_OK, result.output
        assert json.loads(result.stdout) == {
            "status": "revoked",
            "key_id": created["key_id"],
        }
        listing = json.loads(_invoke(["api-keys", "list", "--format", "json"]).stdout)
        assert listing["keys"][0]["revoked"] is True

    def test_revoke_unknown_key_is_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["api-keys", "revoke", "000000000000", "--format", "json"])
        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["error"] == "unknown_key_id"

    def test_revoke_twice_is_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        created = _create_key()
        first = _invoke(["api-keys", "revoke", created["key_id"]])
        assert first.exit_code == EXIT_OK, first.output
        second = _invoke(["api-keys", "revoke", created["key_id"], "--format", "json"])
        assert second.exit_code == EXIT_VALIDATION, second.output
        payload = json.loads(second.stdout)
        assert payload["error"] == "already_revoked"
