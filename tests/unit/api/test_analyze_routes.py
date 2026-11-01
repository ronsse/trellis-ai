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
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.analyze._holdout_fixture import (
    BEHAVIOUR_FLAT,
    BEHAVIOUR_VARIANTS,
    REFUSAL_KEYS,
    UNTIL,
    HoldoutLog,
    seed_arm_shift,
)
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


def test_the_holdout_description_names_the_secondary_fits_the_report_carries(client):
    # The report gained covariate-adjusted secondary fits (#1044); a description
    # that stops at the primary verdict leaves a reader of the Reports tab
    # unable to say what the "not measurable" block under it means.
    rows = {r["name"]: r for r in client.get("/api/v1/analyze").json()["reports"]}
    description = " ".join(rows["holdout"]["description"].split())
    assert "covariate-adjusted" in description
    assert "brief length" in description
    assert "agent type" in description
    assert "not decisive" in description
    assert "not measurable" in description


def test_every_report_carries_a_human_title_distinct_from_its_raw_name(client):
    # B15: the Reports tab's picker must not show the raw kebab-case
    # route/CLI name as its primary label -- every entry needs a title,
    # and it must actually be a human label, not a copy of the name.
    resp = client.get("/api/v1/analyze")
    assert resp.status_code == 200
    for row in resp.json()["reports"]:
        assert row["title"], row
        assert row["title"] != row["name"]


def test_list_reports_includes_params_schema_matching_each_params_model(client):
    # A UI client renders a typed input (number with bounds, checkbox,
    # select) from `params_schema` instead of a plain text box per param,
    # so this must be the exact schema the route's own DTO enforces --
    # not a hand-maintained copy that can drift from it.
    resp = client.get("/api/v1/analyze")
    assert resp.status_code == 200
    for row in resp.json()["reports"]:
        entry = analyze._REPORTS[row["name"]]
        assert row["params_schema"] == entry.params_model.model_json_schema()
        assert set(row["params_schema"]["properties"]) == set(row["default_params"])


def test_holdout_outcome_param_schema_is_an_enum_of_known_outcomes():
    # The schema's enum is read from the same OUTCOMES tuple the
    # field_validator checks against, so a client's <select> options can
    # never list an outcome the server would then reject.
    schema = analyze.HoldoutParams.model_json_schema()
    outcome_schema = schema["properties"]["outcome"]
    assert outcome_schema["enum"] == sorted(analyze.OUTCOMES)


def test_holdout_numeric_param_schemas_carry_their_bounds():
    schema = analyze.HoldoutParams.model_json_schema()
    days_schema = schema["properties"]["days"]
    assert days_schema["minimum"] == 1
    assert days_schema["maximum"] == 365
    # planned_mde uses gt=0 (exclusive) rather than ge=0.
    mde_schema = schema["properties"]["planned_mde"]
    assert mde_schema["exclusiveMinimum"] == 0
    assert mde_schema["maximum"] == 100


def test_holdout_boolean_param_schema_is_typed_boolean():
    schema = analyze.HoldoutParams.model_json_schema()
    assert schema["properties"]["itt"]["type"] == "boolean"


def test_optional_param_schema_unwraps_to_a_typed_inner_branch():
    # `until: str | None` is pydantic's Optional shape: an anyOf with a
    # null branch. The UI unwraps this to the non-null branch to pick a
    # control -- pin the shape it depends on.
    schema = analyze.HoldoutParams.model_json_schema()
    until_schema = schema["properties"]["until"]
    assert "anyOf" in until_schema
    non_null = [b for b in until_schema["anyOf"] if b.get("type") != "null"]
    assert non_null
    assert non_null[0]["type"] == "string"


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
            title=analyze._REPORTS["value"].title,
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
            title=analyze._REPORTS["token-usage"].title,
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
            title=analyze._REPORTS["token-usage"].title,
            description=analyze._REPORTS["token-usage"].description,
            params_model=analyze._REPORTS["token-usage"].params_model,
            run=_counting_run,
        ),
    )
    client.get("/api/v1/analyze/token-usage")
    monkeypatch.setattr(analyze, "_CACHE_TTL_SECONDS", -1.0)
    client.get("/api/v1/analyze/token-usage")
    assert calls["n"] == 2


