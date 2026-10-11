"""Tests for ``SettingsUpdateHandler`` (governed settings write, PR 3).

``Operation.SETTINGS_UPDATE`` is the first thing this registry's
``type``/``minimum``/``maximum``/``enum`` fields actually gate (see
``trellis.core.settings_registry``'s module docstring). ``args["value"]
is None`` means "remove the override" — there is no separate
``SETTINGS_RESET`` operation; see the handler's own docstring for why.

Every refusal (unknown name, wrong type, out of range, degraded/stale
store) must come back as a ``ValidationError`` so the executor's normal
REJECTED path records the real reason — never a silent clamp.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from trellis.core.settings_registry import SETTINGS_REGISTRY
from trellis.errors import ValidationError
from trellis.mutate import build_settings_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import (
    SettingsUpdateHandler,
    create_curate_handlers,
    create_settings_handlers,
)
from trellis.mutate.policy_gate import DefaultPolicyGate
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.settings_store import SettingsStore, resolve_settings_path


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _store(registry: StoreRegistry) -> SettingsStore:
    path = resolve_settings_path(registry.stores_dir)
    assert path is not None
    return SettingsStore(path)


def _cmd(name: str, value: object, **kwargs: object) -> Command:
    """A correctly-targeted settings.update Command, the shape every real
    surface (admin route, ``trellis admin settings``) sends -- see the
    defence-in-depth check in ``SettingsUpdateHandler.handle``.
    """
    return Command(
        operation=Operation.SETTINGS_UPDATE,
        args={"name": name, "value": value},
        target_id=name,
        target_type="setting",
        **kwargs,  # type: ignore[arg-type]
    )


class TestSetsAnOverride:
    def test_valid_float_in_range_is_stored(self, registry: StoreRegistry) -> None:
        handler = SettingsUpdateHandler(registry)
        cmd = _cmd("pack_holdout_rate", 0.2)

        name, message = handler.handle(cmd)

        assert name == "pack_holdout_rate"
        assert "0.2" in message
        assert _store(registry).get("pack_holdout_rate").value == 0.2

    def test_bool_accepts_true_false(self, registry: StoreRegistry) -> None:
        handler = SettingsUpdateHandler(registry)
        cmd = _cmd("memory_extraction", True)

        handler.handle(cmd)

        assert _store(registry).get("memory_extraction").value is True

    def test_string_setting_with_no_enum_accepts_free_text(
        self, registry: StoreRegistry
    ) -> None:
        handler = SettingsUpdateHandler(registry)
        cmd = _cmd("reconcile_model", "qwen2.5:14b")

        handler.handle(cmd)

        assert _store(registry).get("reconcile_model").value == "qwen2.5:14b"

    def test_emits_settings_updated_event_with_old_and_new_value(
        self, registry: StoreRegistry
    ) -> None:
        handler = SettingsUpdateHandler(registry)
        cmd = _cmd("pack_holdout_rate", 0.3, requested_by="cli:settings-set")

        handler.handle(cmd)
        cmd2 = _cmd("pack_holdout_rate", 0.5, requested_by="cli:settings-set")
        handler.handle(cmd2)

        events = registry.operational.event_log.get_events(
            event_type=EventType.SETTINGS_UPDATED,
            order="asc",
            limit=100,
        )
        assert len(events) == 2
        second = events[1]
        assert second.payload["name"] == "pack_holdout_rate"
        assert second.payload["old_value"] == 0.3
        assert second.payload["new_value"] == 0.5
        assert second.payload["changed_by"] == "cli:settings-set"
        # A constant "settings" satisfies a bare "key present" check,
        # so this asserts the real resolved value, not just presence.
        assert second.payload["overridden_by_at_write"] == "settings"
        assert second.payload["settings_live"] is True
        assert second.payload["restart_required"] is False

    def test_overridden_by_at_write_is_env_when_the_env_var_is_pinned(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of M5: a constant "settings" would also survive
        unless some test asserts the *env-pinned* value too.
        """
        monkeypatch.setenv("TRELLIS_PACK_HOLDOUT_RATE", "0.9")
        handler = SettingsUpdateHandler(registry)
        cmd = _cmd("pack_holdout_rate", 0.3, requested_by="cli:settings-set")

        handler.handle(cmd)

        events = registry.operational.event_log.get_events(
            event_type=EventType.SETTINGS_UPDATED,
            order="asc",
            limit=100,
        )
        assert events[-1].payload["overridden_by_at_write"] == "env"

    def test_requested_by_claim_is_carried_on_the_event_when_present(
        self, registry: StoreRegistry
    ) -> None:
        """F4: the REST route passes the client's claimed identity
        separately from ``changed_by`` (which comes from the
        authenticated principal) -- this is the handler-level contract
        that makes that split possible.
        """
        handler = SettingsUpdateHandler(registry)
        cmd = Command(
            operation=Operation.SETTINGS_UPDATE,
            args={
                "name": "pack_holdout_rate",
                "value": 0.2,
                "requested_by_claim": "api:settings",
            },
            target_id="pack_holdout_rate",
            target_type="setting",
            requested_by="api:settings:key_abc123",
        )

        handler.handle(cmd)

        events = registry.operational.event_log.get_events(
            event_type=EventType.SETTINGS_UPDATED,
            order="asc",
            limit=100,
        )
        payload = events[-1].payload
        assert payload["changed_by"] == "api:settings:key_abc123"
        assert payload["requested_by_claim"] == "api:settings"


