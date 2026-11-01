"""``trellis admin api-keys create`` and the ``external`` scope.

The only legal spelling is ``read,external``: a read-only key whose clearance is
``public``. Anything else that names ``external`` is refused at mint with the
reason, and a refused mint stores no key.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.cli_output import plain
from tests.unit.cli.test_admin_api_keys import _create_key, _init_stores, _invoke
from trellis.auth import clearance_of
from trellis_cli.exit_codes import EXIT_OK, EXIT_VALIDATION

if TYPE_CHECKING:
    from pathlib import Path


def _count(tmp_path: Path) -> int:
    result = _invoke(["api-keys", "list", "--format", "json"])
    assert result.exit_code == EXIT_OK, result.output
    return json.loads(result.stdout)["count"]


def test_read_and_external_mints_a_key_that_reads_at_public(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_stores(tmp_path, monkeypatch)
    created = _create_key("read,external", name="partner")
    assert sorted(created["scopes"]) == ["external", "read"]
    assert clearance_of(set(created["scopes"])) == "public"
    listed = json.loads(_invoke(["api-keys", "list", "--format", "json"]).stdout)
    assert sorted(listed["keys"][0]["scopes"]) == ["external", "read"]


def test_the_text_output_says_what_the_key_does_and_does_not_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_stores(tmp_path, monkeypatch)
    result = _invoke(
        ["api-keys", "create", "--name", "partner", "--scopes", "read,external"]
    )
    assert result.exit_code == EXIT_OK, result.output
    text = " ".join(plain(result.output).split())
    assert "labelled public" in text
    assert "does not hide unlabelled content from other keys" in text


@pytest.mark.parametrize(
    "scopes",
    [
        "external",
        "read,external,admin",
        "read,external,ingest",
        "read,external,mutate",
        "read,external,confidential",
        "read,external,restricted",
        "external,admin",
    ],
)
def test_external_beside_anything_else_is_refused_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scopes: str
) -> None:
    _init_stores(tmp_path, monkeypatch)
    result = _invoke(
        [
            "api-keys",
            "create",
            "--name",
            "partner",
            "--scopes",
            scopes,
            "--format",
            "json",
        ]
    )
    assert result.exit_code == EXIT_VALIDATION, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == "validation_error"
    assert "read,external" in payload["message"]
    assert _count(tmp_path) == 0


def test_a_text_mode_refusal_exits_the_same_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_stores(tmp_path, monkeypatch)
    result = _invoke(
        ["api-keys", "create", "--name", "partner", "--scopes", "read,external,admin"]
    )
    assert result.exit_code == EXIT_VALIDATION, result.output
    assert _count(tmp_path) == 0


def test_the_other_scopes_still_mint_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_stores(tmp_path, monkeypatch)
    for scopes in ("read", "admin", "read,restricted", "ingest,mutate"):
        assert sorted(_create_key(scopes, name=scopes)["scopes"]) == sorted(
            scopes.split(",")
        )
