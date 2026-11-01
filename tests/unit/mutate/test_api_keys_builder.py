"""``build_api_keys_executor``: who tolerates a failed event-log open, and what it says.

Only the CLI recovery path passes ``tolerate_unopenable_event_log=True``. A
tolerated run swaps in a no-op log, so the executor's own ``mutation.executed``
record is gone even when the handler's later access to the real log works; the
result must say so rather than report success with ``warnings: []``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trellis.auth import SCOPE_READ, generate_api_key
from trellis.mutate import CommandStatus, build_api_keys_executor
from trellis.mutate.api_key_commands import build_revoke_command
from trellis.mutate.executor import AUDIT_EMIT_FAILED_MARKER
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

from ...flaky_registry import FlakyRegistry


@pytest.fixture
def registry(tmp_path: Path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    yield reg
    reg.close()


def _victim(registry: StoreRegistry) -> str:
    _, record = generate_api_key("victim", [SCOPE_READ])
    registry.operational.api_key_store.create(record)
    return record.key_id


def test_the_default_does_not_tolerate_an_unopenable_event_log(
    registry: StoreRegistry,
) -> None:
    with pytest.raises(sqlite3.OperationalError):
        build_api_keys_executor(FlakyRegistry(registry, fail_first=10**6))


def test_a_healthy_open_adds_no_warning(registry: StoreRegistry) -> None:
    key_id = _victim(registry)
    executor = build_api_keys_executor(registry, tolerate_unopenable_event_log=True)
    result = executor.execute(build_revoke_command(key_id, requested_by="cli:admin"))
    assert result.status == CommandStatus.SUCCESS
    assert result.warnings == []
    log = registry.operational.event_log
    assert log.count(event_type=EventType.MUTATION_EXECUTED) == 1


def test_a_failed_first_open_and_a_working_second_one_still_warns(
    registry: StoreRegistry,
) -> None:
    key_id = _victim(registry)
    flaky = FlakyRegistry(registry, fail_first=1)
    executor = build_api_keys_executor(flaky, tolerate_unopenable_event_log=True)
    result = executor.execute(build_revoke_command(key_id, requested_by="cli:admin"))
    assert result.status == CommandStatus.SUCCESS
    assert result.warnings, "the missing audit record was reported as nothing"
    assert any("audit_event_not_recorded" in w for w in result.warnings)
    assert all("SECRET-DRIVER-TEXT" not in w for w in result.warnings)
    log = registry.operational.event_log
    assert log.count(event_type=EventType.API_KEY_REVOKED) == 1
    assert log.count(event_type=EventType.MUTATION_EXECUTED) == 0
    assert AUDIT_EMIT_FAILED_MARKER == "audit_event_not_recorded"


def test_a_log_that_stays_dead_warns_too(registry: StoreRegistry) -> None:
    key_id = _victim(registry)
    flaky = FlakyRegistry(registry, fail_first=10**6)
    executor = build_api_keys_executor(flaky, tolerate_unopenable_event_log=True)
    result = executor.execute(build_revoke_command(key_id, requested_by="cli:admin"))
    assert result.status == CommandStatus.SUCCESS
    assert any("audit_event_not_recorded" in w for w in result.warnings)
    record = registry.operational.api_key_store.get(key_id)
    assert record is not None
    assert record.revoked_at is not None
