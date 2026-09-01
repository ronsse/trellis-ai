"""Tests for Stage 2 wiring — policy source, and the transparency guarantee.

The single most important property here is
:class:`TestDefaultPostureIsTransparent`: wiring a gate must not change
behaviour for any deployment that has not declared a policy. Those tests
are written to fail loudly if the empty gate ever stops being a no-op.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from tests.policy_shapes import DEGENERATE_POLICY_FILES, DEGENERATE_POLICY_IDS
from tests.structlog_isolation import IsolatedCliRunner
from trellis.errors import ConfigError
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.policy_gate import DefaultPolicyGate
from trellis.mutate.policy_source import (
    POLICY_FILENAME,
    POLICY_GATE_SURFACE,
    build_policy_gate,
    load_policies,
    resolve_policy_path,
)
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.base.event_log import EventType


class _RecordingEventLog:
    """Minimal EventLog capturing emitted events for comparison."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(
        self,
        event_type: EventType,
        actor: str,
        *,
        entity_id: str | None = None,
        entity_type: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            {
                "event_type": event_type,
                "actor": actor,
                "entity_id": entity_id,
                "entity_type": entity_type,
                "payload": dict(payload or {}),
            }
        )

    def has_idempotency_key(self, key: str) -> bool:
        return False


class _EchoHandler:
    """Handler that succeeds and reports a stable id."""

    def handle(self, command: Command) -> tuple[str | None, str]:
        return "created-1", "ok"


def _cmd(
    op: Operation = Operation.ENTITY_CREATE,
    *,
    target_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Command:
    return Command(
        operation=op,
        args={"entity_type": "service", "name": "auth"},
        target_type=target_type,
        metadata=metadata or {},
    )


def _policy(
    *,
    level: str = "global",
    value: str | None = None,
    rules: list[PolicyRule] | None = None,
    enforcement: Enforcement = Enforcement.ENFORCE,
) -> Policy:
    return Policy(
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level=level, value=value),  # type: ignore[arg-type]
        rules=rules or [],
        enforcement=enforcement,
    )


def _write_policy_file(stores_dir: Path, policies: list[Policy]) -> Path:
    stores_dir.mkdir(parents=True, exist_ok=True)
    path = stores_dir / POLICY_FILENAME
    path.write_text(
        json.dumps({"policies": [p.model_dump(mode="json") for p in policies]}),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# The safety property
# ---------------------------------------------------------------------------


class TestDefaultPostureIsTransparent:
    """An empty gate must behave exactly like no gate at all.

    If any of these fail, wiring the gate has changed production
    behaviour for deployments that never asked for governance — the one
    outcome this change must not have.
    """

    @pytest.mark.parametrize(
        ("operation", "args"),
        [
            (Operation.ENTITY_CREATE, {"entity_type": "service", "name": "auth"}),
            (Operation.TRACE_INGEST, {"trace": {}}),
            (
                Operation.PRECEDENT_PROMOTE,
                {"title": "t", "description": "d"},
            ),
            (
                Operation.EVIDENCE_ATTACH,
                {
                    "evidence_id": "e1",
                    "target_id": "n1",
                    "target_type": "entity",
                },
            ),
        ],
    )
    def test_result_identical_with_and_without_gate(
        self, operation: Operation, args: dict[str, Any]
    ) -> None:
        """Same status, message, warnings — gate or no gate.

        Args are deliberately *valid* so the command survives Stage 1 and
        actually traverses Stage 2. An invalid command short-circuits at
        validation and would pass this test without ever reaching the gate,
        which is exactly the kind of test that looks like coverage and is not.
        """
        command = Command(operation=operation, args=args)

        ungated = MutationExecutor(
            event_log=_RecordingEventLog(),
            handlers={operation: _EchoHandler()},
        )
        gated = MutationExecutor(
            event_log=_RecordingEventLog(),
            handlers={operation: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[]),
        )

        a = ungated.execute(command)
        b = gated.execute(command)

        # Reaching SUCCESS proves Stage 2 was traversed, not skipped.
        assert a.status == b.status == CommandStatus.SUCCESS
        assert a.message == b.message
        assert a.warnings == b.warnings == []
        assert a.created_id == b.created_id

    def test_emitted_events_identical_with_and_without_gate(self) -> None:
        """The audit payload must not gain a key just because a gate exists.

        ``policy_warnings`` is added only when a policy actually fired,
        so a policy-free deployment's events stay byte-identical to the
        pre-wiring world.
        """
        ungated_log = _RecordingEventLog()
        gated_log = _RecordingEventLog()

        ungated = MutationExecutor(
            event_log=ungated_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
        )
        gated = MutationExecutor(
            event_log=gated_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[]),
        )

        command = _cmd()
        ungated.execute(command)
        gated.execute(command)

        assert len(ungated_log.events) == len(gated_log.events) == 1
        # command_id is shared (same Command object), so payloads must match
        # exactly — including the absence of ``policy_warnings``.
        assert ungated_log.events[0] == gated_log.events[0]
        assert "policy_warnings" not in gated_log.events[0]["payload"]

    def test_rejection_still_happens_at_stage_1_with_empty_gate(self) -> None:
        """An empty gate must not mask validation failures."""
        gated = MutationExecutor(
            event_log=_RecordingEventLog(),
            handlers={},
            policy_gate=DefaultPolicyGate(policies=[]),
        )
        # entity.create requires args the registry validates.
        result = gated.execute(Command(operation=Operation.ENTITY_CREATE, args={}))
        assert result.status == CommandStatus.FAILED

    def test_build_curate_executor_wires_a_gate(self, tmp_path: Path) -> None:
        """The wiring itself: the factory must attach a gate, not None."""
        from trellis.stores.registry import StoreRegistry

        registry = StoreRegistry(
            config={"event_log": {"backend": "null"}},
            stores_dir=tmp_path / "stores",
        )
        executor = build_curate_executor(registry)
        assert executor._policy_gate is not None
        assert isinstance(executor._policy_gate, DefaultPolicyGate)

    def test_build_curate_executor_gate_is_empty_by_default(
        self, tmp_path: Path
    ) -> None:
        """No policy file means no policies. Trellis ships none."""
        from trellis.stores.registry import StoreRegistry

        registry = StoreRegistry(
            config={"event_log": {"backend": "null"}},
            stores_dir=tmp_path / "stores",
        )
        gate = build_curate_executor(registry)._policy_gate
        assert isinstance(gate, DefaultPolicyGate)
        assert gate._policies == []
        # And it allows.
        allowed, message, warnings = gate.check(_cmd())
        assert (allowed, message, warnings) == (True, "", [])