def test_expired_entries_are_evicted_on_write(client, monkeypatch):
    # Distinct params each mint a key; without eviction the cache would
    # keep every key requested for the life of the process.
    client.get("/api/v1/analyze/token-usage?days=1")
    client.get("/api/v1/analyze/token-usage?days=2")
    assert len(analyze._CACHE) == 2
    monkeypatch.setattr(analyze, "_CACHE_TTL_SECONDS", -1.0)
    client.get("/api/v1/analyze/token-usage?days=3")
    assert len(analyze._CACHE) == 1


def test_report_route_is_a_sync_handler():
    # A coroutine handler would run the synchronous store scan on the
    # event loop and stall every concurrent request.
    import inspect

    assert not inspect.iscoroutinefunction(analyze.get_analyze_report)


# ---------------------------------------------------------------------------
# An unsettled holdout read is a refusal the store cannot move.
# ---------------------------------------------------------------------------

_HOLDOUT_FAST = {
    "days": 60,
    "until": UNTIL.isoformat(),
    "rate": 0.2,
    "limit": 20_000,
    "permutations": 299,
    "bootstraps": 100,
    "power_sims": 20,
}
_CLOCK_KEYS = ("generated_at", "as_of")


def _without_clock(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _without_clock(v) for k, v in node.items() if k not in _CLOCK_KEYS}
    if isinstance(node, list):
        return [_without_clock(v) for v in node]
    return node


def _holdout_read(client, tmp_path, name, *, seed, settled, **params):
    registry = StoreRegistry(stores_dir=tmp_path / name)
    try:
        log = HoldoutLog(registry.operational.event_log)
        seed_arm_shift(log, **seed)
        if settled:
            log.sweep()
        else:
            log.sweep(at=UNTIL - timedelta(hours=1))
        app_module._registry = registry
        analyze._CACHE.clear()
        resp = client.get("/api/v1/analyze/holdout", params={**_HOLDOUT_FAST, **params})
        assert resp.status_code == 200
        return _without_clock(resp.json()["data"])
    finally:
        registry.close()


@pytest.mark.parametrize("variant", sorted(BEHAVIOUR_VARIANTS))
@pytest.mark.parametrize(
    ("outcome", "itt"), [("log1p_turns", False), ("commits", True)]
)
def test_an_unsettled_holdout_read_answers_the_same_whatever_the_store_holds(
    client, tmp_path, variant, outcome, itt
):
    """Two stores that differ in what the sessions did answer the same, whole.

    The pairs differ in the withheld arm's values, cut-offs and missing
    outcomes, its pack calls and join times, the timing's tool-call and
    hook-message counts, and an outcome block.
    """
    params = {"outcome": outcome, "itt": itt}
    flat = _holdout_read(
        client, tmp_path, "flat", seed=BEHAVIOUR_FLAT, settled=False, **params
    )
    changed = _holdout_read(
        client,
        tmp_path,
        "changed",
        seed=BEHAVIOUR_VARIANTS[variant],
        settled=False,
        **params,
    )

    assert set(flat) == REFUSAL_KEYS
    assert flat["settled_window"]["settled"] is False
    assert flat == changed


@pytest.mark.parametrize("variant", sorted(set(BEHAVIOUR_VARIANTS) - {"join_time"}))
def test_the_same_pairs_do_differ_when_the_holdout_window_is_settled(
    client, tmp_path, variant
):
    """The comparison above is not vacuous: settled, the same stores differ."""
    params = {"outcome": "commits"}
    flat = _holdout_read(
        client, tmp_path, "flat", seed=BEHAVIOUR_FLAT, settled=True, **params
    )
    changed = _holdout_read(
        client,
        tmp_path,
        "changed",
        seed=BEHAVIOUR_VARIANTS[variant],
        settled=True,
        **params,
    )

    assert flat["settled_window"]["settled"] is True
    assert "funnel" in flat
    assert flat != changed
