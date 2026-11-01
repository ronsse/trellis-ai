"""A rejected write followed by an accepted call of the same tool is counted.

The boundary records a rejection and nothing after it, so the health report
could not say whether a caller fixed its payload and got through or gave up.
``_ExplainArgumentRejections`` now notes a rejection in memory and, when the
same tool next returns without raising, emits ``WRITE_RECOVERED``.

These drive the real dispatch through an in-memory ``fastmcp.Client``: the
accepted-call hook lives in the middleware, so a direct call to a tool function
never reaches it. The pairing is keyed by tool inside a window, which is a
choice and not a limit of the transport; see ``RejectionRecoveryTracker``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client

import trellis.mcp.server as server_mod
from trellis.mcp.auth import set_auth_enforced
from trellis.ops.write_health import (
    RECOVERY_WINDOW_SECONDS,
    RejectionRecoveryTracker,
    summarize_write_health,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

_BAD = {"pack_id": "pack-1", "item_ids": ["a"]}  # the misspelt argument
_GOOD = {"pack_id": "pack-1", "rating": 0.5}
_PAIRING_DOWN = "pairing down"


@pytest.fixture(autouse=True)
def _allow_in_memory_client() -> None:
    set_auth_enforced(enforced=False)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A fresh tracker per test, on a clock the test advances."""
    now = [5000.0]
    monkeypatch.setattr(
        server_mod,
        "_REJECTION_RECOVERY",
        RejectionRecoveryTracker(clock=lambda: now[0]),
    )
    return now


async def _call(name: str, args: dict[str, Any]) -> Any:
    async with Client(server_mod.mcp) as client:
        return await client.call_tool(name, args, raise_on_error=False)


def _recoveries(registry: StoreRegistry) -> list[Any]:
    return list(
        registry.operational.event_log.get_events(
            event_type=EventType.WRITE_RECOVERED, limit=50
        )
    )


async def test_a_rejection_then_an_acceptance_is_recovered(
    temp_registry: StoreRegistry, clock: list[float]
) -> None:
    rejected = await _call("record_feedback", _BAD)
    assert rejected.is_error
    accepted = await _call("record_feedback", _GOOD)
    assert not accepted.is_error

    (event,) = _recoveries(temp_registry)
    assert event.source == "mcp:record_feedback"
    assert event.payload == {
        "tool": "record_feedback",
        "rejected_attempts": 1,
        "window_s": RECOVERY_WINDOW_SECONDS,
    }


async def test_the_recovery_reaches_the_health_report(
    temp_registry: StoreRegistry, clock: list[float]
) -> None:
    await _call("record_feedback", _BAD)
    await _call("record_feedback", _GOOD)

    recovery = summarize_write_health(
        temp_registry.operational.event_log, days=1
    ).recovery
    assert (recovery.tracked_rejections, recovery.recovered_rejections) == (1, 1)
    assert recovery.recovered_share == 1.0


async def test_one_acceptance_recovers_each_pending_rejection_once(
    temp_registry: StoreRegistry, clock: list[float]
) -> None:
    await _call("record_feedback", _BAD)
    await _call("record_feedback", _BAD)
    await _call("record_feedback", _GOOD)
    await _call("record_feedback", _GOOD)

    (event,) = _recoveries(temp_registry)
    assert event.payload["rejected_attempts"] == 2


async def test_an_acceptance_with_no_rejection_emits_nothing(
    temp_registry: StoreRegistry, clock: list[float]
) -> None:
    await _call("record_feedback", _GOOD)
    assert _recoveries(temp_registry) == []


async def test_another_tools_acceptance_does_not_recover_it(
    temp_registry: StoreRegistry, clock: list[float]
) -> None:
    await _call("record_feedback", _BAD)
    # ``intent`` is the argument the tool takes. A misspelt one is itself a
    # rejection, which would never reach the hook and leave this vacuous.
    other = await _call("get_context", {"intent": "anything"})
    assert not other.is_error
    assert _recoveries(temp_registry) == []


