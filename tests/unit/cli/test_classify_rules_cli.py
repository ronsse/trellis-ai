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

from trellis.stores.base.event_log import EventType
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_OK, EXIT_STORE, EXIT_VALIDATION
from trellis_cli.main import app as root_app
from trellis_cli.stores import _get_registry

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

    def test_reserved_namespace_alias_key_apply_exits_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``sensitivity -> privacy`` was previously accepted: only the
        clean target was checked, never the alias key."""
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "apply-rule",
                "domain_alias",
                "sensitivity",
                "privacy",
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

    def test_short_tag_keyword_apply_exits_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A one- or two-letter key defeats the classifier's two-hit
        minimum on its own under substring semantics."""
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "ml",
                "finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"
        assert "3-character minimum" in payload["message"]

        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        assert not [r for r in rules if r["key"] == "ml"]

    def test_nan_precision_cli_exits_validation_and_writes_no_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Click's FLOAT type parses ``"nan"`` via Python's ``float()``,
        unlike the REST DTO's ``allow_inf_nan=False`` -- caught here,
        before a Command is even built."""
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "zzzkw",
                "finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--precision",
                "nan",
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output

        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        assert not [r for r in rules if r["key"] == "zzzkw"]

    def test_infinite_lift_cli_exits_validation_and_writes_no_row(
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
                "--lift",
                "inf",
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output

        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        assert not [r for r in rules if r["key"] == "zzzkw"]

    def test_nan_precision_cli_text_format_also_exits_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit code must not depend on ``--format``
        (``tests/unit/test_format_exit_parity_rule.py``)."""
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "zzzkw",
                "finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--precision",
                "nan",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output

    def test_nan_precision_cli_json_format_still_has_a_parseable_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There used to be a pre-check here, ahead of the handler, that
        printed to stderr and exited before a ``--format json`` caller
        ever got a payload -- leaving stdout empty for an otherwise
        ordinary rejection. The rejection now reaches
        ``ClassifyRuleApplyHandler``'s own metric validation through the
        normal command path, so ``--format json`` gets the same
        ``{"status": "rejected", ...}`` shape every other refusal does."""
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "zzzkw",
                "finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--precision",
                "nan",
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        assert result.stdout.strip(), "stdout must not be empty under --format json"
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"

    def test_apply_rule_risk_warning_on_stderr_json_stdout_stays_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N3: the UI's risk copy has a CLI stderr counterpart now.

        Printed unconditionally, before the write even executes, so
        ``--format json`` stdout must still parse as exactly the success
        payload -- the warning never leaks onto stdout.
        """
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "finance",
                "finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_OK, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "success"
        assert "domain" in result.stderr
        assert "keyword" in result.stderr.lower()

    def test_apply_rule_risk_warning_on_stderr_text_format_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same warning, text format: still on stderr, exit code unchanged."""
        _init_stores(tmp_path, monkeypatch)
        result = _invoke(
            [
                "apply-rule",
                "domain_alias",
                "fin",
                "finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
            ]
        )
        assert result.exit_code == EXIT_OK, result.output
        assert "Merge" in result.stderr
        assert "fin" in result.stderr
        assert "does not change the domain on any document already stored" in (
            result.stderr
        )
        assert "Merge" not in result.stdout

    def _applied_event_count(self) -> int:
        return len(
            _get_registry().operational.event_log.get_events(
                event_type=EventType.CLASSIFY_RULE_APPLIED, limit=100
            )
        )

    def test_leading_whitespace_key_cli_exits_validation_writes_no_row_no_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A whitespace-padded key is long enough to pass the 3-character
        tag_keyword floor on its raw, unstripped length -- the floor check
        never sees the 1-character token that actually gets stored once
        ``ClassifyOverrideRow`` strips it. The shared identifier validator
        must refuse the whitespace before the floor check ever runs."""
        _init_stores(tmp_path, monkeypatch)
        before = self._applied_event_count()
        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "  a",
                "infrastructure",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"

        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        assert not [
            r for r in rules if r["kind"] == "tag_keyword" and r["key"].strip() == "a"
        ]
        assert self._applied_event_count() == before

    def test_whitespace_target_cli_exits_validation_writes_no_row_no_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A target padded with whitespace is a different string from the
        reserved namespace name it stringwise-escapes, bypassing the
        reserved-target check unless the padding is refused outright."""
        _init_stores(tmp_path, monkeypatch)
        before = self._applied_event_count()
        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "zzzkw",
                " retention",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"

        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        assert not [r for r in rules if r["key"] == "zzzkw"]
        assert self._applied_event_count() == before

    def test_whitespace_chain_bypass_target_cli_rejected_no_row_no_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With ``finance -> treasury`` already in the overlay, a target of
        ``" finance"`` (leading space) is a different string from
        ``"finance"`` and would otherwise slip past the chain check that
        compares merge destinations by exact string match."""
        _init_stores(tmp_path, monkeypatch)
        seed = _invoke(
            [
                "apply-rule",
                "domain_alias",
                "finance",
                "treasury",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert seed.exit_code == EXIT_OK, seed.output
        before = self._applied_event_count()

        result = _invoke(
            [
                "apply-rule",
                "domain_alias",
                "fin",
                " finance",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"

        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        assert not [
            r for r in rules if r["kind"] == "domain_alias" and r["key"] == "fin"
        ]
        assert self._applied_event_count() == before

    def test_whitespace_reserved_alias_key_cli_exits_validation_writes_no_row_no_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``" sensitivity"`` (leading space) is a different string from
        the reserved namespace name ``"sensitivity"`` and would otherwise
        slip past the reserved-key check."""
        _init_stores(tmp_path, monkeypatch)
        before = self._applied_event_count()
        result = _invoke(
            [
                "apply-rule",
                "domain_alias",
                " sensitivity",
                "privacy",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"

        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        assert not [r for r in rules if r["kind"] == "domain_alias"]
        assert self._applied_event_count() == before

    def test_whitespace_source_event_id_cli_exits_validation_writes_no_row_no_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Surrounding whitespace on ``--source-event-id`` must not split
        row vs payload: ``source_event_id`` runs through the same
        identifier validator as key/target, so it is refused before
        anything is written, not stripped-and-stored by
        ``ClassifyOverrideRow``'s own whitespace stripping."""
        _init_stores(tmp_path, monkeypatch)
        before = self._applied_event_count()
        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "zzzkw",
                "finance",
                "--source-event-id",
                f" {_SOURCE_EVENT_ID} ",
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"

        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        assert not [r for r in rules if r["key"] == "zzzkw"]
        assert self._applied_event_count() == before

    def test_overlong_source_event_id_cli_exits_validation_writes_no_row_no_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pins the length cap through the CLI surface, not just the
        handler directly."""
        _init_stores(tmp_path, monkeypatch)
        before = self._applied_event_count()
        result = _invoke(
            [
                "apply-rule",
                "tag_keyword",
                "zzzkw",
                "finance",
                "--source-event-id",
                "e" * 10_000,
                "--format",
                "json",
            ]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "rejected"

        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        assert not [r for r in rules if r["key"] == "zzzkw"]
        assert self._applied_event_count() == before

    def test_apply_rule_stamps_cli_identity_on_the_audit_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``apply-rule`` always calls ``_audit_identity()`` as
        ``Command.metadata`` -- ``key_id`` is always ``None`` locally (no
        authenticated credential), ``key_name`` is the stamped
        ``"cli:<os user>"``, and the handler prefers that identity over
        ``requested_by`` for ``applied_by``."""
        _init_stores(tmp_path, monkeypatch)
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

        events = _get_registry().operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_APPLIED, limit=10
        )
        payload = events[-1].payload
        assert payload["key_id"] is None
        assert payload["key_name"].startswith("cli:")
        assert payload["applied_by"] == payload["key_name"]


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

    def test_revoke_rule_stamps_cli_identity_but_not_on_revoked_by(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``revoke-rule`` calls the same ``_audit_identity()`` as
        ``apply-rule`` (``key_id`` always ``None`` locally, ``key_name``
        the stamped ``"cli:<os user>"``), but unlike Apply's
        ``applied_by``, Revoke's ``revoked_by`` is ``requested_by``
        verbatim -- the CLI's constant ``"cli:classify-revoke-rule"``
        default, not the stamped identity."""
        _init_stores(tmp_path, monkeypatch)
        self._apply(tmp_path, monkeypatch)

        result = _invoke(["revoke-rule", "tag_keyword", "zzzkw", "--format", "json"])
        assert result.exit_code == EXIT_OK, result.output

        events = _get_registry().operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_REVOKED, limit=10
        )
        payload = events[-1].payload
        assert payload["key_id"] is None
        assert payload["key_name"].startswith("cli:")
        assert payload["revoked_by"] == "cli:classify-revoke-rule"

    def test_revoke_rule_risk_warning_on_stderr_json_stdout_stays_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N3: revoke's UI risk copy has a CLI stderr counterpart too."""
        _init_stores(tmp_path, monkeypatch)
        self._apply(tmp_path, monkeypatch)

        result = _invoke(["revoke-rule", "tag_keyword", "zzzkw", "--format", "json"])
        assert result.exit_code == EXIT_OK, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "success"
        assert "Revoke" in result.stderr
        assert "retag" in result.stderr

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

    def test_revoke_exposing_a_config_chain_is_refused_both_formats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Revoking an overlay alias can resurface a config.yaml alias
        that chains with a different, still-applied overlay alias --
        refused with ``invalid_alias_rule``, the same code Apply uses, and
        the shadowing row is left in place (mirrors
        ``test_classify_rules_routes.py::TestRevokeRule::
        test_revoke_exposing_a_config_chain_is_refused_4xx``).
        """
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        init_result = runner.invoke(root_app, ["admin", "init"])
        assert init_result.exit_code == EXIT_OK, init_result.output
        config_path = tmp_path / "config" / "config.yaml"
        config_path.write_text(
            "classify:\n  domain_aliases:\n    b-alias: x-canonical\n",
            encoding="utf-8",
        )

        shadow = _invoke(
            [
                "apply-rule",
                "domain_alias",
                "b-alias",
                "y-canonical",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert shadow.exit_code == EXIT_OK, shadow.output

        second = _invoke(
            [
                "apply-rule",
                "domain_alias",
                "x-canonical",
                "q-final",
                "--source-event-id",
                _SOURCE_EVENT_ID,
                "--format",
                "json",
            ]
        )
        assert second.exit_code == EXIT_OK, second.output

        json_revoke = _invoke(
            ["revoke-rule", "domain_alias", "b-alias", "--format", "json"]
        )
        assert json_revoke.exit_code == EXIT_VALIDATION, json_revoke.output
        payload = json.loads(json_revoke.stdout)
        assert payload["status"] == "rejected"

        text_revoke = _invoke(["revoke-rule", "domain_alias", "b-alias"])
        assert text_revoke.exit_code == EXIT_VALIDATION, text_revoke.output

        rules = _effective_rules_json(tmp_path, monkeypatch)["rules"]
        row = next(
            r for r in rules if r["kind"] == "domain_alias" and r["key"] == "b-alias"
        )
        assert row["source"] == "overlay"
        assert row["target"] == "y-canonical"

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

    def test_config_only_chain_is_exit_internal_not_traceback_both_formats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A chain introduced purely by editing config.yaml -- no overlay
        write to have caught it -- must read as a composed error, never a
        traceback (mirrors
        ``test_classify_rules_routes.py::TestListRules::
        test_config_only_chain_is_400_not_500``)."""
        _init_stores(tmp_path, monkeypatch)
        config_path = tmp_path / "config" / "config.yaml"
        config_path.write_text(
            "classify:\n  domain_aliases:\n    fin: finance\n    finance: treasury\n",
            encoding="utf-8",
        )

        json_result = _invoke(["effective-rules", "--format", "json"])
        assert json_result.exit_code == EXIT_INTERNAL, json_result.output
        payload = json.loads(json_result.stdout)
        assert payload["status"] == "error"

        text_result = _invoke(["effective-rules"])
        assert text_result.exit_code == EXIT_INTERNAL, text_result.output
        assert "Traceback" not in text_result.output
        # Pins C1: a mutation that re-raises the chain's bare ``ValueError``
        # instead of composing ``typer.Exit(code=EXIT_INTERNAL)`` would
        # still exit 1 (``ValueError`` is not caught, so Click's own
        # unhandled-exception handling could coincidentally produce the
        # same code) but CliRunner.invoke's ``result.exception`` would be
        # that ``ValueError``, not a ``SystemExit`` -- and the composed
        # message carries the chain detail the bare exception never
        # reaches the CLI's own formatting for.
        assert isinstance(text_result.exception, SystemExit)
        assert "appear as both an alias and a merge destination" in text_result.output


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
