"""Tests for ``trellis admin settings set|unset`` (PR 3, CLI parity).

Both subcommands submit the same ``Operation.SETTINGS_UPDATE`` command
the REST route and MCP surface do (see ``trellis_cli/admin_settings.py``
and ``tests/unit/api/test_settings_routes.py``), so this file does not
re-prove the handler's validation rules (covered end to end in
``tests/unit/mutate/test_settings_update_handler.py``) -- it proves the
CLI wiring itself: argument plumbing, ``--format`` output shapes, and
exit-code parity between them (``tests/unit/test_format_exit_parity_rule.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from tests.cli_output import plain

if TYPE_CHECKING:
    import pytest

from trellis.mutate.policy_source import POLICY_FILENAME
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.settings_store import SettingsStore, resolve_settings_path
from trellis_cli.exit_codes import EXIT_OK, EXIT_POLICY, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.main import app as root_app

runner = CliRunner()


def _invoke(args: list[str]):  # type: ignore[no-untyped-def]
    return runner.invoke(root_app, ["admin", "settings", *args])


def _init_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    init_result = runner.invoke(root_app, ["admin", "init"])
    assert init_result.exit_code == EXIT_OK, init_result.output


def _store(tmp_path: Path) -> SettingsStore:
    stores_dir = tmp_path / "data" / "stores"
    path = resolve_settings_path(stores_dir)
    assert path is not None
    return SettingsStore(path)


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------


class TestSet:
    def test_json_success_shape_and_persists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["set", "pack_holdout_rate", "0.3", "--format", "json"])
        assert result.exit_code == EXIT_OK, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "success"
        assert payload["created_id"] == "pack_holdout_rate"
        assert payload["warnings"] == []
        assert _store(tmp_path).get("pack_holdout_rate").value == 0.3

    def test_text_success_mentions_the_operation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["set", "pack_holdout_rate", "0.3"])
        assert result.exit_code == EXIT_OK, result.output
        assert "settings.update" in plain(result.output)

    def test_by_flag_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "set",
                "pack_holdout_rate",
                "0.3",
                "--by",
                "audit-test",
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_OK, result.output

    def test_boolean_setting_accepts_string_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["set", "memory_extraction", "true", "--format", "json"])
        assert result.exit_code == EXIT_OK, result.output
        assert _store(tmp_path).get("memory_extraction").value is True

    def test_unknown_name_exits_validation_both_formats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        json_result = _invoke(["set", "not_a_real_setting", "1", "--format", "json"])
        assert json_result.exit_code == EXIT_VALIDATION, json_result.output
        text_result = _invoke(["set", "not_a_real_setting", "1"])
        assert text_result.exit_code == EXIT_VALIDATION, text_result.output

    def test_wrong_type_exits_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["set", "memory_extraction", "not-a-bool", "--format", "json"])
        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"
        assert "boolean" in payload["message"]

    def test_out_of_range_exits_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["set", "pack_holdout_rate", "5.0", "--format", "json"])
        assert result.exit_code == EXIT_VALIDATION, result.output

    def test_env_only_setting_refuses_a_write_on_the_cli_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B3 (re-gate #867): ``graph_max_subgraph_depth`` ships
        ``settings_writable=False`` -- the same ``SettingsUpdateHandler``
        the REST route uses refuses this write here too, with the env
        var named in the message rather than a bare validation error.
        """
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["set", "graph_max_subgraph_depth", "10", "--format", "json"])
        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"
        assert "TRELLIS_GRAPH_MAX_SUBGRAPH_DEPTH" in payload["message"]
        assert _store(tmp_path).get("graph_max_subgraph_depth") is None

    def test_entity_type_setting_deny_policy_refuses_the_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Required item 1's third check, CLI half: a deny policy scoped
        to ``entity_type=setting`` blocks ``settings set`` too, through
        the real :func:`build_settings_executor` Stage 2 gate -- the same
        proof as ``test_settings_routes.py``'s route-level test.
        """
        _init_stores(tmp_path, monkeypatch)
        policy = Policy(
            policy_type=PolicyType.MUTATION,
            scope=PolicyScope(level="entity_type", value="setting"),
            rules=[PolicyRule(operation="settings.update", action="deny")],
            enforcement=Enforcement.ENFORCE,
        )
        (tmp_path / "data" / "stores" / POLICY_FILENAME).write_text(
            json.dumps({"policies": [policy.model_dump(mode="json")]}),
            encoding="utf-8",
        )

        result = _invoke(["set", "pack_holdout_rate", "0.9", "--format", "json"])

        assert result.exit_code == EXIT_POLICY, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"
        assert _store(tmp_path).get("pack_holdout_rate") is None

    def test_degraded_store_exits_store_not_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M11 (re-gate #857): a degraded settings file must exit
        :data:`EXIT_STORE` (5) -- "backend down, don't retry with
        different input" -- never :data:`EXIT_VALIDATION` (2), which
        would tell an operator to fix their arguments. The route's own
        test of the same refusal is ``test_settings_routes.py``'s
        ``test_degraded_store_refuses_the_write``; this is the CLI half,
        through :func:`~trellis_cli.exit_codes.refusal_exit_code`.
        """
        _init_stores(tmp_path, monkeypatch)
        path = resolve_settings_path(tmp_path / "data" / "stores")
        assert path is not None
        path.write_text("{not valid json", encoding="utf-8")

        result = _invoke(["set", "pack_holdout_rate", "0.3", "--format", "json"])

        assert result.exit_code == EXIT_STORE, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"
        assert "mv " in payload["message"]
        assert ".corrupt" in payload["message"]


# ---------------------------------------------------------------------------
# unset
# ---------------------------------------------------------------------------


class TestUnset:
    def test_removes_an_existing_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        set_result = _invoke(["set", "pack_holdout_rate", "0.4", "--format", "json"])
        assert set_result.exit_code == EXIT_OK, set_result.output
        assert _store(tmp_path).get("pack_holdout_rate") is not None

        unset_result = _invoke(["unset", "pack_holdout_rate", "--format", "json"])
        assert unset_result.exit_code == EXIT_OK, unset_result.output
        assert _store(tmp_path).get("pack_holdout_rate") is None

    def test_unsetting_an_already_unset_name_is_still_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["unset", "pack_holdout_rate", "--format", "json"])
        assert result.exit_code == EXIT_OK, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "success"

    def test_unknown_name_exits_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["unset", "not_a_real_setting", "--format", "json"])
        assert result.exit_code == EXIT_VALIDATION, result.output
