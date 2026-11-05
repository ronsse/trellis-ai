"""``POST /api/v1/traces`` refuses a trace nested past the bound (#1186)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.stores.base.trace import MAX_TRACE_PAYLOAD_DEPTH
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app


@pytest.fixture
def registry(tmp_path: Path):
    stores = tmp_path / "stores"
    stores.mkdir()
    reg = StoreRegistry(stores_dir=stores)
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


def _body(levels: int) -> dict[str, Any]:
    result: dict[str, Any] = {"leaf": 1}
    for _ in range(levels - 1):
        result = {"k": result}
    return {
        "source": "agent",
        "intent": "probe",
        "context": {},
        "steps": [{"step_type": "t", "name": "n", "result": result}],
    }


def test_a_trace_at_the_bound_is_stored_and_listed(
    client: TestClient, registry: StoreRegistry
) -> None:
    resp = client.post("/api/v1/traces", json=_body(MAX_TRACE_PAYLOAD_DEPTH))

    assert resp.status_code == 200, resp.text
    assert len(registry.operational.trace_store.query()) == 1


@pytest.mark.parametrize("levels", [MAX_TRACE_PAYLOAD_DEPTH + 1, 200])
def test_a_trace_past_the_bound_is_a_clear_400_and_stores_nothing(
    client: TestClient, registry: StoreRegistry, levels: int
) -> None:
    resp = client.post("/api/v1/traces", json=_body(levels))

    assert resp.status_code == 400
    assert f"more than {MAX_TRACE_PAYLOAD_DEPTH} levels" in resp.json()["detail"]
    assert registry.operational.trace_store.count() == 0