class TestRefusesRatherThanClamps:
    def test_unknown_name_is_rejected(self, registry: StoreRegistry) -> None:
        handler = SettingsUpdateHandler(registry)
        cmd = _cmd("not_a_real_setting", 1)

        with pytest.raises(ValidationError, match="not_a_real_setting"):
            handler.handle(cmd)

    def test_wrong_type_is_rejected_not_coerced_to_default(
        self, registry: StoreRegistry
    ) -> None:
        handler = SettingsUpdateHandler(registry)
        cmd = _cmd("memory_extraction", "not-a-bool")

        with pytest.raises(ValidationError):
            handler.handle(cmd)
        assert _store(registry).get("memory_extraction") is None

    def test_out_of_range_float_is_rejected(self, registry: StoreRegistry) -> None:
        handler = SettingsUpdateHandler(registry)
        cmd = _cmd("pack_holdout_rate", 1.5)

        with pytest.raises(ValidationError, match=re.escape("1.5")):
            handler.handle(cmd)

    def test_negative_int_below_minimum_is_rejected(
        self, registry: StoreRegistry
    ) -> None:
        handler = SettingsUpdateHandler(registry)
        cmd = _cmd("minhash_seed_max_docs", -1)

        with pytest.raises(ValidationError):
            handler.handle(cmd)

    def test_degraded_store_refuses_the_write(
        self, registry: StoreRegistry, tmp_path: Path
    ) -> None:
        path = resolve_settings_path(registry.stores_dir)
        assert path is not None
        path.write_text("{not valid json", encoding="utf-8")

        handler = SettingsUpdateHandler(registry)
        cmd = _cmd("pack_holdout_rate", 0.1)

        with pytest.raises(ValidationError, match="degraded"):
            handler.handle(cmd)

    def test_untargeted_command_is_refused(self, registry: StoreRegistry) -> None:
        """Defence in depth: even a well-formed args dict is refused when
        the Command itself is not targeted at the setting it names -- the
        shape a generic, untargeted batch/MCP command would have if
        ``Operation.SETTINGS_UPDATE`` ever reached this handler at all
        (it cannot, since it is never registered on the generic executor
        those surfaces build -- this is the second layer).
        """
        handler = SettingsUpdateHandler(registry)
        cmd = Command(
            operation=Operation.SETTINGS_UPDATE,
            args={"name": "pack_holdout_rate", "value": 0.9},
        )

        with pytest.raises(ValidationError, match=r"untargeted_command|target_type"):
            handler.handle(cmd)
        assert _store(registry).get("pack_holdout_rate") is None

    def test_mismatched_target_id_is_refused(self, registry: StoreRegistry) -> None:
        """A Command targeted at a *different* setting than the one named
        in ``args["name"]`` is refused -- it must not silently redirect.
        """
        handler = SettingsUpdateHandler(registry)
        cmd = Command(
            operation=Operation.SETTINGS_UPDATE,
            args={"name": "pack_holdout_rate", "value": 0.9},
            target_id="memory_extraction",
            target_type="setting",
        )

        with pytest.raises(ValidationError):
            handler.handle(cmd)
        assert _store(registry).get("pack_holdout_rate") is None

    def test_overflowing_float_is_a_governed_rejection_not_a_panic(
        self, registry: StoreRegistry
    ) -> None:
        """``10**400`` overflows plain ``float()`` with ``OverflowError``,
        a class ``validate_value``'s
        ``except ValueError`` would not catch on its own --
        ``parse_finite_float`` normalizes it first. Asserting the specific
        ``code`` (not just "some ValidationError") is what tells apart a
        governed ``invalid_value`` rejection from an uncaught exception
        that would instead surface as the executor's generic ``FAILED``.
        """
        handler = SettingsUpdateHandler(registry)
        cmd = _cmd("pack_holdout_rate", 10**400)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)
        assert exc_info.value.code == "invalid_value"
        assert _store(registry).get("pack_holdout_rate") is None


class TestNullValueResets:
    def test_null_value_removes_an_existing_override(
        self, registry: StoreRegistry
    ) -> None:
        handler = SettingsUpdateHandler(registry)
        handler.handle(_cmd("pack_holdout_rate", 0.4))
        assert _store(registry).get("pack_holdout_rate") is not None

        handler.handle(_cmd("pack_holdout_rate", None))

        assert _store(registry).get("pack_holdout_rate") is None

    def test_null_value_on_an_already_unset_name_is_not_an_error(
        self, registry: StoreRegistry
    ) -> None:
        handler = SettingsUpdateHandler(registry)
        name, message = handler.handle(_cmd("pack_holdout_rate", None))
        assert name == "pack_holdout_rate"
        assert message


