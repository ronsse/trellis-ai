"""Tests for ``ClassifyRuleApplyHandler`` / ``ClassifyRuleRevokeHandler``.

The REST (``tests/unit/api/test_classify_rules_routes.py``) and CLI
(``tests/unit/cli/test_classify_rules_cli.py``) test files prove the same
handler validation rules through their own surfaces. REST layers a
surface-specific guard in front (the DTO's ``allow_inf_nan=False``) that
stops a bad value before it ever reaches here; the CLI has no finiteness
check of its own and reaches this exact code path unguarded. This file
calls the handlers directly, the same pattern
``test_settings_update_handler.py`` uses, so it is the one place a metric
genuinely reaches ``_validate_classify_rule_metrics`` -- the check every
caller that *can* reach it (REST, CLI, a direct
``MutationExecutor.execute`` from Python) passes through. MCP's
``execute_mutation`` is not one of those callers: the classify-rule
handlers register only on ``build_classify_rules_executor``, which
``build_curate_executor`` (what MCP uses) does not.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trellis.errors import ValidationError
from trellis.mutate.commands import Command, Operation
from trellis.mutate.handlers import ClassifyRuleApplyHandler, ClassifyRuleRevokeHandler
from trellis.schemas.classify_overrides import ClassifyOverrideRow
from trellis.stores.base.event_log import EventType
from trellis.stores.classify_overrides_store import (
    ClassifyOverridesStore,
    resolve_classify_overrides_path,
)
from trellis.stores.registry import StoreRegistry

_SOURCE_EVENT_ID = "01EVT000000000000000000"


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _store(registry: StoreRegistry) -> ClassifyOverridesStore:
    path = resolve_classify_overrides_path(registry.stores_dir)
    assert path is not None
    return ClassifyOverridesStore(path)


def _apply_cmd(
    kind: str,
    key: str,
    target: str,
    *,
    source_event_id: str = _SOURCE_EVENT_ID,
    requested_by: str = "test:classify",
    metadata: dict[str, object] | None = None,
    **extra_args: object,
) -> Command:
    args: dict[str, object] = {
        "kind": kind,
        "key": key,
        "target": target,
        "source_event_id": source_event_id,
        "support": None,
        "precision": None,
        "lift": None,
    }
    args.update(extra_args)
    return Command(
        operation=Operation.CLASSIFY_RULE_APPLY,
        args=args,
        target_id=f"{kind}:{key}",
        target_type="classify_rule",
        requested_by=requested_by,
        metadata=metadata or {},
    )


def _revoke_cmd(
    kind: str,
    key: str,
    *,
    requested_by: str = "test:classify",
    metadata: dict[str, object] | None = None,
) -> Command:
    return Command(
        operation=Operation.CLASSIFY_RULE_REVOKE,
        args={"kind": kind, "key": key},
        target_id=f"{kind}:{key}",
        target_type="classify_rule",
        requested_by=requested_by,
        metadata=metadata or {},
    )


def _applied_event_count(registry: StoreRegistry) -> int:
    return len(
        registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_APPLIED, limit=100
        )
    )


def _revoked_event_count(registry: StoreRegistry) -> int:
    return len(
        registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_REVOKED, limit=100
        )
    )


# ---------------------------------------------------------------------------
# N2 — metric validation (NaN/inf/out-of-range/wrong-type), no partial write
# ---------------------------------------------------------------------------


class TestApplyMetricValidation:
    def test_nan_precision_is_refused_with_invalid_metric_and_writes_no_row(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance", precision=math.nan)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_metric"
        assert _store(registry).get("tag_keyword", "zzzkw") is None
        assert _applied_event_count(registry) == 0

    def test_infinite_lift_is_refused_with_invalid_metric_and_writes_no_row(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance", lift=math.inf)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_metric"
        assert _store(registry).get("tag_keyword", "zzzkw") is None
        assert _applied_event_count(registry) == 0

    def test_negative_infinite_lift_is_refused(self, registry: StoreRegistry) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance", lift=-math.inf)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_metric"
        assert _store(registry).get("tag_keyword", "zzzkw") is None

    def test_negative_support_is_refused_and_writes_no_row(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance", support=-1)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_metric"
        assert _store(registry).get("tag_keyword", "zzzkw") is None

    def test_bool_support_is_refused(self, registry: StoreRegistry) -> None:
        """``bool`` is an ``int`` subclass -- ``isinstance(True, int)`` is
        ``True`` -- so it needs an explicit check beyond ``isinstance``."""
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance", support=True)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_metric"
        assert _store(registry).get("tag_keyword", "zzzkw") is None

    def test_bool_precision_is_refused(self, registry: StoreRegistry) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance", precision=False)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_metric"
        assert _store(registry).get("tag_keyword", "zzzkw") is None

    def test_precision_above_one_is_refused(self, registry: StoreRegistry) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance", precision=1.5)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_metric"
        assert _store(registry).get("tag_keyword", "zzzkw") is None

    def test_negative_precision_is_refused(self, registry: StoreRegistry) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance", precision=-0.1)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_metric"
        assert _store(registry).get("tag_keyword", "zzzkw") is None

    def test_negative_lift_is_refused(self, registry: StoreRegistry) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance", lift=-0.5)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_metric"
        assert _store(registry).get("tag_keyword", "zzzkw") is None

    def test_valid_metrics_are_accepted_and_carried_onto_the_event(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd(
            "tag_keyword", "zzzkw", "finance", support=10, precision=0.9, lift=0.2
        )

        handler.handle(cmd)

        assert _store(registry).get("tag_keyword", "zzzkw") is not None
        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_APPLIED, limit=10
        )
        assert events[-1].payload["support"] == 10
        assert events[-1].payload["precision"] == 0.9
        assert events[-1].payload["lift"] == 0.2

    def test_null_metrics_are_accepted(self, registry: StoreRegistry) -> None:
        """``support``/``precision``/``lift`` are all optional -- ``None``
        must never trip the finiteness/range checks."""
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance")

        handler.handle(cmd)

        assert _store(registry).get("tag_keyword", "zzzkw") is not None


# ---------------------------------------------------------------------------
# Follow-up #3 — tag_keyword 3-character floor
# ---------------------------------------------------------------------------


class TestTagKeywordLengthFloor:
    def test_two_char_key_is_refused(self, registry: StoreRegistry) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "ml", "finance")

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "tag_keyword_too_short"
        assert _store(registry).get("tag_keyword", "ml") is None

    def test_one_char_key_is_refused(self, registry: StoreRegistry) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "x", "finance")

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "tag_keyword_too_short"

    def test_three_char_key_is_the_accepted_floor(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "abc", "finance")

        handler.handle(cmd)

        assert _store(registry).get("tag_keyword", "abc") is not None

    def test_domain_alias_kind_is_exempt_from_the_floor(
        self, registry: StoreRegistry
    ) -> None:
        """The floor is ``tag_keyword``-specific -- a short domain_alias
        key is a different kind of value (an alias, not a keyword token)
        and is not covered by this check."""
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("domain_alias", "ab", "widgets")

        handler.handle(cmd)

        assert _store(registry).get("domain_alias", "ab") is not None


# ---------------------------------------------------------------------------
# Follow-up #4 — reserved-namespace check on alias keys, not just targets
# ---------------------------------------------------------------------------


class TestReservedNamespaceOnAliasKey:
    def test_alias_key_in_reserved_namespace_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("domain_alias", "sensitivity", "privacy")

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "reserved_namespace_target"
        assert _store(registry).get("domain_alias", "sensitivity") is None

    def test_tag_keyword_key_is_not_subject_to_the_alias_key_check(
        self, registry: StoreRegistry
    ) -> None:
        """The alias-key check is specific to ``domain_alias`` -- a
        ``tag_keyword`` key is never merged as a domain name, so
        ``kind == "domain_alias"`` gates it."""
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "sensitivity", "finance")

        handler.handle(cmd)

        assert _store(registry).get("tag_keyword", "sensitivity") is not None


# ---------------------------------------------------------------------------
# N1 — Revoke validates the prospective merged alias map
# ---------------------------------------------------------------------------


class TestRevokeAliasChainExposure:
    def test_revoke_exposing_a_config_chain_is_refused_and_row_kept(
        self, tmp_path: Path
    ) -> None:
        """config.yaml has ``b-alias -> x-canonical``. The overlay applies
        ``b-alias -> y-canonical`` (shadows config's row) and then
        ``x-canonical -> q-final`` (fine while config's ``b-alias`` stays
        shadowed). Revoking ``b-alias`` un-shadows config's
        ``b-alias -> x-canonical``, which now chains with the
        still-applied ``x-canonical -> q-final``."""
        reg = StoreRegistry(
            stores_dir=tmp_path / "stores",
            classify_config={"domain_aliases": {"b-alias": "x-canonical"}},
        )
        apply_handler = ClassifyRuleApplyHandler(reg)
        apply_handler.handle(_apply_cmd("domain_alias", "b-alias", "y-canonical"))
        apply_handler.handle(_apply_cmd("domain_alias", "x-canonical", "q-final"))

        revoke_handler = ClassifyRuleRevokeHandler(reg)
        with pytest.raises(ValidationError) as exc_info:
            revoke_handler.handle(_revoke_cmd("domain_alias", "b-alias"))

        assert exc_info.value.code == "invalid_alias_rule"
        row = _store(reg).get("domain_alias", "b-alias")
        assert row is not None
        assert row.target == "y-canonical"

    def test_revoke_with_no_chain_exposure_still_succeeds(
        self, registry: StoreRegistry
    ) -> None:
        """Sanity check that the new guard does not over-refuse an
        ordinary revoke with nothing left to chain against."""
        apply_handler = ClassifyRuleApplyHandler(registry)
        apply_handler.handle(_apply_cmd("domain_alias", "zzzalias", "zzzcanonical"))

        revoke_handler = ClassifyRuleRevokeHandler(registry)
        revoke_handler.handle(_revoke_cmd("domain_alias", "zzzalias"))

        assert _store(registry).get("domain_alias", "zzzalias") is None


# ---------------------------------------------------------------------------
# Follow-up #2 — pin Revoke's config_target / revoked_by (mutants M22, M24)
# ---------------------------------------------------------------------------


class TestRevokeAuditPayloadFields:
    def test_revoked_by_is_the_commands_requested_by(
        self, registry: StoreRegistry
    ) -> None:
        apply_handler = ClassifyRuleApplyHandler(registry)
        apply_handler.handle(_apply_cmd("tag_keyword", "zzzkw", "finance"))

        revoke_handler = ClassifyRuleRevokeHandler(registry)
        revoke_handler.handle(
            _revoke_cmd("tag_keyword", "zzzkw", requested_by="cli:audit-probe")
        )

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_REVOKED, limit=10
        )
        assert events[-1].payload["revoked_by"] == "cli:audit-probe"

    def test_config_target_is_none_when_overlay_shadows_nothing(
        self, registry: StoreRegistry
    ) -> None:
        apply_handler = ClassifyRuleApplyHandler(registry)
        apply_handler.handle(_apply_cmd("domain_alias", "zzzalias", "zzzcanonical"))

        revoke_handler = ClassifyRuleRevokeHandler(registry)
        revoke_handler.handle(_revoke_cmd("domain_alias", "zzzalias"))

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_REVOKED, limit=10
        )
        assert events[-1].payload["config_target"] is None

    def test_config_target_names_the_shadowed_config_value(
        self, tmp_path: Path
    ) -> None:
        """Revoking an alias that shadowed a config.yaml row must record
        what config.yaml still says for that key -- not just ``None``,
        which would read as "nothing to fall back to"."""
        reg = StoreRegistry(
            stores_dir=tmp_path / "stores",
            classify_config={"domain_aliases": {"legacyname": "config-canonical"}},
        )
        apply_handler = ClassifyRuleApplyHandler(reg)
        apply_handler.handle(
            _apply_cmd("domain_alias", "legacyname", "overlay-canonical")
        )

        revoke_handler = ClassifyRuleRevokeHandler(reg)
        revoke_handler.handle(_revoke_cmd("domain_alias", "legacyname"))

        events = reg.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_REVOKED, limit=10
        )
        assert events[-1].payload["config_target"] == "config-canonical"

    def test_config_target_is_none_for_tag_keyword_kind(
        self, registry: StoreRegistry
    ) -> None:
        """``config_target`` only has meaning for ``domain_alias`` --
        ``tag_keyword`` never populates it."""
        apply_handler = ClassifyRuleApplyHandler(registry)
        apply_handler.handle(_apply_cmd("tag_keyword", "zzzkw", "finance"))

        revoke_handler = ClassifyRuleRevokeHandler(registry)
        revoke_handler.handle(_revoke_cmd("tag_keyword", "zzzkw"))

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_REVOKED, limit=10
        )
        assert events[-1].payload["config_target"] is None


# ---------------------------------------------------------------------------
# Follow-up #2 — pin the M9/M9b mistargeted-command guards
# ---------------------------------------------------------------------------


class TestMistargetedCommandGuard:
    def test_apply_wrong_target_type_is_refused(self, registry: StoreRegistry) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance")
        cmd = cmd.model_copy(update={"target_type": "setting"})

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "untargeted_command"
        assert _store(registry).get("tag_keyword", "zzzkw") is None

    def test_apply_wrong_target_id_is_refused(self, registry: StoreRegistry) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance")
        cmd = cmd.model_copy(update={"target_id": "tag_keyword:some-other-key"})

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "untargeted_command"
        assert _store(registry).get("tag_keyword", "zzzkw") is None

    def test_revoke_wrong_target_type_is_refused(self, registry: StoreRegistry) -> None:
        apply_handler = ClassifyRuleApplyHandler(registry)
        apply_handler.handle(_apply_cmd("tag_keyword", "zzzkw", "finance"))

        revoke_handler = ClassifyRuleRevokeHandler(registry)
        cmd = _revoke_cmd("tag_keyword", "zzzkw")
        cmd = cmd.model_copy(update={"target_type": "setting"})

        with pytest.raises(ValidationError) as exc_info:
            revoke_handler.handle(cmd)

        assert exc_info.value.code == "untargeted_command"
        # Left in place: the mistargeted command never reached the write.
        assert _store(registry).get("tag_keyword", "zzzkw") is not None

    def test_revoke_wrong_target_id_is_refused(self, registry: StoreRegistry) -> None:
        apply_handler = ClassifyRuleApplyHandler(registry)
        apply_handler.handle(_apply_cmd("tag_keyword", "zzzkw", "finance"))

        revoke_handler = ClassifyRuleRevokeHandler(registry)
        cmd = _revoke_cmd("tag_keyword", "zzzkw")
        cmd = cmd.model_copy(update={"target_id": "tag_keyword:some-other-key"})

        with pytest.raises(ValidationError) as exc_info:
            revoke_handler.handle(cmd)

        assert exc_info.value.code == "untargeted_command"
        assert _store(registry).get("tag_keyword", "zzzkw") is not None


# ---------------------------------------------------------------------------
# X1 / X2 — shared key/target normal-form validator, called before any write
# ---------------------------------------------------------------------------


class TestClassifyRuleIdentifierNormalForm:
    """A key/target that is not already stripped, control-char-free and
    under the length cap is refused outright, never silently normalised --
    ``ClassifyOverrideRow`` strips whitespace on assignment, which would
    otherwise split one logical identifier across the stored row and the
    (unstripped) audit event. Each case must leave no overlay row and no
    audit event, on both Apply and Revoke."""

    def test_apply_leading_whitespace_key_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", " an", "finance")

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_key"
        assert _store(registry).get("tag_keyword", "an") is None
        assert _store(registry).get("tag_keyword", " an") is None
        assert _applied_event_count(registry) == 0

    def test_apply_trailing_whitespace_target_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", " retention")

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_target"
        assert _store(registry).get("tag_keyword", "zzzkw") is None
        assert _applied_event_count(registry) == 0

    def test_apply_nul_key_is_refused(self, registry: StoreRegistry) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "abc\x00def", "finance")

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_key"
        assert _store(registry).get("tag_keyword", "abc\x00def") is None
        assert _applied_event_count(registry) == 0

    def test_apply_nul_target_is_refused(self, registry: StoreRegistry) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "fin\x00ance")

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_target"
        assert _store(registry).get("tag_keyword", "zzzkw") is None
        assert _applied_event_count(registry) == 0

    def test_apply_overlong_key_is_refused(self, registry: StoreRegistry) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        long_key = "a" * 10_000
        cmd = _apply_cmd("tag_keyword", long_key, "finance")

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_key"
        assert _store(registry).get("tag_keyword", long_key) is None
        assert _applied_event_count(registry) == 0

    def test_revoke_whitespace_key_is_refused_as_invalid_key_not_no_such_rule(
        self, registry: StoreRegistry
    ) -> None:
        """Revoke is not exploitable even without this check --
        ``ClassifyOverridesStore.get`` looks up the raw key, so
        ``" ann"`` would already be refused as ``no_such_rule``. The
        validator fires first anyway, so the message names the real
        problem (a malformed key) rather than a misleading "not found"."""
        apply_handler = ClassifyRuleApplyHandler(registry)
        apply_handler.handle(_apply_cmd("tag_keyword", "ann", "finance"))

        revoke_handler = ClassifyRuleRevokeHandler(registry)
        cmd = _revoke_cmd("tag_keyword", " ann")

        with pytest.raises(ValidationError) as exc_info:
            revoke_handler.handle(cmd)

        assert exc_info.value.code == "invalid_key"
        # The real row (stored under the stripped key) is untouched.
        assert _store(registry).get("tag_keyword", "ann") is not None
        assert _revoked_event_count(registry) == 0

    def test_revoke_nul_key_is_refused(self, registry: StoreRegistry) -> None:
        revoke_handler = ClassifyRuleRevokeHandler(registry)
        cmd = _revoke_cmd("tag_keyword", "abc\x00def")

        with pytest.raises(ValidationError) as exc_info:
            revoke_handler.handle(cmd)

        assert exc_info.value.code == "invalid_key"
        assert _revoked_event_count(registry) == 0

    def test_revoke_overlong_key_is_refused(self, registry: StoreRegistry) -> None:
        revoke_handler = ClassifyRuleRevokeHandler(registry)
        long_key = "b" * 10_000
        cmd = _revoke_cmd("tag_keyword", long_key)

        with pytest.raises(ValidationError) as exc_info:
            revoke_handler.handle(cmd)

        assert exc_info.value.code == "invalid_key"
        assert _revoked_event_count(registry) == 0

    def test_domain_alias_key_reserved_namespace_check_is_casefolded(
        self, registry: StoreRegistry
    ) -> None:
        """``RESERVED_NAMESPACES`` holds lowercase strings, and nothing
        else in the merged alias map compares an alias key
        case-sensitively -- so ``Sensitivity``/``SENSITIVITY`` must be
        refused exactly like the lowercase spelling, not accepted as a
        different string."""
        handler = ClassifyRuleApplyHandler(registry)

        for variant in ("Sensitivity", "SENSITIVITY"):
            cmd = _apply_cmd("domain_alias", variant, "privacy")

            with pytest.raises(ValidationError) as exc_info:
                handler.handle(cmd)

            assert exc_info.value.code == "reserved_namespace_target"
            assert _store(registry).get("domain_alias", variant) is None

    def test_domain_alias_target_is_not_casefolded(
        self, registry: StoreRegistry
    ) -> None:
        """Sanity check for the test above: a target is never folded.
        ``Sensitivity`` (mixed case) collides with the reserved namespace
        ``sensitivity`` only under case-insensitive comparison -- unlike
        the alias-*key* check above, the target check compares verbatim,
        so this is accepted and stored exactly as given, not refused as
        ``reserved_namespace_target``."""
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("domain_alias", "secretalias", "Sensitivity")

        handler.handle(cmd)

        row = _store(registry).get("domain_alias", "secretalias")
        assert row is not None
        assert row.target == "Sensitivity"

    # -- B1: source_event_id is the third field through the same validator --

    def test_apply_nul_source_event_id_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd(
            "tag_keyword", "zzzkw", "finance", source_event_id="01EVT\x00000"
        )

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_source_event_id"
        assert _store(registry).get("tag_keyword", "zzzkw") is None
        assert _applied_event_count(registry) == 0

    def test_apply_leading_whitespace_source_event_id_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance", source_event_id=" 01EVT000")

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_source_event_id"
        assert _store(registry).get("tag_keyword", "zzzkw") is None
        assert _applied_event_count(registry) == 0

    def test_apply_overlong_source_event_id_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd(
            "tag_keyword", "zzzkw", "finance", source_event_id="e" * 10_000
        )

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_source_event_id"
        assert _store(registry).get("tag_keyword", "zzzkw") is None
        assert _applied_event_count(registry) == 0

    # -- S3: pin the 200-char boundary itself, on both sides, for key and target --

    def test_apply_key_at_200_chars_is_accepted(self, registry: StoreRegistry) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        key = "k" * 200
        cmd = _apply_cmd("tag_keyword", key, "finance")

        handler.handle(cmd)

        assert _store(registry).get("tag_keyword", key) is not None

    def test_apply_key_at_201_chars_is_refused(self, registry: StoreRegistry) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        key = "k" * 201
        cmd = _apply_cmd("tag_keyword", key, "finance")

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_key"
        assert _store(registry).get("tag_keyword", key) is None

    def test_apply_target_at_200_chars_is_accepted(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        target = "t" * 200
        cmd = _apply_cmd("tag_keyword", "zzzkw", target)

        handler.handle(cmd)

        row = _store(registry).get("tag_keyword", "zzzkw")
        assert row is not None
        assert row.target == target

    def test_apply_target_at_201_chars_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        target = "t" * 201
        cmd = _apply_cmd("tag_keyword", "zzzkw", target)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_target"
        assert _store(registry).get("tag_keyword", "zzzkw") is None

    # -- S3: non-ASCII whitespace and a non-NUL Cc character are refused too --

    def test_apply_key_with_nbsp_is_refused(self, registry: StoreRegistry) -> None:
        """U+00A0 (NBSP) is whitespace under ``str.strip()`` but is not
        the ASCII space the validator could be narrowed to check for
        instead -- it must still be refused."""
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "\u00a0zzzkw", "finance")

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_key"
        assert _store(registry).get("tag_keyword", "\u00a0zzzkw") is None

    def test_apply_key_with_esc_control_char_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        """ESC (U+001B) is category ``Cc`` like NUL, but is not NUL itself
        -- a validator narrowed to check only for ``"\\x00"`` would let
        this through."""
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzz\x1bkw", "finance")

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_key"
        assert _store(registry).get("tag_keyword", "zzz\x1bkw") is None


# ---------------------------------------------------------------------------
# B1 / S2 — JSONB-safety backstop on the whole audit payload
# ---------------------------------------------------------------------------


class TestJsonbSafeAuditPayload:
    """A NUL byte or lone UTF-16 surrogate anywhere in the audit payload
    fails a Postgres ``jsonb`` column outright (``UntranslatableCharacter``
    / unencodable to UTF-8) even when the SQLite-backed test suite cannot
    see it. ``_validate_jsonb_safe_payload`` is a backstop independent of
    backend: it refuses before the write it would otherwise follow in
    either handler, so a refusal here always leaves prior state --
    row present or absent -- untouched."""

    def test_apply_metadata_key_name_lone_surrogate_is_refused_no_row_no_event(
        self, registry: StoreRegistry
    ) -> None:
        """The same backstop on Apply's side: a NUL or lone surrogate in
        ``command.metadata`` (not reachable through the key/target/
        source_event_id identifier validator) must not reach the
        overlay row or the audit event either."""
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd(
            "tag_keyword",
            "zzzkw",
            "finance",
            metadata={"key_name": "cli:\udcff"},
        )

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "unsafe_audit_payload"
        assert _store(registry).get("tag_keyword", "zzzkw") is None
        assert _applied_event_count(registry) == 0

    def test_revoke_metadata_key_name_lone_surrogate_is_refused_row_untouched(
        self, registry: StoreRegistry
    ) -> None:
        apply_handler = ClassifyRuleApplyHandler(registry)
        apply_handler.handle(_apply_cmd("tag_keyword", "zzzkw", "finance"))

        revoke_handler = ClassifyRuleRevokeHandler(registry)
        cmd = _revoke_cmd("tag_keyword", "zzzkw", metadata={"key_name": "cli:\udcff"})

        with pytest.raises(ValidationError) as exc_info:
            revoke_handler.handle(cmd)

        assert exc_info.value.code == "unsafe_audit_payload"
        assert _store(registry).get("tag_keyword", "zzzkw") is not None
        assert _revoked_event_count(registry) == 0

    def test_revoke_row_with_nul_in_source_event_id_is_refused_row_untouched(
        self, registry: StoreRegistry
    ) -> None:
        """A row carrying a NUL in ``source_event_id`` -- as Apply's own
        validator now refuses on the way in, this can only be legacy
        data from an earlier build or a hand-edited overlay file -- must
        still be refused on Revoke rather than removed with a broken
        audit event."""
        store = _store(registry)
        store.put(
            ClassifyOverrideRow(
                kind="tag_keyword",
                key="legacykw",
                target="finance",
                applied_at=datetime.now(UTC),
                applied_by="legacy-writer",
                source_event_id="01EVT\x00legacy",
            )
        )

        revoke_handler = ClassifyRuleRevokeHandler(registry)
        cmd = _revoke_cmd("tag_keyword", "legacykw")

        with pytest.raises(ValidationError) as exc_info:
            revoke_handler.handle(cmd)

        assert exc_info.value.code == "unsafe_audit_payload"
        assert _store(registry).get("tag_keyword", "legacykw") is not None
        assert _revoked_event_count(registry) == 0


# ---------------------------------------------------------------------------
# N2 mutant pins — H2 (isfinite clause message), H5/H6 (serialisation probe)
# ---------------------------------------------------------------------------


class TestMetricValidationMutantPins:
    def test_nan_precision_message_names_finite_number(
        self, registry: StoreRegistry
    ) -> None:
        """Pins H2: without the ``math.isfinite`` clause, a NaN still
        passes every bounds comparison (every comparison with NaN is
        false) and reaches the serialisation probe below, which also
        refuses with ``invalid_metric`` -- so the *code* alone cannot
        distinguish the two layers. The message can: only the
        ``isfinite`` clause says "must be a finite number"."""
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance", precision=math.nan)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_metric"
        assert "must be a finite number" in exc_info.value.message

    def test_huge_support_fails_serialization_not_the_range_check(
        self, registry: StoreRegistry
    ) -> None:
        """Pins H5/H6: ``support=10**5000`` is a non-negative ``int``, so
        ``_validate_classify_rule_metrics`` accepts it outright -- the
        only thing that refuses it is the ``json.dumps(..., allow_nan=False)``
        probe run before ``store.put``, because Python's own integer string
        conversion limit (4,300 digits by default) makes a 5,001-digit int
        unserialisable. Moving or deleting that probe must be caught here,
        since no shipped surface can otherwise reach it (REST/CLI integers
        this large fail earlier, at JSON parse / Click's own int parsing)."""
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd("tag_keyword", "zzzkw", "finance", support=10**5000)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)

        assert exc_info.value.code == "invalid_metric"
        assert _store(registry).get("tag_keyword", "zzzkw") is None
        assert _applied_event_count(registry) == 0


# ---------------------------------------------------------------------------
# D16 — structured caller identity (Command.metadata) on the applied_by field
# ---------------------------------------------------------------------------


class TestAppliedByUsesStructuredIdentity:
    def test_applied_by_prefers_metadata_key_id_over_requested_by(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd(
            "tag_keyword",
            "zzzkw",
            "finance",
            requested_by="api:classify.apply-rule",
            metadata={"key_id": "key_abc123", "key_name": "ci-bot"},
        )

        handler.handle(cmd)

        row = _store(registry).get("tag_keyword", "zzzkw")
        assert row is not None
        assert row.applied_by == "key_abc123"
        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_APPLIED, limit=10
        )
        assert events[-1].payload["applied_by"] == "key_abc123"

    def test_applied_by_falls_back_to_requested_by_with_no_metadata(
        self, registry: StoreRegistry
    ) -> None:
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd(
            "tag_keyword", "zzzkw", "finance", requested_by="api:classify.apply-rule"
        )

        handler.handle(cmd)

        row = _store(registry).get("tag_keyword", "zzzkw")
        assert row is not None
        assert row.applied_by == "api:classify.apply-rule"

    def test_applied_by_falls_back_to_metadata_key_name_without_key_id(
        self, registry: StoreRegistry
    ) -> None:
        """``key_id`` is the preferred identity, but a caller that only
        has a name (no stable id -- e.g. a CLI OS-user stamp) still wins
        over the constant surface label in ``requested_by``."""
        handler = ClassifyRuleApplyHandler(registry)
        cmd = _apply_cmd(
            "tag_keyword",
            "zzzkw",
            "finance",
            requested_by="cli:classify-apply-rule",
            metadata={"key_name": "cli:alice"},
        )

        handler.handle(cmd)

        row = _store(registry).get("tag_keyword", "zzzkw")
        assert row is not None
        assert row.applied_by == "cli:alice"


# ---------------------------------------------------------------------------
# S3 identity pins — Revoke's payload carries key_id/key_name too
# ---------------------------------------------------------------------------


class TestRevokePayloadCarriesStructuredIdentity:
    def test_revoke_payload_carries_metadata_key_id(
        self, registry: StoreRegistry
    ) -> None:
        apply_handler = ClassifyRuleApplyHandler(registry)
        apply_handler.handle(_apply_cmd("tag_keyword", "zzzkw", "finance"))

        revoke_handler = ClassifyRuleRevokeHandler(registry)
        cmd = _revoke_cmd(
            "tag_keyword",
            "zzzkw",
            metadata={"key_id": "key_abc123", "key_name": "ci-bot"},
        )

        revoke_handler.handle(cmd)

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_REVOKED, limit=10
        )
        assert events[-1].payload["key_id"] == "key_abc123"

    def test_revoke_payload_carries_metadata_key_name(
        self, registry: StoreRegistry
    ) -> None:
        apply_handler = ClassifyRuleApplyHandler(registry)
        apply_handler.handle(_apply_cmd("tag_keyword", "zzzkw", "finance"))

        revoke_handler = ClassifyRuleRevokeHandler(registry)
        cmd = _revoke_cmd(
            "tag_keyword",
            "zzzkw",
            metadata={"key_id": "key_abc123", "key_name": "ci-bot"},
        )

        revoke_handler.handle(cmd)

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_REVOKED, limit=10
        )
        assert events[-1].payload["key_name"] == "ci-bot"

    def test_revoke_payload_key_id_is_none_without_metadata(
        self, registry: StoreRegistry
    ) -> None:
        """Sanity check for the two tests above: with no metadata at
        all, Revoke's payload still carries the ``key_id``/``key_name``
        fields (so a reader can rely on their presence), just as
        ``None``, rather than omitting them."""
        apply_handler = ClassifyRuleApplyHandler(registry)
        apply_handler.handle(_apply_cmd("tag_keyword", "zzzkw", "finance"))

        revoke_handler = ClassifyRuleRevokeHandler(registry)
        revoke_handler.handle(_revoke_cmd("tag_keyword", "zzzkw"))

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_REVOKED, limit=10
        )
        assert events[-1].payload["key_id"] is None
        assert events[-1].payload["key_name"] is None

    def test_revoke_by_is_requested_by_not_structured_identity(
        self, registry: StoreRegistry
    ) -> None:
        """Asymmetric with Apply's ``applied_by`` on purpose: ``revoked_by``
        is always ``command.requested_by`` verbatim, even when metadata
        carries a ``key_id``/``key_name`` that would outrank it on the
        Apply side."""
        apply_handler = ClassifyRuleApplyHandler(registry)
        apply_handler.handle(_apply_cmd("tag_keyword", "zzzkw", "finance"))

        revoke_handler = ClassifyRuleRevokeHandler(registry)
        cmd = _revoke_cmd(
            "tag_keyword",
            "zzzkw",
            requested_by="api:classify.revoke-rule",
            metadata={"key_id": "key_abc123", "key_name": "ci-bot"},
        )

        revoke_handler.handle(cmd)

        events = registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_REVOKED, limit=10
        )
        assert events[-1].payload["revoked_by"] == "api:classify.revoke-rule"
