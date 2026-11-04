"""A ``local_model`` candidate cannot be forged through ``POST /commands/batch``.

With no principal the route passes the body's ``requested_by`` through, so the
sanctioned requester string is one any caller can type. The handler's second
conjunct, the judged operation in the event log, is what refuses it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.precedent_candidates import local_model_record_payload, log_judged_lesson
from trellis.mutate.handlers.precedent import (
    JUDGED_OP_MISSING_MESSAGE,
    LOCAL_MODEL_CANDIDATE_REQUESTERS,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import mutations

SANCTIONED = min(LOCAL_MODEL_CANDIDATE_REQUESTERS)


@pytest.fixture
def registry(tmp_path: Path):
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir(parents=True, exist_ok=True)
    registry = StoreRegistry(stores_dir=stores_dir)
    app_module._registry = registry
    yield registry
    registry.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry):
    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(mutations.router, prefix="/api/v1")
    return TestClient(app)


def _post(client: TestClient) -> dict:
    resp = client.post(
        "/api/v1/commands/batch",
        json={
            "commands": [
                {
                    "operation": "precedent.candidate",
                    "args": {"record": local_model_record_payload()},
                }
            ],
            "requested_by": SANCTIONED,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _mined(registry: StoreRegistry) -> list:
    return registry.operational.event_log.get_events(
        event_type=EventType.PRECEDENT_CANDIDATE_MINED
    )


def test_a_forged_requester_does_not_land_a_local_model_candidate(
    client: TestClient, registry: StoreRegistry
) -> None:
    body = _post(client)

    (result,) = body["results"]
    assert result["status"] == "rejected"
    assert result["message"] == JUDGED_OP_MISSING_MESSAGE
    assert body["succeeded"] == 0
    assert _mined(registry) == []


def test_control_the_same_batch_lands_once_its_judged_operation_is_logged(
    client: TestClient, registry: StoreRegistry
) -> None:
    log_judged_lesson(registry.operational.event_log, "trace-recover")

    body = _post(client)

    (result,) = body["results"]
    assert result["status"] == "success"
    assert len(_mined(registry)) == 1