class TestWiredIntoCurateHandlers:
    def test_create_curate_handlers_does_not_register_settings_update(
        self, registry: StoreRegistry
    ) -> None:
        """The generic curate handler set backs REST's
        ``POST /commands/batch`` and MCP's ``execute_mutation`` both, so
        ``Operation.SETTINGS_UPDATE`` must be absent from it -- otherwise
        either generic surface could write a setting with no admin scope
        and no ``target_type="setting"`` requirement in front of it.
        """
        handlers = create_curate_handlers(registry)
        assert Operation.SETTINGS_UPDATE not in handlers

    def test_create_settings_handlers_registers_settings_update(
        self, registry: StoreRegistry
    ) -> None:
        """The narrow counterpart: only the admin route and CLI build
        this handler set (via :func:`build_settings_executor`), so this
        is where ``Operation.SETTINGS_UPDATE`` actually lives.
        """
        handlers = create_settings_handlers(registry)
        assert Operation.SETTINGS_UPDATE in handlers
        assert isinstance(handlers[Operation.SETTINGS_UPDATE], SettingsUpdateHandler)


class TestStage2GateIsInvoked:
    """A policy denying ``settings.update`` must block before the handler
    ever touches the file — the same "Stage 2 always runs" contract every
    other curate operation gets (CLAUDE.md, ``test_policy_wiring.py``).
    Built through :func:`build_settings_executor`, the narrow factory the
    admin route and CLI call (``Operation.SETTINGS_UPDATE`` is not
    reachable through :func:`build_curate_executor` at all), rather than a
    bespoke executor, so this proves the production wiring and not just
    the handler in isolation.
    """

    def test_denied_policy_blocks_the_write(self, registry: StoreRegistry) -> None:
        policy = Policy(
            policy_type=PolicyType.MUTATION,
            scope=PolicyScope(level="global", value=None),
            rules=[PolicyRule(operation="settings.update", action="deny")],
            enforcement=Enforcement.ENFORCE,
        )
        executor = MutationExecutor(
            event_log=registry.operational.event_log,
            handlers=create_settings_handlers(registry),
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )
        cmd = _cmd("pack_holdout_rate", 0.9)

        result = executor.execute(cmd)

        assert result.status == CommandStatus.REJECTED
        assert _store(registry).get("pack_holdout_rate") is None

    def test_build_settings_executor_gates_settings_update_too(
        self, registry: StoreRegistry
    ) -> None:
        """Same assertion, through the real factory the admin route and
        CLI call, reading a policy file off disk the way
        ``trellis policy add`` actually writes one — not a
        ``DefaultPolicyGate`` built by hand.
        """
        policy = Policy(
            policy_type=PolicyType.MUTATION,
            scope=PolicyScope(level="global", value=None),
            rules=[PolicyRule(operation="*", action="deny")],
            enforcement=Enforcement.ENFORCE,
        )
        policy_path = registry.stores_dir / "policies.json"  # type: ignore[union-attr]
        policy_path.write_text(
            json.dumps({"policies": [policy.model_dump(mode="json")]}),
            encoding="utf-8",
        )

        executor = build_settings_executor(registry)
        cmd = _cmd("pack_holdout_rate", 0.9)

        result = executor.execute(cmd)

        assert result.status == CommandStatus.REJECTED
        assert _store(registry).get("pack_holdout_rate") is None

    def test_entity_type_setting_deny_policy_refuses_the_write(
        self, registry: StoreRegistry
    ) -> None:
        """Required item 1's third assertion: an ``entity_type=setting``
        scoped deny policy (rather than an operation-name or wildcard
        rule) refuses the write too -- the policy scope that
        ``trellis policy add --entity-type setting`` would produce.
        """
        policy = Policy(
            policy_type=PolicyType.MUTATION,
            scope=PolicyScope(level="entity_type", value="setting"),
            rules=[PolicyRule(operation="settings.update", action="deny")],
            enforcement=Enforcement.ENFORCE,
        )
        executor = MutationExecutor(
            event_log=registry.operational.event_log,
            handlers=create_settings_handlers(registry),
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )
        cmd = _cmd("pack_holdout_rate", 0.9)

        result = executor.execute(cmd)

        assert result.status == CommandStatus.REJECTED
        assert _store(registry).get("pack_holdout_rate") is None


class TestEveryRegistryEntryIsReachable:
    """A spot check, not a sweep: every declared type is exercised by at
    least one real registry entry above. This confirms the catalog itself
    has not drifted to a type ``validate_value`` cannot handle.
    """

    def test_all_declared_types_are_present_in_the_real_registry(self) -> None:
        types = {spec.type for spec in SETTINGS_REGISTRY.values()}
        assert types == {"bool", "int", "float", "str"}
