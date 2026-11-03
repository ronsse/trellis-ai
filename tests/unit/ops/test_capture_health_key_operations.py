"""API-key operations are not capture: neither a rejection nor an accept.

REST stamps ``requested_by`` per key (``api:<name>``), and the same label also
ingests, so a surface deny-list cannot express this; the filter is on the
event's ``operation``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.auth import generate_api_key
from trellis.mutate import build_api_keys_executor
from trellis.mutate.api_key_commands import build_mint_command, build_revoke_command
from trellis.mutate.commands import Operation
from trellis.ops.capture_health import NON_CAPTURE_OPERATIONS, check_capture_health
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

LABEL = "api:root"


@pytest.fixture
def registry(tmp_path: Path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    yield reg
    reg.close()


def _emit(registry: StoreRegistry, event_type: EventType, operation: str) -> None:
    registry.operational.event_log.emit(
        event_type,
        "mutation_executor",
        entity_id="x",
        entity_type="t",
        payload={"operation": operation, "requested_by": LABEL, "reason": "validate"},
    )


def _mint(registry: StoreRegistry) -> None:
    _, _, command = build_mint_command("k", "read", requested_by=LABEL)
    assert build_api_keys_executor(registry).execute(command).status == "success"


def test_the_literals_match_the_operations() -> None:
    assert {Operation.API_KEY_MINT.value, Operation.API_KEY_REVOKE.value} == (
        NON_CAPTURE_OPERATIONS
    )


def test_three_refused_key_operations_raise_no_banner(registry: StoreRegistry) -> None:
    executor = build_api_keys_executor(registry)
    for _ in range(3):
        result = executor.execute(
            build_revoke_command("ffffffffffff", requested_by=LABEL)
        )
        assert result.status == "rejected"
    assert (
        registry.operational.event_log.count(event_type=EventType.MUTATION_REJECTED)
        == 3
    )
    assert check_capture_health(registry.operational.event_log, threshold=3) is None


def test_the_same_three_rejections_of_a_real_write_do_raise_it(
    registry: StoreRegistry,
) -> None:
    for _ in range(3):
        _emit(registry, EventType.MUTATION_REJECTED, "evidence.ingest")
    warning = check_capture_health(registry.operational.event_log, threshold=3)
    assert warning is not None
    assert warning.failing_surfaces == [LABEL]


def test_a_successful_mint_does_not_clear_a_real_banner(
    registry: StoreRegistry,
) -> None:
    for _ in range(3):
        _emit(registry, EventType.MUTATION_REJECTED, "evidence.ingest")
    _mint(registry)
    assert check_capture_health(registry.operational.event_log, threshold=3) is not None
    _emit(registry, EventType.MUTATION_EXECUTED, "evidence.ingest")
    assert check_capture_health(registry.operational.event_log, threshold=3) is None


def test_a_real_accept_is_found_behind_many_key_operations(
    registry: StoreRegistry,
) -> None:
    for _ in range(3):
        _emit(registry, EventType.MUTATION_REJECTED, "evidence.ingest")
    _emit(registry, EventType.MUTATION_EXECUTED, "evidence.ingest")
    for _ in range(25):
        _emit(registry, EventType.MUTATION_EXECUTED, "api_key.mint")
    assert check_capture_health(registry.operational.event_log, threshold=3) is None


def test_generate_api_key_is_unused_here() -> None:
    assert generate_api_key("k", ["read"])[1].name == "k"


def test_an_exhausted_accept_search_keeps_the_banner(registry: StoreRegistry) -> None:
    """More key accepts than the search reads must not read as "a real accept"."""
    for _ in range(3):
        _emit(registry, EventType.MUTATION_REJECTED, "evidence.ingest")
    for _ in range(120):
        _emit(registry, EventType.MUTATION_EXECUTED, "api_key.mint")
    warning = check_capture_health(registry.operational.event_log, threshold=3)
    assert warning is not None
    assert warning.failing_surfaces == [LABEL]


def test_an_unhashable_operation_is_a_real_operation_not_a_crash(
    registry: StoreRegistry,
) -> None:
    for _ in range(3):
        registry.operational.event_log.emit(
            EventType.MUTATION_REJECTED,
            "mutation_executor",
            entity_id="x",
            entity_type="t",
            payload={
                "operation": ["api_key.mint"],
                "requested_by": LABEL,
                "reason": "validate",
            },
        )
    warning = check_capture_health(registry.operational.event_log, threshold=3)
    assert warning is not None
    registry.operational.event_log.emit(
        EventType.MUTATION_EXECUTED,
        "mutation_executor",
        entity_id="x",
        entity_type="t",
        payload={"operation": {"nested": 1}, "requested_by": LABEL},
    )
    assert check_capture_health(registry.operational.event_log, threshold=3) is None