async def test_the_tool_that_was_rejected_is_the_one_that_is_recovered(
    temp_registry: StoreRegistry, clock: list[float]
) -> None:
    """An acceptance elsewhere leaves the rejection pending for its own tool."""
    await _call("record_feedback", _BAD)
    other = await _call("get_context", {"intent": "anything"})
    assert not other.is_error
    assert _recoveries(temp_registry) == []

    await _call("record_feedback", _GOOD)

    (event,) = _recoveries(temp_registry)
    assert event.source == "mcp:record_feedback"
    assert event.payload["rejected_attempts"] == 1


async def test_an_acceptance_after_the_window_does_not_recover_it(
    temp_registry: StoreRegistry, clock: list[float]
) -> None:
    await _call("record_feedback", _BAD)
    clock[0] += RECOVERY_WINDOW_SECONDS + 1
    await _call("record_feedback", _GOOD)
    assert _recoveries(temp_registry) == []


async def test_a_rejected_call_after_a_rejection_is_not_a_recovery(
    temp_registry: StoreRegistry, clock: list[float]
) -> None:
    await _call("record_feedback", _BAD)
    await _call("record_feedback", _BAD)
    assert _recoveries(temp_registry) == []


async def test_a_failing_pairing_does_not_fail_the_accepted_call(
    temp_registry: StoreRegistry,
    clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_: Any, **__: Any) -> None:
        raise RuntimeError(_PAIRING_DOWN)

    await _call("record_feedback", _BAD)
    monkeypatch.setattr(server_mod._REJECTION_RECOVERY, "claim_recovery", _boom)
    accepted = await _call("record_feedback", _GOOD)
    assert not accepted.is_error


async def test_a_stale_rejection_is_not_recovered_by_a_later_acceptance(
    temp_registry: StoreRegistry, clock: list[float]
) -> None:
    """Reject, wait out the window, reject again, accept: one recovered, not two."""
    await _call("record_feedback", _BAD)
    clock[0] += 24 * 3600
    await _call("record_feedback", _BAD)
    await _call("record_feedback", _GOOD)

    (event,) = _recoveries(temp_registry)
    assert event.payload["rejected_attempts"] == 1


async def test_a_recorded_feedback_is_an_accepted_call_in_the_health_report(
    temp_registry: StoreRegistry, clock: list[float]
) -> None:
    """The tool's accept is the ``FEEDBACK_RECORDED`` row it writes as ``mcp``."""
    await _call("record_feedback", _BAD)
    await _call("record_feedback", _GOOD)

    stats = summarize_write_health(temp_registry.operational.event_log, days=1).by_tool[
        "mcp:record_feedback"
    ]
    assert (stats.accepted, stats.attempts) == (1, 2)


async def test_a_duplicate_reply_that_wrote_nothing_still_counts_as_accepted(
    temp_registry: StoreRegistry, clock: list[float]
) -> None:
    """Accepted means returned without raising.

    A rejected ``save_memory`` followed by one that answers a duplicate stored
    nothing, and still pairs with the rejection and still leaves a no-op row.
    The report cannot tell it from a write that landed; the printed note says so.
    """
    memory = "a note about where the spare cables are kept on the second shelf"
    first = await _call("save_memory", {"content": memory})
    assert not first.is_error
    rejected = await _call("save_memory", {"content": memory, "bogus_arg": 1})
    assert rejected.is_error
    again = await _call("save_memory", {"content": memory})
    assert not again.is_error

    (recovered,) = _recoveries(temp_registry)
    assert recovered.source == "mcp:save_memory"
    noops = temp_registry.operational.event_log.get_events(
        event_type=EventType.WRITE_NOOP, limit=10
    )
    assert [e.payload["reason"] for e in noops] == ["exact_duplicate"]