# ---------------------------------------------------------------------------
# Policy source: resolution + loading
# ---------------------------------------------------------------------------


class TestResolvePolicyPath:
    def test_none_stores_dir_returns_none(self) -> None:
        assert resolve_policy_path(None) is None

    def test_canonical_path_under_stores_dir(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        assert resolve_policy_path(stores_dir) == stores_dir / POLICY_FILENAME

    def test_legacy_path_is_honoured_when_canonical_absent(
        self, tmp_path: Path
    ) -> None:
        """A file written by the pre-unification CLI must not be ignored."""
        data_dir = tmp_path
        stores_dir = data_dir / "stores"
        stores_dir.mkdir(parents=True)
        legacy = data_dir / POLICY_FILENAME
        legacy.write_text('{"policies": []}', encoding="utf-8")

        assert resolve_policy_path(stores_dir) == legacy

    def test_canonical_wins_when_both_exist(self, tmp_path: Path) -> None:
        data_dir = tmp_path
        stores_dir = data_dir / "stores"
        stores_dir.mkdir(parents=True)
        (data_dir / POLICY_FILENAME).write_text('{"policies": []}', encoding="utf-8")
        canonical = stores_dir / POLICY_FILENAME
        canonical.write_text('{"policies": []}', encoding="utf-8")

        assert resolve_policy_path(stores_dir) == canonical


class TestLoadPolicies:
    def test_no_file_is_empty_not_an_error(self, tmp_path: Path) -> None:
        assert load_policies(tmp_path / "stores") == []

    def test_none_stores_dir_is_empty(self) -> None:
        assert load_policies(None) == []

    def test_round_trips_a_written_policy(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        policy = _policy(rules=[PolicyRule(operation="entity.create", action="deny")])
        _write_policy_file(stores_dir, [policy])

        loaded = load_policies(stores_dir)
        assert len(loaded) == 1
        assert loaded[0].policy_id == policy.policy_id
        assert loaded[0].rules[0].action == "deny"

    def test_round_trips_through_the_crud_store(self, tmp_path: Path) -> None:
        """What ``trellis policy add`` writes is what the gate loads.

        This is the property the shared resolver exists to guarantee —
        it would have failed before unification, because the CLI wrote
        one path and enforcement would have read another.
        """
        from trellis.stores.policy_store import PolicyStore

        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        policy = _policy(rules=[PolicyRule(operation="entity.create", action="deny")])

        store = PolicyStore(resolve_policy_path(stores_dir))
        store.add(policy)

        loaded = load_policies(stores_dir)
        assert [p.policy_id for p in loaded] == [policy.policy_id]

    def test_malformed_json_raises_rather_than_degrading(self, tmp_path: Path) -> None:
        """Fail closed: a corrupt policy file must not silently mean 'allow all'."""
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        (stores_dir / POLICY_FILENAME).write_text("{not json", encoding="utf-8")

        with pytest.raises(ConfigError) as exc_info:
            load_policies(stores_dir)
        assert POLICY_FILENAME in str(exc_info.value.setting)

    def test_wrong_top_level_shape_raises(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        (stores_dir / POLICY_FILENAME).write_text('["a", "b"]', encoding="utf-8")

        with pytest.raises(ConfigError):
            load_policies(stores_dir)

    def test_invalid_policy_entry_raises_and_names_the_index(
        self, tmp_path: Path
    ) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        good = _policy(rules=[PolicyRule(operation="*", action="deny")])
        (stores_dir / POLICY_FILENAME).write_text(
            json.dumps(
                {
                    "policies": [
                        good.model_dump(mode="json"),
                        {"policy_type": "not_a_real_type"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ConfigError, match="index 1"):
            load_policies(stores_dir)

    def test_empty_policies_list_is_not_an_error(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        (stores_dir / POLICY_FILENAME).write_text('{"policies": []}', encoding="utf-8")
        assert load_policies(stores_dir) == []


class TestBuildPolicyGate:
    def test_gate_carries_declared_policies(self, tmp_path: Path) -> None:
        from trellis.stores.registry import StoreRegistry

        stores_dir = tmp_path / "stores"
        policy = _policy(rules=[PolicyRule(operation="entity.create", action="deny")])
        _write_policy_file(stores_dir, [policy])

        registry = StoreRegistry(config={}, stores_dir=stores_dir)
        gate = build_policy_gate(registry)

        allowed, message, _ = gate.check(_cmd())
        assert allowed is False
        assert "Denied by policy" in message

    def test_gate_is_empty_without_a_registry_stores_dir(self) -> None:
        from trellis.stores.registry import StoreRegistry

        gate = build_policy_gate(StoreRegistry(config={}))
        assert gate.check(_cmd()) == (True, "", [])


class TestSurfacesAgreeOnOneFile:
    """The CLI, the REST route, and enforcement must read one file.

    Before unification ``trellis policy`` wrote ``<data_dir>/policies.json``
    while the REST route read ``<data_dir>/stores/policies.json``. Nothing
    read either, so the divergence was invisible. Now that Stage 2 is
    load-bearing, a surface writing to the wrong path would mean an
    operator declaring a policy that never fires — silently. These tests
    fail if any surface picks a path locally again.
    """

    def test_cli_writes_the_file_enforcement_reads(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cli_runner: IsolatedCliRunner,
    ) -> None:
        # ``cli_runner``, not a bare ``CliRunner``: a Trellis CLI invocation
        # pins structlog's global logger factory to a stream Click closes on
        # return, and this test logs afterwards (the deny path warns). See
        # tests/structlog_isolation.py.
        from trellis_cli.main import app

        data_dir = tmp_path / "data"
        (data_dir / "stores").mkdir(parents=True)
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))

        result = cli_runner.invoke(
            app,
            [
                "policy",
                "add",
                "--operation",
                "entity.create",
                "--action",
                "deny",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0, result.stdout
        policy_id = json.loads(result.stdout.strip())["policy_id"]

        # The gate — reading through its own resolver — must see it.
        gate = DefaultPolicyGate(policies=load_policies(data_dir / "stores"))
        assert [p.policy_id for p in gate._policies] == [policy_id]
        assert gate.check(_cmd())[0] is False

    def test_cli_and_api_resolve_the_same_path(self, tmp_path: Path) -> None:
        """Both surfaces derive their path from ``<data_dir>/stores``."""
        from trellis.stores.registry import StoreRegistry

        data_dir = tmp_path / "data"
        stores_dir = data_dir / "stores"
        stores_dir.mkdir(parents=True)

        # CLI derives stores_dir from data_dir; the API takes it from the
        # registry. Same input, same file.
        registry = StoreRegistry(config={}, stores_dir=stores_dir)
        assert resolve_policy_path(data_dir / "stores") == resolve_policy_path(
            registry.stores_dir
        )


# ---------------------------------------------------------------------------
# End-to-end: a denied command is REJECTED *and* observable
# ---------------------------------------------------------------------------


class TestDeniedCommandIsLoud:
    def test_denied_command_rejects_and_emits_mutation_rejected(self) -> None:
        event_log = _RecordingEventLog()
        policy = _policy(rules=[PolicyRule(operation="entity.create", action="deny")])
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )

        result = executor.execute(_cmd())

        assert result.status == CommandStatus.REJECTED
        assert "Denied by policy" in result.message

        assert len(event_log.events) == 1
        event = event_log.events[0]
        assert event["event_type"] == EventType.MUTATION_REJECTED
        assert event["payload"]["reason"] == "policy_violation"
        assert event["actor"] == "mutation_executor"

    def test_denied_command_does_not_reach_the_handler(self) -> None:
        """Stage 2 must block before Stage 4 — no partial write."""

        class _ExplodingHandler:
            def handle(self, command: Command) -> tuple[str | None, str]:
                msg = "handler must not be reached"
                raise AssertionError(msg)

        policy = _policy(rules=[PolicyRule(operation="entity.create", action="deny")])
        executor = MutationExecutor(
            event_log=_RecordingEventLog(),
            handlers={Operation.ENTITY_CREATE: _ExplodingHandler()},
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )
        assert executor.execute(_cmd()).status == CommandStatus.REJECTED

    def test_rejection_reason_matches_the_capture_health_shape(self) -> None:
        """``MUTATION_REJECTED`` is what ``analyze health`` counts.

        The capture-health banner aggregates executor ``MUTATION_REJECTED``
        events; a policy rejection must land in that same channel rather
        than a private one, or a policy that blocks every write would look
        like silence instead of an outage.
        """
        event_log = _RecordingEventLog()
        policy = _policy(rules=[PolicyRule(operation="*", action="deny")])
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )
        executor.execute(_cmd())

        payload = event_log.events[0]["payload"]
        assert payload["status"] == CommandStatus.REJECTED
        assert payload["reason"] == "policy_violation"
        assert "operation" in payload
        assert "requested_by" in payload


# ---------------------------------------------------------------------------
# Warn enforcement is observable (it was not, before)
# ---------------------------------------------------------------------------


class TestWarningsReachTheCaller:
    def test_warn_enforcement_surfaces_on_a_successful_result(self) -> None:
        """Regression: warnings used to be computed and then dropped."""
        policy = _policy(
            rules=[PolicyRule(operation="entity.create", action="deny")],
            enforcement=Enforcement.WARN,
        )
        executor = MutationExecutor(
            event_log=_RecordingEventLog(),
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )

        result = executor.execute(_cmd())

        assert result.status == CommandStatus.SUCCESS
        assert len(result.warnings) == 1
        assert policy.policy_id in result.warnings[0]

    def test_warn_enforcement_is_recorded_on_the_audit_event(self) -> None:
        event_log = _RecordingEventLog()
        policy = _policy(
            rules=[PolicyRule(operation="entity.create", action="deny")],
            enforcement=Enforcement.WARN,
        )
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )
        executor.execute(_cmd())

        payload = event_log.events[0]["payload"]
        assert payload["status"] == CommandStatus.SUCCESS
        assert len(payload["policy_warnings"]) == 1

    def test_audit_only_stays_silent_to_the_caller(self) -> None:
        event_log = _RecordingEventLog()
        policy = _policy(
            rules=[PolicyRule(operation="entity.create", action="deny")],
            enforcement=Enforcement.AUDIT_ONLY,
        )
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _EchoHandler()},
            policy_gate=DefaultPolicyGate(policies=[policy]),
        )

        result = executor.execute(_cmd())

        assert result.status == CommandStatus.SUCCESS
        assert result.warnings == []
        assert "policy_warnings" not in event_log.events[0]["payload"]


# ---------------------------------------------------------------------------
# #413 — the write that laundered a damaged file past the strict reader
# ---------------------------------------------------------------------------


class TestEnforcementRaisesOnEveryDegenerateShape:
    """Strict means strict — including for files it *can* parse.

    Two of these shapes are the point. ``{}`` and a typo'd key are valid
    JSON, and ``data.get("policies", [])`` loaded them as **zero policies,
    silently**: a one-character hand-edit disabled every policy in the
    deployment while every surface reported normal. That fail-open needed
    no corrupt write at all, so it sat *underneath* the chain #413
    describes.
    """

    @pytest.mark.parametrize(
        ("name", "text", "_reason"),
        DEGENERATE_POLICY_FILES,
        ids=DEGENERATE_POLICY_IDS,
    )
    def test_shape_raises_rather_than_returning_no_policies(
        self, tmp_path: Path, name: str, text: str, _reason: str
    ) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        (stores_dir / POLICY_FILENAME).write_text(text, encoding="utf-8")

        with pytest.raises(ConfigError) as exc_info:
            load_policies(stores_dir)
        assert exc_info.value.setting == POLICY_FILENAME

    @pytest.mark.parametrize(
        ("name", "text", "_reason"),
        DEGENERATE_POLICY_FILES,
        ids=DEGENERATE_POLICY_IDS,
    )
    def test_the_gate_cannot_be_built_from_a_damaged_file(
        self, tmp_path: Path, name: str, text: str, _reason: str
    ) -> None:
        """Failing closed means the pipeline stops, not that it allows.

        ``build_policy_gate`` is what ``build_curate_executor`` calls, so a
        gate that could be built from a damaged file *is* the fail-open.
        """
        from trellis.stores.registry import StoreRegistry

        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        (stores_dir / POLICY_FILENAME).write_text(text, encoding="utf-8")

        with pytest.raises(ConfigError):
            build_policy_gate(StoreRegistry(config={}, stores_dir=stores_dir))

    def test_undecodable_bytes_raise_a_config_error_not_a_traceback(
        self, tmp_path: Path
    ) -> None:
        """``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``.

        Uncaught it still failed closed, but as a bare traceback with none
        of the recovery advice every other malformed shape here carries —
        and through the API's unhandled-exception handler that is a 500
        whose body says only "internal server error".
        """
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        (stores_dir / POLICY_FILENAME).write_bytes(b'{"policies": [\xff\xfe]}')

        with pytest.raises(ConfigError):
            load_policies(stores_dir)

    def test_the_missing_key_message_names_the_keys_it_found(
        self, tmp_path: Path
    ) -> None:
        """An operator's next move is to fix the key, so name it."""
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        (stores_dir / POLICY_FILENAME).write_text(
            '{"policys": [], "version": 1}', encoding="utf-8"
        )

        with pytest.raises(ConfigError, match="policys"):
            load_policies(stores_dir)


class TestTheWriteCannotLaunderTheDamage:
    """#413's chain, end to end, as behaviour rather than as prose."""

    def test_a_crud_write_cannot_replace_a_damaged_ruleset(
        self, tmp_path: Path
    ) -> None:
        """The whole defect in one test.

        Before the fix every assertion below was the opposite: the store
        loaded empty *without* recording anything, ``add`` succeeded and
        rewrote the file as ``{"policies": []}``, ``load_policies`` then
        parsed that perfectly valid file, and the gate allowed the very
        command the lost policy denied. Nothing in that sequence errored.
        """
        from trellis.errors import DegradedStoreWriteError
        from trellis.stores.policy_store import PolicyStore

        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        path = stores_dir / POLICY_FILENAME

        # A real, enforcing policy — then one character of damage to the
        # envelope, the cheapest way to reach the bug.
        denier = _policy(rules=[PolicyRule(operation="entity.create", action="deny")])
        path.write_text(
            json.dumps({"policys": [denier.model_dump(mode="json")]}),
            encoding="utf-8",
        )
        damaged_bytes = path.read_bytes()

        # 1. The CRUD reader degrades — by design, so ``policy list`` works.
        store = PolicyStore(path)
        assert store.list() == []
        assert store.is_degraded is True

        # 2. The write that used to launder it is refused...
        with pytest.raises(DegradedStoreWriteError) as exc_info:
            store.add(_policy(rules=[PolicyRule(operation="*", action="warn")]))
        assert exc_info.value.recovery == f"mv {path} {path}.corrupt"

        # 3. ...so the damaged bytes are still on disk...
        assert path.read_bytes() == damaged_bytes

        # 4. ...and enforcement is still failing closed on them. This is the
        #    assertion that would have caught the original defect: before the
        #    fix this returned ``[]`` and the pipeline ran ungoverned.
        with pytest.raises(ConfigError):
            load_policies(stores_dir)

    def test_a_partial_ruleset_is_never_written_back(self, tmp_path: Path) -> None:
        """The narrower, quieter version of the same laundering.

        A file with one bad row still lists policies, so nothing looks
        wrong — and a permitted write would rewrite it *without* the bad
        row, producing a valid file that enforcement then accepts as the
        whole ruleset.
        """
        from trellis.errors import DegradedStoreWriteError
        from trellis.stores.policy_store import PolicyStore

        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        path = stores_dir / POLICY_FILENAME
        good = _policy(rules=[PolicyRule(operation="entity.create", action="deny")])
        path.write_text(
            json.dumps(
                {"policies": [good.model_dump(mode="json"), {"policy_type": "bogus"}]}
            ),
            encoding="utf-8",
        )
        before = path.read_bytes()

        store = PolicyStore(path)
        assert [p.policy_id for p in store.list()] == [good.policy_id]

        with pytest.raises(DegradedStoreWriteError):
            store.add(_policy(rules=[PolicyRule(operation="*", action="warn")]))

        assert path.read_bytes() == before
        # And enforcement still refuses the file outright, rather than
        # quietly enforcing the one row that happened to parse.
        with pytest.raises(ConfigError):
            load_policies(stores_dir)


class TestZeroPoliciesStaysTransparent:
    """#370's property, restated against the two ways of reaching zero.

    #413 asks whether enforcement should distinguish "no file" from "a file
    that parsed to zero policies". It deliberately does not — every
    *dangerous* way of reaching zero now raises, so what is left in that
    class is an operator declaring zero, and the two must behave
    identically. If this test ever starts failing, the fix has changed the
    behaviour of every deployment that never asked for governance.
    """

    def test_absent_file_and_declared_empty_list_are_indistinguishable(
        self, tmp_path: Path
    ) -> None:
        absent_dir = tmp_path / "absent" / "stores"
        absent_dir.mkdir(parents=True)

        declared_dir = tmp_path / "declared" / "stores"
        declared_dir.mkdir(parents=True)
        (declared_dir / POLICY_FILENAME).write_text(
            '{"policies": []}', encoding="utf-8"
        )

        assert load_policies(absent_dir) == load_policies(declared_dir) == []

        from trellis.stores.registry import StoreRegistry

        absent_gate = build_policy_gate(StoreRegistry(config={}, stores_dir=absent_dir))
        declared_gate = build_policy_gate(
            StoreRegistry(config={}, stores_dir=declared_dir)
        )
        assert (
            absent_gate.check(_cmd()) == declared_gate.check(_cmd()) == (True, "", [])
        )

    def test_removing_the_last_policy_leaves_an_enforceable_file(
        self, tmp_path: Path
    ) -> None:
        """``{"policies": []}`` is reachable through a sanctioned surface.

        That is the reason it must not raise: it is what ``trellis policy
        remove`` writes when the last policy goes, so treating it as
        suspicious would make an ordinary operator action break the
        mutation pipeline.
        """
        from trellis.stores.policy_store import PolicyStore

        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True)
        store = PolicyStore(resolve_policy_path(stores_dir))
        policy = _policy(rules=[PolicyRule(operation="*", action="deny")])
        store.add(policy)
        store.remove(policy.policy_id)

        assert json.loads(
            (stores_dir / POLICY_FILENAME).read_text(encoding="utf-8")
        ) == {"policies": []}
        assert load_policies(stores_dir) == []


# ---------------------------------------------------------------------------
# Failing closed is right; failing closed invisibly is #425
# ---------------------------------------------------------------------------


class TestGateLoadFailureIsObservable:
    """A damaged policy file fails every write. Something must say so.

    The availability mirror of #413's fail-open: there the file was damaged
    and everything looked normal while nothing was *governed*; here the file
    is damaged and everything looks normal while nothing can be *written*.

    Three channels were structurally unable to see it. ``build_policy_gate``
    raises **before** a ``MutationExecutor`` exists, so no
    ``MUTATION_REJECTED``. Every surface builds its executor outside its
    ``WRITE_REJECTED`` boundary try/except, so no boundary rejection.
    ``capture_health`` counts exactly those two, so the banner built to
    notice a write surface going dark was blind to the one failure that
    darkens all of them at once.

    The fix emits into the channel that already exists rather than adding a
    probe — the same resolution #448 reached for the nightly advisory
    writer. These tests assert the signal reaches the two *readers*, not
    merely that an event was emitted: an event nothing aggregates is the
    ``status: "stale"`` field #448 is about.

    Latent, and worth saying plainly: a deployment that has declared zero
    policies has no file to damage. It goes live with the first policy.
    """

    @staticmethod
    def _registry(stores_dir: Path, event_log: Any) -> Any:
        """Minimal stand-in: ``build_policy_gate`` reads exactly two things."""
        return SimpleNamespace(
            stores_dir=stores_dir,
            operational=SimpleNamespace(event_log=event_log),
        )

    @staticmethod
    def _damage(tmp_path: Path, contents: str = "{ broken") -> Path:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True, exist_ok=True)
        (stores_dir / POLICY_FILENAME).write_text(contents, encoding="utf-8")
        return stores_dir

    @pytest.mark.parametrize(
        ("contents", "_reason"),
        [(shape[1], shape[2]) for shape in DEGENERATE_POLICY_FILES],
        ids=DEGENERATE_POLICY_IDS,
    )
    def test_every_damaged_shape_emits_the_signal(
        self, tmp_path: Path, contents: str, _reason: str
    ) -> None:
        """Over the shared table, not over one hand-picked typo.

        #423 widened the strict reader by three shapes, so the number of
        routes into "every write fails" grew. Parametrising over
        ``DEGENERATE_POLICY_FILES`` means the next widening cannot add a
        route that raises without also being observed.
        """
        stores_dir = self._damage(tmp_path, contents)
        event_log = _RecordingEventLog()

        with pytest.raises(ConfigError):
            build_policy_gate(self._registry(stores_dir, event_log))

        assert len(event_log.events) == 1
        event = event_log.events[0]
        assert event["event_type"] == EventType.WRITE_REJECTED
        assert event["actor"] == POLICY_GATE_SURFACE
        assert event["payload"]["tool"] == POLICY_GATE_SURFACE
        assert event["payload"]["error_class"] == "ConfigError"
        assert [r["kind"] for r in event["payload"]["rejections"]] == [
            "config_unreadable"
        ]
        # The recovery advice the ConfigError carries survives into the
        # event, so an operator reading it gets the fix without re-running
        # the command that failed.
        assert (
            str(stores_dir / POLICY_FILENAME)
            in (event["payload"]["rejections"][0]["msg"])
        )

    def test_the_label_stays_inside_the_global_recovery_class(self) -> None:
        """The two constants must not drift apart, silently.

        ``capture_health`` gives a ``config:``-prefixed surface its own
        recovery rule *because* no accepted write can ever carry this
        ``requested_by`` — without it the banner could fire and never clear.
        Rename this label out of the prefix and the banner silently reverts
        to the never-clearing behaviour, with every test here still green.
        """
        from trellis.ops.capture_health import _GLOBAL_SURFACE_PREFIX

        assert POLICY_GATE_SURFACE.startswith(_GLOBAL_SURFACE_PREFIX)

    def test_a_healthy_load_says_nothing(self, tmp_path: Path) -> None:
        """No file, and a declared-empty file, are both normal.

        Trellis ships zero policies, so a signal on the default posture
        would fire on every write on every deployment — which is how
        operators learn to ignore a signal.
        """
        event_log = _RecordingEventLog()

        absent = tmp_path / "absent"
        build_policy_gate(self._registry(absent, event_log))

        declared_empty = tmp_path / "empty" / "stores"
        declared_empty.mkdir(parents=True)
        (declared_empty / POLICY_FILENAME).write_text(
            '{"policies": []}', encoding="utf-8"
        )
        build_policy_gate(self._registry(declared_empty, event_log))

        populated = tmp_path / "full" / "stores"
        _write_policy_file(
            populated, [_policy(rules=[PolicyRule(operation="*", action="deny")])]
        )
        build_policy_gate(self._registry(populated, event_log))

        assert event_log.events == []

    def test_the_line_is_error_not_info(self, tmp_path: Path) -> None:
        """``trellis_cli.main._root`` *defaults* ``TRELLIS_LOG_LEVEL`` to
        ``WARNING``, so an ``info`` line here is filtered out of the CLI —
        the surface an operator most often meets this on, and the one where
        the raw ``ConfigError`` currently surfaces worst.

        The level is pinned because ``capture_logs`` swaps the processor
        chain but leaves ``wrapper_class`` alone: under pytest the bound
        logger records ``debug`` too, so every other assertion here would
        pass just as well against an invisible line (#395).
        """
        stores_dir = self._damage(tmp_path)

        with capture_logs() as logs, pytest.raises(ConfigError):
            build_policy_gate(self._registry(stores_dir, _RecordingEventLog()))

        lines = [e for e in logs if e["event"] == "policy_gate_load_failed"]
        assert len(lines) == 1
        assert lines[0]["log_level"] == "error"
        assert lines[0]["path"] == str(stores_dir / POLICY_FILENAME)
        assert lines[0]["surface"] == POLICY_GATE_SURFACE

    def test_analyze_health_sees_it(self, tmp_path: Path) -> None:
        """The reader, not the emit. An event nothing aggregates is #448."""
        from trellis.ops.write_health import summarize_write_health
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        stores_dir = self._damage(tmp_path)
        event_log = SQLiteEventLog(tmp_path / "events.db")
        registry = self._registry(stores_dir, event_log)

        for _ in range(3):
            with pytest.raises(ConfigError):
                build_policy_gate(registry)

        report = summarize_write_health(event_log, days=1)

        assert report.by_tool[POLICY_GATE_SURFACE].boundary_rejected == 3
        # Not folded into ``mcp:``-anything: this is not a tool surface.
        assert not any(k.startswith("mcp:") for k in report.by_tool)
        assert report.boundary_kinds[f"config_unreadable@{POLICY_FILENAME}"] == 3
        assert report.status == "warn"
        assert any("zero accepted writes" in r for r in report.reasons)
        # Recurrence reads correctly: the same unfixed file, over and over.
        assert report.repeated_collisions[0]["kind"] == "config_unreadable"

    def test_the_capture_banner_fires(self, tmp_path: Path) -> None:
        """#309's banner is the surface an agent actually sees.

        It needs ``threshold`` rejections *and* zero accepts for the same
        surface. The dedicated label has no accepts by construction, and a
        gate that will not load fails every governed write anyway — so this
        can reach the threshold but cannot cry wolf.
        """
        from trellis.ops.capture_health import (
            check_capture_health,
            format_capture_warning,
        )
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        stores_dir = self._damage(tmp_path)
        event_log = SQLiteEventLog(tmp_path / "events.db")
        registry = self._registry(stores_dir, event_log)

        for _ in range(2):
            with pytest.raises(ConfigError):
                build_policy_gate(registry)
        assert check_capture_health(event_log, threshold=3) is None

        with pytest.raises(ConfigError):
            build_policy_gate(registry)

        warning = check_capture_health(event_log, threshold=3)
        assert warning is not None
        assert warning.failing_surfaces == [POLICY_GATE_SURFACE]
        assert POLICY_GATE_SURFACE in format_capture_warning(warning)

    def test_telemetry_failure_never_replaces_the_config_error(
        self, tmp_path: Path
    ) -> None:
        """Fail-soft, both halves.

        The ``ConfigError`` names the file and the fix. A broken event log
        must not swap it for a ``RuntimeError`` from the telemetry path —
        that would take an operator further from the one-character edit that
        resolves this, which is what the whole issue is about.
        """
        stores_dir = self._damage(tmp_path)

        class _BrokenEventLog(_RecordingEventLog):
            def emit(self, *args: Any, **kwargs: Any) -> None:
                msg = "event log is down"
                raise RuntimeError(msg)

        with pytest.raises(ConfigError):
            build_policy_gate(self._registry(stores_dir, _BrokenEventLog()))

        class _NoOperationalPlane:
            stores_dir = None

            @property
            def operational(self) -> Any:
                msg = "operational plane unavailable"
                raise RuntimeError(msg)

        broken = _NoOperationalPlane()
        broken.stores_dir = stores_dir  # type: ignore[assignment]
        with pytest.raises(ConfigError):
            build_policy_gate(broken)  # type: ignore[arg-type]

    def test_the_signal_reaches_the_surfaces_that_build_executors(
        self, tmp_path: Path
    ) -> None:
        """Through ``build_curate_executor``, i.e. the way every surface
        (CLI, REST, MCP) actually reaches the gate — not only through the
        function under test.

        This is why the emit lives in ``build_policy_gate`` rather than
        inside one surface's ``WRITE_REJECTED`` boundary: one edit covers
        every caller, including ``trellis_workers``.
        """
        from trellis.stores.registry import StoreRegistry

        stores_dir = self._damage(tmp_path)
        registry = StoreRegistry(
            config={
                "event_log": {
                    "backend": "sqlite",
                    "db_path": str(tmp_path / "events.db"),
                }
            },
            stores_dir=stores_dir,
        )

        with pytest.raises(ConfigError):
            build_curate_executor(registry)

        events = registry.operational.event_log.get_events(
            event_type=EventType.WRITE_REJECTED, limit=10
        )
        assert [e.payload["tool"] for e in events] == [POLICY_GATE_SURFACE]
