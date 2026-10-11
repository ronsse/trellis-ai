"""Tests for ``GET /api/v1/analyze`` and ``GET /api/v1/analyze/{report}``.

The route family exists so the question "are our learning loops
performing?" can be asked over HTTP, not just the CLI
(``trellis analyze <report>``). Every report route must call the exact
library function the CLI calls — this file's parity tests pin that down
for three reports, rather than trusting a docstring's claim of it.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.analyze.domains import analyze_domains
from trellis.retrieve.telemetry import analyze_pack_telemetry
from trellis.retrieve.token_usage import analyze_token_usage
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import analyze


@pytest.fixture
def registry(tmp_path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
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


# ---------------------------------------------------------------------------
# GET /api/v1/analyze
# ---------------------------------------------------------------------------


def test_list_reports_names_every_allowlisted_report(client):
    resp = client.get("/api/v1/analyze")
    assert resp.status_code == 200
    names = {row["name"] for row in resp.json()["reports"]}
    assert names == set(analyze._REPORTS)
    for row in resp.json()["reports"]:
        assert row["description"], row
        assert isinstance(row["default_params"], dict)


def test_pack_quality_is_not_in_the_allowlist():
    # pack-quality's only entry point requires a --scenarios file with no
    # built-in default, so there is no bounded query-param shape for it
    # (see the module docstring). This pins the exclusion so it survives
    # a later refactor without anyone noticing it quietly disappeared.
    assert "pack-quality" not in analyze._REPORTS
    assert "replay" not in analyze._REPORTS
    assert "generate-advisories" not in analyze._REPORTS
    assert "apply-noise-tags" not in analyze._REPORTS


# ---------------------------------------------------------------------------
# 200 on an empty store, for every allowlisted report, with finite JSON.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("report", sorted(analyze._REPORTS))
def test_every_report_returns_200_on_empty_store(client, report):
    resp = client.get(f"/api/v1/analyze/{report}")
    assert resp.status_code == 200, resp.text
    assert "Infinity" not in resp.text
    assert "NaN" not in resp.text
    body = resp.json()
    assert body["report"] == report
    assert body["cached"] is False
    assert "computed_at" in body
    assert "data" in body


# ---------------------------------------------------------------------------
# Unknown report -> 404 listing allowed names.
# ---------------------------------------------------------------------------


def test_unknown_report_is_404_with_allowed_names(client):
    resp = client.get("/api/v1/analyze/not-a-real-report")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["report"] == "not-a-real-report"
    assert set(detail["allowed"]) == set(analyze._REPORTS)


# ---------------------------------------------------------------------------
# Bad params -> 422, including an unknown param name (extra="forbid").
# ---------------------------------------------------------------------------


def test_out_of_bound_param_is_422(client):
    resp = client.get("/api/v1/analyze/holdout", params={"days": 0})
    assert resp.status_code == 422


def test_unknown_param_name_is_422(client):
    resp = client.get("/api/v1/analyze/value", params={"not_a_real_param": "x"})
    assert resp.status_code == 422


def test_unknown_outcome_value_is_422(client):
    resp = client.get(
        "/api/v1/analyze/holdout", params={"outcome": "not_a_real_outcome"}
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Parity: route and CLI call the same library function on the same store.
# ---------------------------------------------------------------------------


def test_domains_route_matches_direct_library_call(client, registry):
    resp = client.get("/api/v1/analyze/domains", params={"days": 10, "limit": 500})
    assert resp.status_code == 200
    direct = analyze_domains(
        registry.operational.trace_store,
        registry.knowledge.document_store,
        registry.operational.event_log,
        days=10,
        scan_limit=500,
    ).to_payload()
    assert resp.json()["data"] == direct


def test_pack_telemetry_route_matches_direct_library_call(client, registry):
    resp = client.get(
        "/api/v1/analyze/pack-telemetry", params={"days": 5, "limit": 250}
    )
    assert resp.status_code == 200
    direct = analyze_pack_telemetry(
        registry.operational.event_log, days=5, limit=250
    ).model_dump()
    assert resp.json()["data"] == direct


def test_token_usage_route_matches_direct_library_call(client, registry):
    resp = client.get("/api/v1/analyze/token-usage", params={"days": 14, "limit": 100})
    assert resp.status_code == 200
    direct = analyze_token_usage(
        registry.operational.event_log, days=14, limit=100
    ).model_dump()
    assert resp.json()["data"] == direct


# ---------------------------------------------------------------------------
# Non-finite values never reach the wire.
# ---------------------------------------------------------------------------


def test_finite_sanitizer_replaces_non_finite_floats_with_none():
    poisoned = {
        "a": float("inf"),
        "b": float("-inf"),
        "c": float("nan"),
        "d": 1.5,
        "e": [float("inf"), 2, {"f": float("nan")}],
        "g": None,
        "h": "Infinity",  # a literal string must survive untouched
    }
    cleaned = analyze._finite(poisoned)
    assert cleaned == {
        "a": None,
        "b": None,
        "c": None,
        "d": 1.5,
        "e": [None, 2, {"f": None}],
        "g": None,
        "h": "Infinity",
    }
    # Every value must be JSON-serializable with allow_nan=False.
    raw = json.dumps(cleaned, allow_nan=False)
    assert "Infinity" not in raw.replace('"Infinity"', "")
    assert "NaN" not in raw


def test_infected_report_payload_is_sanitized_end_to_end(client, monkeypatch):
    def _poisoned_run(_params: Any, _registry: Any) -> dict[str, Any]:
        return {"ok": True, "rate": float("nan"), "lift": float("inf")}

    monkeypatch.setitem(
        analyze._REPORTS,
        "value",
        analyze._ReportEntry(
            description=analyze._REPORTS["value"].description,
            params_model=analyze._REPORTS["value"].params_model,
            run=_poisoned_run,
        ),
    )
    resp = client.get("/api/v1/analyze/value")
    assert resp.status_code == 200
    assert "NaN" not in resp.text
    assert "Infinity" not in resp.text
    data = resp.json()["data"]
    assert data["rate"] is None
    assert data["lift"] is None
    assert data["ok"] is True


# ---------------------------------------------------------------------------
# TTL cache: second call within the window is served cached; a cleared
# entry is recomputed.
# ---------------------------------------------------------------------------


def test_second_call_within_ttl_is_served_from_cache(client, monkeypatch):
    calls = {"n": 0}
    real_run = analyze._REPORTS["token-usage"].run

    def _counting_run(params: Any, reg: Any) -> dict[str, Any]:
        calls["n"] += 1
        return real_run(params, reg)

    monkeypatch.setitem(
        analyze._REPORTS,
        "token-usage",
        analyze._ReportEntry(
            description=analyze._REPORTS["token-usage"].description,
            params_model=analyze._REPORTS["token-usage"].params_model,
            run=_counting_run,
        ),
    )
    first = client.get("/api/v1/analyze/token-usage")
    second = client.get("/api/v1/analyze/token-usage")
    assert first.status_code == second.status_code == 200
    assert calls["n"] == 1
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert second.json()["computed_at"] == first.json()["computed_at"]


def test_cache_expiry_recomputes(client, monkeypatch):
    calls = {"n": 0}
    real_run = analyze._REPORTS["token-usage"].run

    def _counting_run(params: Any, reg: Any) -> dict[str, Any]:
        calls["n"] += 1
        return real_run(params, reg)

    monkeypatch.setitem(
        analyze._REPORTS,
        "token-usage",
        analyze._ReportEntry(
            description=analyze._REPORTS["token-usage"].description,
            params_model=analyze._REPORTS["token-usage"].params_model,
            run=_counting_run,
        ),
    )
    client.get("/api/v1/analyze/token-usage")
    monkeypatch.setattr(analyze, "_CACHE_TTL_SECONDS", -1.0)
    client.get("/api/v1/analyze/token-usage")
    assert calls["n"] == 2
