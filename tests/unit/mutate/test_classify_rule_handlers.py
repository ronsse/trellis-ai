"""Tests for ``ClassifyRuleApplyHandler`` / ``ClassifyRuleRevokeHandler``.

The REST (``tests/unit/api/test_classify_rules_routes.py``) and CLI
(``tests/unit/cli/test_classify_rules_cli.py``) test files prove the same
handler validation rules through their own surfaces, each layered with a
surface-specific guard in front (the DTO's ``allow_inf_nan=False``, the
CLI's own finiteness check) that stops a bad value before it ever reaches
here. This file calls the handlers directly, the same pattern
``test_settings_update_handler.py`` uses, so it is the one place a metric
genuinely reaches ``_validate_classify_rule_metrics`` -- the check every
caller (REST, CLI, MCP, a future worker) passes through, not just the two
that currently guard themselves.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from trellis.errors import ValidationError
from trellis.mutate.commands import Command, Operation
from trellis.mutate.handlers import ClassifyRuleApplyHandler, ClassifyRuleRevokeHandler
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
    )


def _revoke_cmd(kind: str, key: str, *, requested_by: str = "test:classify") -> Command:
    return Command(
        operation=Operation.CLASSIFY_RULE_REVOKE,
        args={"kind": kind, "key": key},
        target_id=f"{kind}:{key}",
        target_type="classify_rule",
        requested_by=requested_by,
    )


def _applied_event_count(registry: StoreRegistry) -> int:
    return len(
        registry.operational.event_log.get_events(
            event_type=EventType.CLASSIFY_RULE_APPLIED, limit=100
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
