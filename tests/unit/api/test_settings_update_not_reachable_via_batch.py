"""``settings.update`` is unreachable from the generic batch route (B1).

``POST /commands/batch`` builds :func:`~trellis.mutate.build_curate_executor`
(``trellis_api/routes/mutations.py``), the same factory MCP's
``execute_mutation`` builds. Neither registers
:data:`~trellis.mutate.commands.Operation.SETTINGS_UPDATE` any more (gate
report on #857, B1): a caller who knows the operation name and schema
(``{"name": ..., "value": ...}``) and is willing to supply a
``target_type="setting"``/``target_id=<name>`` pair that would satisfy the
handler's own defence-in-depth check still cannot reach the settings store
through this surface, because the handler is never registered here at all.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.stores.registry import StoreRegistry
from trellis.stores.settings_store import SettingsStore, resolve_settings_path
from trellis_api.routes import mutations


@pytest.fixture
def client(tmp_path: Path):
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir(parents=True, exist_ok=True)
    registry = StoreRegistry(stores_dir=stores_dir)
    app_module._registry = registry

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(mutations.router, prefix="/api/v1")
    yield TestClient(app)
    registry.close()
    app_module._registry = None


def test_settings_update_on_batch_is_refused_not_executed(
    client: TestClient, tmp_path: Path
) -> None:
    resp = client.post(
        "/api/v1/commands/batch",
        json={
            "commands": [
                {
                    "operation": "settings.update",
                    # A fully-targeted command, as if the caller had read
                    # the handler's own defence-in-depth requirement and
                    # tried to satisfy it anyway -- still refused, because
                    # no handler for this operation is registered on the
                    # executor this route builds.
                    "target_id": "pack_holdout_rate",
                    "target_type": "setting",
                    "args": {"name": "pack_holdout_rate", "value": 0.9},
                }
            ],
            "requested_by": "test:batch-bypass-attempt",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    (result,) = body["results"]
    assert result["status"] == "failed"
    assert "No handler registered" in result["message"]
    assert body["succeeded"] == 0

    # And the write really did not happen: either no settings file exists
    # at all (the stronger guarantee), or one exists but has no override.
    path = resolve_settings_path(tmp_path / "stores")
    if path is not None and path.exists():
        assert SettingsStore(path).get("pack_holdout_rate") is None
