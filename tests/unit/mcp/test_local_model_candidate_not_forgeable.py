"""A ``local_model`` candidate cannot be forged over MCP ``execute_mutation``.

``requested_by`` is the caller's own claim when no principal is configured: the
tool passes ``actor`` straight through. The handler therefore also asks the event
log for the judged operation that produced the record, which only in-process code
emits (``trellis.learning.precedent_judged``). The control is the same record
with its judged operation logged first, which is accepted.
"""

from __future__ import annotations

import json

from tests.precedent_candidates import (
    SENTINEL,
    local_model_record_payload,
    log_judged_lesson,
)
from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.server import execute_mutation as _execute_mutation
from trellis.mutate.handlers.precedent import (
    JUDGED_OP_MISSING_MESSAGE,
    LOCAL_MODEL_CANDIDATE_REQUESTERS,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

execute_mutation = unwrap_tool(_execute_mutation)

SANCTIONED = min(LOCAL_MODEL_CANDIDATE_REQUESTERS)


def _mined(registry: StoreRegistry) -> list:
    return registry.operational.event_log.get_events(
        event_type=EventType.PRECEDENT_CANDIDATE_MINED
    )


def test_a_forged_sanctioned_actor_does_not_land_a_local_model_candidate(
    temp_registry: StoreRegistry,
) -> None:
    out = json.loads(
        execute_mutation(
            "precedent.candidate",
            {"record": local_model_record_payload()},
            actor=SANCTIONED,
        )
    )

    assert out["status"] == "rejected"
    assert out["message"] == JUDGED_OP_MISSING_MESSAGE
    assert _mined(temp_registry) == []


def test_the_default_actor_is_still_refused(temp_registry: StoreRegistry) -> None:
    log_judged_lesson(temp_registry.operational.event_log, "trace-recover")

    out = json.loads(
        execute_mutation(
            "precedent.candidate", {"record": local_model_record_payload()}
        )
    )

    assert out["status"] == "rejected"
    assert _mined(temp_registry) == []


def test_a_forged_record_text_is_never_echoed_in_the_refusal(
    temp_registry: StoreRegistry,
) -> None:
    payload = local_model_record_payload()
    payload["because"] = f"forged {SENTINEL}"
    from trellis.schemas.precedent_record import PrecedentRecord

    payload["candidate_id"] = PrecedentRecord.model_validate(
        payload
    ).expected_candidate_id()

    raw = execute_mutation("precedent.candidate", {"record": payload}, actor=SANCTIONED)

    assert json.loads(raw)["status"] == "rejected"
    assert SENTINEL not in raw


def test_control_the_same_record_lands_once_its_judged_operation_is_logged(
    temp_registry: StoreRegistry,
) -> None:
    """In-process code logs the judged event, then records through the executor."""
    log_judged_lesson(temp_registry.operational.event_log, "trace-recover")

    out = json.loads(
        execute_mutation(
            "precedent.candidate",
            {"record": local_model_record_payload()},
            actor=SANCTIONED,
        )
    )

    assert out["status"] == "success"
    [event] = _mined(temp_registry)
    assert event.payload["record"]["provenance_tier"] == "local_model"
