"""``CommandResult.warnings`` reaches a REST caller.

``Enforcement.WARN`` exists to say "allow, but say so", and until now the
wire DTO that mirrors ``CommandResult`` had no field to say it in: three
routes hand-wrote the same five-field projection and all three stopped at
``created_id``. A policy could fire on every write and no HTTP client
could tell.

``warnings`` is present unconditionally, defaulting to ``[]``. That is the
opposite of the choice made for the audit *event*, and deliberately so:
an event keeps the key absent when empty, because a zero-policy
deployment must emit a byte-identical payload to the pre-gate world
(``test_policy_wiring.TestDefaultPostureIsTransparent``). A response a
caller parses has no such twin to be identical to, and there an
always-present ``[]`` is what distinguishes "the gate ran and nothing
warned" from "this build predates the field" — the same reasoning #404
used for the withholding note.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.mutate.policy_source import POLICY_FILENAME
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import curate, mutations

WARNING_TEXT = "Policy warning (pol-warn): unusual write"


def _policy(*, deny: bool) -> Policy:
    rules = [PolicyRule(operation="*", condition="unusual write", action="warn")]
    if deny:
        rules.append(
            PolicyRule(operation="*", condition="not permitted", action="deny")
        )
    return Policy(
        policy_id="pol-warn",
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level="global"),
        rules=rules,
        enforcement=Enforcement.ENFORCE,
    )


@pytest.fixture
def make_client(tmp_path: Path):
    """Build a client over a store dir, optionally with a policy declared.

    The gate is rebuilt per call from ``<stores_dir>/policies.json``, so the
    file has to be written before the request, not before the app.
    """
    created: list[StoreRegistry] = []

    def _factory(policies: list[Policy] | None = None) -> TestClient:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir(parents=True, exist_ok=True)
        if policies is not None:
            (stores_dir / POLICY_FILENAME).write_text(
                json.dumps({"policies": [p.model_dump(mode="json") for p in policies]}),
                encoding="utf-8",
            )
        registry = StoreRegistry(stores_dir=stores_dir)
        created.append(registry)
        app_module._registry = registry

        @asynccontextmanager
        async def noop_lifespan(app):
            yield

        app = FastAPI(lifespan=noop_lifespan)
        app.include_router(curate.router, prefix="/api/v1")
        app.include_router(mutations.router, prefix="/api/v1")
        return TestClient(app)

    yield _factory
    for registry in created:
        registry.close()
    app_module._registry = None


def _batch(client: TestClient) -> dict[str, Any]:
    """Run one ``entity.create`` through the batch route and return its result.

    The batch route is the surface under test because it returns the
    ``CommandResponse`` for *every* outcome. ``/precedents`` raises a 400 on
    a rejection, so a denied command's warnings are unreachable there by
    construction — a pre-existing shape of that route, not of the DTO.
    """
    resp = client.post(
        "/api/v1/commands/batch",
        json={
            "commands": [
                {
                    "operation": "entity.create",
                    "args": {"entity_type": "service", "name": "auth"},
                }
            ],
            "requested_by": "test",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) == 1
    return body["results"][0]


class TestWarningsReachTheCaller:
    def test_warn_policy_surfaces_on_a_successful_write(self, make_client) -> None:
        result = _batch(make_client([_policy(deny=False)]))
        assert result["status"] == "success"
        assert result["warnings"] == [WARNING_TEXT]

    def test_warn_before_deny_surfaces_on_a_rejection(self, make_client) -> None:
        """The warning that the gate accumulated before the rule that blocked.

        Worth its own case: this is the path where the write did not happen,
        so the response is the only place the caller can learn that a second
        policy also had something to say.
        """
        result = _batch(make_client([_policy(deny=True)]))
        assert result["status"] == "rejected"
        assert "not permitted" in result["message"]
        assert result["warnings"] == [WARNING_TEXT]


class TestTheKeyIsUnconditional:
    @pytest.mark.parametrize(
        ("policies", "label"),
        [(None, "no_policy_file"), ([], "declared_zero")],
        ids=["no_policy_file", "declared_zero"],
    )
    def test_empty_list_not_a_missing_key(
        self, make_client, policies: list[Policy] | None, label: str
    ) -> None:
        result = _batch(make_client(policies))
        assert result["status"] == "success"
        assert "warnings" in result, f"{label}: key absent, not merely empty"
        assert result["warnings"] == []
