"""``retention.prune`` over REST names a capped entity scan in its message.

The route returns the executor's ``CommandResponse`` unchanged, so the
resolver's ``scan_truncated`` reaches an HTTP caller as the message the
handler composes. The status stays ``success``: a capped scan is reported,
not refused.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.mutate import handlers as handlers_module
from trellis.mutate import retention as retention_module
from trellis.schemas.extraction import (
    EXTRACTION_STATUS_PROPERTY,
    EXTRACTION_STATUS_UNCONFIRMED,
)
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import curate, mutations


@pytest.fixture
def client(tmp_path: Path):
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    registry = StoreRegistry(stores_dir=stores_dir)
    for i in range(3):
        registry.knowledge.graph_store.upsert_node(
            node_id=f"m{i}",
            node_type="concept",
            properties={
                "name": f"m{i}",
                EXTRACTION_STATUS_PROPERTY: EXTRACTION_STATUS_UNCONFIRMED,
            },
        )
    app_module._registry = registry

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(curate.router, prefix="/api/v1")
    app.include_router(mutations.router, prefix="/api/v1")
    yield TestClient(app)
    registry.close()
    app_module._registry = None


def _prune(client: TestClient) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/commands/batch",
        json={
            "commands": [
                {
                    "operation": "retention.prune",
                    "args": {
                        "criteria": {"unconfirmed_mints": True},
                        "reason": "r",
                        "dry_run": True,
                    },
                }
            ],
            "requested_by": "test",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["results"][0]


@pytest.mark.parametrize(("cap", "named"), [(2, True), (10, False)])
def test_the_message_says_the_scan_stopped_early_only_when_it_did(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, cap: int, named: bool
) -> None:
    monkeypatch.setattr(retention_module, "MAX_ENTITIES_SCANNED", cap)
    monkeypatch.setattr(handlers_module, "MAX_ENTITIES_SCANNED", cap)
    monkeypatch.setattr(retention_module, "_SCAN_PAGE", 2)

    result = _prune(client)

    assert result["status"] == "success"
    assert (
        "entity scan stopped early (the 2-entity cap," in result["message"]
    ) is named
    assert "scan capped" not in result["message"]
