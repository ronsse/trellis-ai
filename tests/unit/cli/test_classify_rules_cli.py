"""Tests for ``trellis classify apply-rule|revoke-rule|effective-rules``.

CLI parity for the governed Apply/Revoke review flow whose REST half is
covered by ``tests/unit/api/test_classify_rules_routes.py``. Both surfaces
submit the same ``Operation.CLASSIFY_RULE_APPLY``/``CLASSIFY_RULE_REVOKE``
commands through :func:`~trellis.mutate.build_classify_rules_executor` and
read through the same :func:`~trellis.classify.factory.effective_classify_rule_rows`,
so this file does not re-prove the handlers' validation rules -- it proves
the CLI wiring: argument plumbing, ``--format`` output shapes, and
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

from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.main import app as root_app

runner = CliRunner()

_SOURCE_EVENT_ID = "01EVT000000000000000000"


def _invoke(args: list[str]):  # type: ignore[no-untyped-def]
    return runner.invoke(root_app, ["classify", *args])


def _init_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    init_result = runner.invoke(root_app, ["admin", "init"])
    assert init_result.exit_code == EXIT_OK, init_result.output


def _effective_rules_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:  # type: ignore[type-arg]
    result = _invoke(["effective-rules", "--format", "json"])
    assert result.exit_code == EXIT_OK, result.output
    return json.loads(result.stdout)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# apply-rule
# ---------------------------------------------------------------------------


class TestApplyRule:
    def test_json_success_shape_and_visible_without_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "zzzkw",
                "finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--support",
                "10",
                "--precision",
                "0.9",
                "--lift",
                "0.2",
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_OK, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "success"
        assert payload["created_id"] == "tag_keyword:zzzkw"
        assert payload["warnings"] == []

        # Same process, no restart: the overlay row is immediately visible
        # through the read-only sibling command.
        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        row = next(r for r in rules if r["key"] == "zzzkw")
        assert row["source"] == "overlay"
        assert row["target"] == "finance"
        assert row["applied_at"] is not None

    def test_text_success_mentions_the_operation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "zzzkw",
                "finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
            ]
        )
        assert result.exit_code == EXIT_OK, result.output
        assert "classify_rule.apply" in plain(result.output)

    def test_domain_alias_apply_is_visible_and_badged_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Mirrors test_json_success_shape_and_visible_without_restart above,
        # but for kind="domain_alias" -- effective_classify_rule_rows badges
        # the two kinds through separate branches (one per loop in
        # trellis.classify.factory), so a tag_keyword-only regression test
        # cannot see a bug confined to the domain_alias branch.
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "apply-rule",
                "domain_alias",
                "zzzalias",
                "zzzcanonical",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_OK, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "success"
        assert payload["created_id"] == "domain_alias:zzzalias"

        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        row = next(
            r for r in rules if r["kind"] == "domain_alias" and r["key"] == "zzzalias"
        )
        assert row["source"] == "overlay"
        assert row["target"] == "zzzcanonical"
        assert row["applied_at"] is not None

    def test_by_flag_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "zzzkw",
                "finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--by",
                "audit-test",
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_OK, result.output

    def test_unknown_kind_exits_validation_both_formats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        json_result = _invoke(
            [
                "apply-rule",
                "bogus_kind",
                "zzzkw",
                "finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert json_result.exit_code == EXIT_VALIDATION, json_result.output
        payload = json.loads(json_result.stdout)
        assert payload["status"] == "rejected"

        text_result = _invoke(
            [
                "apply-rule",
                "bogus_kind",
                "zzzkw",
                "finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
            ]
        )
        assert text_result.exit_code == EXIT_VALIDATION, text_result.output

    def test_missing_source_event_id_is_a_cli_usage_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--source-event-id`` has no default (``...``) -- Typer/Click
        refuses before the command body runs (a usage error, which
        happens to share :data:`EXIT_VALIDATION`'s value of 2 by
        coincidence of Click's own convention, not because the executor
        ran), so the overlay must stay empty: no command was submitted.
        """
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(["apply-rule", "tag_keyword", "zzzkw", "finance"])
        assert result.exit_code != EXIT_OK
        assert "Missing option" in result.output
        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        assert not [r for r in rules if r["key"] == "zzzkw"]

    def test_self_mapping_alias_apply_exits_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "apply-rule",
                "domain_alias",
                "widgets",
                "widgets",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"
        assert "itself" in payload["message"]

        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        assert not [
            r for r in rules if r["kind"] == "domain_alias" and r["key"] == "widgets"
        ]

    def test_chained_alias_apply_exits_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        first = _invoke(
            [
                "apply-rule",
                "domain_alias",
                "b-alias",
                "c-canonical",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert first.exit_code == EXIT_OK, first.output

        chained = _invoke(
            [
                "apply-rule",
                "domain_alias",
                "a-alias",
                "b-alias",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert chained.exit_code == EXIT_VALIDATION, chained.output
        payload = json.loads(chained.stdout)
        assert payload["status"] == "rejected"

        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        assert not [
            r for r in rules if r["kind"] == "domain_alias" and r["key"] == "a-alias"
        ]

    def test_reserved_namespace_target_apply_exits_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "secretword",
                "sensitivity",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"
        assert "reserved namespace" in payload["message"]


# ---------------------------------------------------------------------------
# revoke-rule
# ---------------------------------------------------------------------------


class TestRevokeRule:
    def _apply(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "zzzkw",
                "finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_OK, result.output

    def test_revoke_overlay_row_succeeds_and_removes_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        self._apply(tmp_path, monkeypatch)

        result = _invoke(["revoke-rule", "tag_keyword", "zzzkw", "--format", "json"])
        assert result.exit_code == EXIT_OK, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "success"

        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        assert not [r for r in rules if r["key"] == "zzzkw"]

    def test_text_success_mentions_the_operation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        self._apply(tmp_path, monkeypatch)

        result = _invoke(["revoke-rule", "tag_keyword", "zzzkw"])
        assert result.exit_code == EXIT_OK, result.output
        assert "classify_rule.revoke" in plain(result.output)

    def test_revoke_unknown_key_exits_validation_both_formats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)

        json_result = _invoke(
            ["revoke-rule", "tag_keyword", "doesnotexist", "--format", "json"]
        )
        assert json_result.exit_code == EXIT_VALIDATION, json_result.output
        payload = json.loads(json_result.stdout)
        assert payload["status"] == "rejected"
        assert "No applied tag_keyword rule found" in payload["message"]

        text_result = _invoke(["revoke-rule", "tag_keyword", "doesnotexist"])
        assert text_result.exit_code == EXIT_VALIDATION, text_result.output

    def test_revoke_config_sourced_rule_is_refused_not_a_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rule that comes from ``config.yaml`` was never written by the
        overlay store and cannot be un-applied through it (mirrors
        ``test_classify_rules_routes.py::test_revoke_config_sourced_rule_is_refused_4xx``).
        """
        _init_stores(tmp_path, monkeypatch)
        config_path = tmp_path / "config" / "config.yaml"
        config_path.write_text(
            "classify:\n  domain_keywords:\n    finance:\n      - cfgword\n",
            encoding="utf-8",
        )

        result = _invoke(["revoke-rule", "tag_keyword", "cfgword", "--format", "json"])
        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"
        assert "config.yaml" in payload["message"]


# ---------------------------------------------------------------------------
# effective-rules
# ---------------------------------------------------------------------------


class TestEffectiveRules:
    def test_json_shape_includes_the_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        body = _effective_rules_json(tmp_path, monkeypatch)
        assert body["count"] > 0
        assert all(
            r["source"] in ("default", "config", "overlay") for r in body["rules"]
        )
        assert not [r for r in body["rules"] if r["source"] == "overlay"]

    def test_text_renders_without_error_and_badges_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        apply_result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "zzzkw",
                "finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert apply_result.exit_code == EXIT_OK, apply_result.output

        result = _invoke(["effective-rules"])
        assert result.exit_code == EXIT_OK, result.output
        output = plain(result.output)
        assert "zzzkw" in output
        assert "overlay" in output

    def test_exit_code_is_ok_regardless_of_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        assert _invoke(["effective-rules"]).exit_code == EXIT_OK
        assert _invoke(["effective-rules", "--format", "json"]).exit_code == EXIT_OK


# ---------------------------------------------------------------------------
# Degraded overlay store — reads degrade, writes refuse (B4)
# ---------------------------------------------------------------------------


def _corrupt_overlay(tmp_path: Path) -> None:
    stores_dir = tmp_path / "data" / "stores"
    stores_dir.mkdir(parents=True, exist_ok=True)
    (stores_dir / "classify_overrides.json").write_text(
        "{not valid json", encoding="utf-8"
    )


class TestDegradedOverlayStore:
    def test_effective_rules_reports_degraded_store_and_exit_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        _corrupt_overlay(tmp_path)

        json_result = _invoke(["effective-rules", "--format", "json"])
        assert json_result.exit_code == EXIT_STORE, json_result.output
        payload = json.loads(json_result.stdout)
        assert payload["status"] == "degraded"
        assert payload["store_degradation"] is not None

        text_result = _invoke(["effective-rules"])
        assert text_result.exit_code == EXIT_STORE, text_result.output
        assert "DEGRADED" in plain(text_result.output)

    def test_apply_on_degraded_store_exits_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        _corrupt_overlay(tmp_path)

        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "zzzkw",
                "finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_STORE, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"

    def test_revoke_on_degraded_store_exits_store_not_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        _corrupt_overlay(tmp_path)

        result = _invoke(["revoke-rule", "tag_keyword", "anything", "--format", "json"])
        assert result.exit_code == EXIT_STORE, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"
        assert "No applied" not in payload["message"]

    def test_backfill_on_degraded_overlay_refuses_with_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_stores(tmp_path, monkeypatch)
        _corrupt_overlay(tmp_path)

        json_result = _invoke(["backfill", "--format", "json"])
        assert json_result.exit_code == EXIT_STORE, json_result.output
        payload = json.loads(json_result.stdout)
        assert payload["status"] == "error"
        assert payload["error_type"] == "ConfigError"

        text_result = _invoke(["backfill"])
        assert text_result.exit_code == EXIT_STORE, text_result.output
        assert "CONFIG_ERROR" in plain(text_result.output)
