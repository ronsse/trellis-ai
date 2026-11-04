"""``GET /api/v1/analyze/intent-families``: the relabel report over HTTP.

The route runs the same library function the CLI does, is not readout-gated (it
carries no outcome figure of its own and changes nothing), takes bounded,
validated params, and answers a malformed ``retrieval.intent_families`` block as
a 409 rather than reading it as the shipped rules.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.learning import _intent_family_seed as seed_mod
from trellis.errors import TrellisError
from trellis.learning.intent_families import load_rule_set, summarize_intent_families
from trellis.stores.registry import StoreRegistry
from trellis_api.middleware import trellis_error_handler
from trellis_api.routes import analyze

QUERY = "days=30&until=2026-11-04T12:00:00"


def _client(registry: StoreRegistry) -> TestClient:
    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    app_module._registry = registry
    app = FastAPI(lifespan=noop_lifespan)
    app.add_exception_handler(TrellisError, trellis_error_handler)
    app.include_router(analyze.router, prefix="/api/v1", tags=["analyze"])
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def registry(tmp_path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    seed_mod.seed(reg.operational.event_log)
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    return _client(registry)


@pytest.fixture(autouse=True)
def _clear_cache():
    analyze._CACHE.clear()
    yield
    analyze._CACHE.clear()


def test_the_report_is_listed_and_not_readout_gated(client):
    names = {
        row["name"]: row for row in client.get("/api/v1/analyze").json()["reports"]
    }

    assert "intent-families" in names
    assert names["intent-families"]["default_params"]["days"] == 30
    assert "measurement" in names["intent-families"]["description"]
    assert analyze._REPORTS["intent-families"].readout_gated is False


def test_the_route_returns_the_report_the_library_computes(client, registry):
    resp = client.get(f"/api/v1/analyze/intent-families?{QUERY}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["report"] == "intent-families"
    assert body["cached"] is False
    expected = summarize_intent_families(
        registry.operational.event_log, days=30, until=seed_mod.UNTIL
    ).model_dump(mode="json")
    assert body["data"] == expected
    assert body["data"]["packs_analyzed"] == 38
    assert body["data"]["fallback_packs"] == 5


def test_a_second_identical_request_is_served_from_the_cache(client):
    client.get(f"/api/v1/analyze/intent-families?{QUERY}")

    assert (
        client.get(f"/api/v1/analyze/intent-families?{QUERY}").json()["cached"] is True
    )


@pytest.mark.parametrize(
    "query",
    ["days=0", "days=366", "since=last%20tuesday", "until=nope", "limit=0", "weeks=3"],
)
def test_out_of_bounds_or_unknown_params_are_a_422(client, query):
    assert client.get(f"/api/v1/analyze/intent-families?{query}").status_code == 422


def test_a_configured_set_is_used(tmp_path):
    reg = StoreRegistry(
        stores_dir=tmp_path / "stores",
        retrieval_config={
            "intent_families": {
                "fallback": "misc",
                "rules": [{"id": "x.fix", "family": "repairs", "keywords": ["fix"]}],
            }
        },
    )
    seed_mod.seed(reg.operational.event_log)
    try:
        data = (
            _client(reg).get(f"/api/v1/analyze/intent-families?{QUERY}").json()["data"]
        )
    finally:
        reg.close()
        app_module._registry = None

    assert data["rule_set"]["source"] == "config"
    assert data["rule_set"]["families"] == ["repairs", "misc"]
    assert load_rule_set(reg.retrieval_config).source == "config"


def test_a_malformed_config_is_a_409_not_the_shipped_rules(tmp_path):
    reg = StoreRegistry(
        stores_dir=tmp_path / "stores", retrieval_config={"intent_families": []}
    )
    seed_mod.seed(reg.operational.event_log)
    try:
        resp = _client(reg).get(f"/api/v1/analyze/intent-families?{QUERY}")
    finally:
        reg.close()
        app_module._registry = None

    assert resp.status_code == 409
    assert "families" not in resp.json()


def test_no_intent_text_is_returned(tmp_path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    seed_mod.seed(reg.operational.event_log)
    seed_mod.seed_sentinel(reg.operational.event_log)
    try:
        resp = _client(reg).get(f"/api/v1/analyze/intent-families?{QUERY}")
    finally:
        reg.close()
        app_module._registry = None

    families = {row["family"]: row for row in resp.json()["data"]["families"]}
    assert families["bugfix"]["packs"] == 21
    assert seed_mod.SENTINEL not in json.dumps(resp.json()).lower()
