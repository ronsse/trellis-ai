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
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.core.settings_registry import (
    SETTINGS_REGISTRY,
    SettingSpec,
    validate_value,
)
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


# ---------------------------------------------------------------------------
# B6 (re-gate #867): every registry entry, not just the twelve original
# WriteBehaviourConfig fields, must be settable/unsettable without a crash
# ---------------------------------------------------------------------------


def _literal_for(spec: SettingSpec) -> str:
    """A value *spec*'s own ``validate_value`` accepts, as a plain string --
    used for both the CLI argument and the env-var override, so this test
    needs no hand-maintained per-knob table (which is exactly the kind of
    table that went stale and caused B6 in the first place).

    Every entry ships a valid, in-range ``default`` except
    ``trace_extraction_min_confidence`` (``None`` means "no gate"), which
    falls back to its declared ``minimum``.
    """
    if spec.default is not None:
        if spec.type == "bool":
            return "true" if spec.default else "false"
        return str(spec.default)
    if spec.enum:
        return spec.enum[0]
    if spec.minimum is not None:
        return str(spec.minimum)
    return "0"


def _b6_cases() -> list[tuple[str, str, bool, str]]:
    cases: list[tuple[str, str, bool, str]] = []
    for spec in SETTINGS_REGISTRY.values():
        actions = ("set", "unset") if spec.settings_writable else ("unset",)
        cases.extend(
            (spec.name, action, env_set, output_format)
            for action in actions
            for env_set in (True, False)
            for output_format in ("json", "text")
        )
    return cases


_B6_CASES = _b6_cases()
_B6_IDS = [
    f"{name}-{action}-env{'On' if env_set else 'Off'}-{fmt}"
    for name, action, env_set, fmt in _B6_CASES
]


class TestEffectiveValueAcrossRegistry:
    """Re-gate #867 B6: ``set``/``unset`` used to resolve the "env wins"
    success note by indexing
    ``WriteBehaviourConfig.from_env_and_settings().as_dict()[spec.name]``
    directly -- a mapping that only ever held the twelve original fields.
    Every knob a later PR added to :data:`SETTINGS_REGISTRY` then raised
    ``KeyError`` here whenever its env var was set, turning what the
    governed executor had already recorded as a successful write (row
    and event both persisted) into an uncaught exception and exit code 1.

    ``_env_effective_value`` (``trellis_cli/admin_settings.py``) now
    delegates to :func:`trellis_api.routes.settings._effective_values`,
    the one resolver ``GET /settings`` already uses and which already
    covers every entry. This proves that across the *whole* registry --
    every entry, both actions it supports, the env var set and unset,
    both output formats -- not just the fields the old mapping knew
    about.
    """

    @pytest.mark.parametrize(
        ("name", "action", "env_set", "output_format"), _B6_CASES, ids=_B6_IDS
    )
    def test_exits_ok_with_well_formed_output(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        action: str,
        env_set: bool,
        output_format: str,
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        spec = SETTINGS_REGISTRY[name]
        literal = _literal_for(spec)

        if env_set:
            monkeypatch.setenv(spec.env_var, literal)
        else:
            monkeypatch.delenv(spec.env_var, raising=False)

        fmt_args = ["--format", output_format] if output_format == "json" else []
        args = (
            ["set", name, literal, *fmt_args]
            if action == "set"
            else [
                "unset",
                name,
                *fmt_args,
            ]
        )

        result = _invoke(args)

        assert result.exception is None, result.output
        assert result.exit_code == EXIT_OK, result.output
        assert "Traceback" not in result.output

        if output_format == "json":
            payload = json.loads(result.stdout)
            assert payload["status"] == "success"
            assert isinstance(payload["command_id"], str)
            assert payload["command_id"]
        else:
            assert "settings.update" in plain(result.output)


def _env_pinned_cases() -> list[tuple[str, str]]:
    """``(name, action)`` with the env var always set -- the one branch
    ``_truthfulness_note`` prints an "effective value" for.
    """
    cases: list[tuple[str, str]] = []
    for spec in SETTINGS_REGISTRY.values():
        actions = ("set", "unset") if spec.settings_writable else ("unset",)
        cases.extend((spec.name, action) for action in actions)
    return cases


_ENV_PINNED_CASES = _env_pinned_cases()
_ENV_PINNED_IDS = [f"{name}-{action}" for name, action in _ENV_PINNED_CASES]


class TestEffectiveValueNoteContent:
    """Re-gate #867 M30: ``TestEffectiveValueAcrossRegistry`` above proves
    every registry entry exits 0 under ``--format json``/``text`` with
    the env var both set and unset -- it never reads ``payload["note"]``,
    so a mutant that hard-codes ``_truthfulness_note``'s ``effective``
    local to ``None`` (dropping the real
    ``_env_effective_value(spec, settings_values)`` call entirely) still
    passes every one of those cases: exit 0 and a well-formed envelope
    say nothing about whether the printed value is the *real* one.

    This computes the expected value independently, via
    :func:`validate_value`'s own coercion of the same literal used as the
    env var -- not via ``_env_effective_value``/``_effective_values``, so
    a defect in either of those is still visible here -- and asserts the
    note's ``(effective value: ...)`` text matches it exactly, for both
    the ``set`` (not-reset) and ``unset`` (reset) wordings.
    """

    @pytest.mark.parametrize(("name", "action"), _ENV_PINNED_CASES, ids=_ENV_PINNED_IDS)
    def test_note_echoes_the_real_env_resolved_value(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        action: str,
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        spec = SETTINGS_REGISTRY[name]
        literal = _literal_for(spec)
        monkeypatch.setenv(spec.env_var, literal)
        expected = validate_value(spec, literal)

        args = (
            ["set", name, literal, "--format", "json"]
            if action == "set"
            else ["unset", name, "--format", "json"]
        )
        result = _invoke(args)

        assert result.exit_code == EXIT_OK, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "success"
        note = payload["note"]
        assert note is not None, payload
        match = re.search(r"\(effective value: (.*?)\)", note)
        assert match is not None, note
        assert match.group(1) == repr(expected), note
