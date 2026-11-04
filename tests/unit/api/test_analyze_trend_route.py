"""``GET /api/v1/analyze/trend``: the weekly series over HTTP.

The route runs the same library function the CLI does, is not readout-gated
(it carries no outcome figure), and takes bounded, validated params.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.analyze import _trend_seed as seed_mod
from trellis.analyze.trend import summarize_trend
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import analyze

QUERY = "weeks=3&until=2026-11-04T12:00:00"


@pytest.fixture
def registry(tmp_path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    seed_mod.seed(reg.operational.event_log)
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(analyze.router, prefix="/api/v1", tags=["analyze"])
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_cache():
    analyze._CACHE.clear()
    yield
    analyze._CACHE.clear()


def test_the_report_is_listed_and_not_readout_gated(client):
    names = {
        row["name"]: row for row in client.get("/api/v1/analyze").json()["reports"]
    }

    assert "trend" in names
    assert names["trend"]["default_params"]["weeks"] == 8
    assert "sealed" in names["trend"]["description"]
    assert analyze._REPORTS["trend"].readout_gated is False


def test_the_route_returns_the_series_the_library_computes(client, registry):
    resp = client.get(f"/api/v1/analyze/trend?{QUERY}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["report"] == "trend"
    assert body["cached"] is False
    data = body["data"]
    expected = summarize_trend(
        registry.operational.event_log,
        registry.knowledge.graph_store,
        weeks=3,
        until=seed_mod.UNTIL,
    ).model_dump(mode="json")
    for volatile in ("generated_at",):
        data.pop(volatile), expected.pop(volatile)
    data["graph_shape"].pop("as_of"), expected["graph_shape"].pop("as_of")
    assert data == expected
    assert [week["week"] for week in data["weeks"]] == [
        "2026-W43",
        "2026-W44",
        "2026-W45",
    ]
    assert [build["commit"] for build in data["builds"]] == ["aaaaaaa", "bbbbbbb", None]


def test_a_second_identical_request_is_served_from_the_cache(client):
    client.get(f"/api/v1/analyze/trend?{QUERY}")

    assert client.get(f"/api/v1/analyze/trend?{QUERY}").json()["cached"] is True


@pytest.mark.parametrize(
    "query",
    ["weeks=0", "weeks=27", "until=last%20tuesday", "limit=0", "days=7"],
)
def test_out_of_bounds_or_unknown_params_are_a_422(client, query):
    assert client.get(f"/api/v1/analyze/trend?{query}").status_code == 422
