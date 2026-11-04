"""``GET /api/v1/analyze/holdout`` behind the readout seal.

``TRELLIS_HOLDOUT_READOUT_AFTER`` keeps the outcome unread until the
pre-registered horizon. The route answers a sealed read with 409, the status
``ConfigError`` and the other typed refusals already get here: a 403 would be
read by the Reports tab as an under-scoped API key, and the tab shows the
refusal text only for a response it does not take for an auth problem. Time is
frozen at the gate's own clock (``holdout_gate._now``). Every id the fixtures
write is synthetic.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.analyze._holdout_fixture import UNTIL, HoldoutLog, seed_arm_shift
from trellis.analyze import holdout_gate
from trellis.analyze.holdout_gate import HOLDOUT_READOUT_AFTER_ENV
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import analyze

HORIZON = datetime(2031, 3, 11, tzinfo=UTC)
BEFORE = HORIZON - timedelta(seconds=1)
URL = "/api/v1/analyze/holdout"
PARAMS = {
    "days": 60,
    "until": UNTIL.isoformat(),
    "rate": 0.2,
    "limit": 20_000,
    "permutations": 99,
    "bootstraps": 50,
    "power_sims": 5,
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(HOLDOUT_READOUT_AFTER_ENV, raising=False)
    analyze._CACHE.clear()
    yield
    analyze._CACHE.clear()


@pytest.fixture
def registry(tmp_path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    log = HoldoutLog(reg.operational.event_log)
    seed_arm_shift(log, shift=2)
    log.sweep()
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
    app_module.register_exception_handlers(app)
    return TestClient(app, raise_server_exceptions=False)


def _freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(holdout_gate, "_now", lambda: moment)


def _seal(monkeypatch: pytest.MonkeyPatch, *, now: datetime) -> None:
    monkeypatch.setenv(HOLDOUT_READOUT_AFTER_ENV, HORIZON.isoformat())
    _freeze(monkeypatch, now)


def _read(client: TestClient, **extra: Any):
    return client.get(URL, params={**PARAMS, **extra})


def _events(registry: StoreRegistry) -> int:
    return len(registry.operational.event_log.get_events(limit=1_000_000))


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"outcome": "commits", "itt": "true"},
        {"rate": 0.5, "settle_hours": 0, "limit": 50},
    ],
    ids=["plain", "outcome-itt", "rate-settle-limit"],
)
def test_a_read_before_the_horizon_is_refused_with_409(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    registry: StoreRegistry,
    extra: dict[str, str],
) -> None:
    _seal(monkeypatch, now=BEFORE)
    events = _events(registry)

    resp = _read(client, **extra)

    assert resp.status_code == 409
    assert resp.status_code != 403
    body = resp.json()
    assert body["code"] == "holdout_readout_sealed"
    assert HORIZON.isoformat() in body["message"]
    assert "sealed" in body["message"]
    assert "analyze holdout --operational" in body["message"]
    assert "data" not in body
    assert set(body) <= {"code", "message", "request_id"}
    assert _events(registry) == events


def test_the_refusal_carries_no_figure(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _seal(monkeypatch, now=BEFORE)

    body = _read(client).json()

    leftover = body["message"].replace(HORIZON.isoformat(), "")
    assert not [c for c in leftover if c.isdigit()], leftover
    for absent in ("mean", "p-value", "estimate", "arm", "tasks"):
        assert absent not in leftover, absent


def test_the_refusal_is_the_same_whatever_the_store_holds(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, tmp_path
) -> None:
    _seal(monkeypatch, now=BEFORE)
    seeded = _read(client)

    empty = StoreRegistry(stores_dir=tmp_path / "empty")
    try:
        app_module._registry = empty
        analyze._CACHE.clear()
        bare = _read(client)
    finally:
        empty.close()

    assert seeded.status_code == bare.status_code == 409
    assert {k: v for k, v in seeded.json().items() if k != "request_id"} == {
        k: v for k, v in bare.json().items() if k != "request_id"
    }


def test_the_report_opens_at_the_horizon(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _seal(monkeypatch, now=HORIZON)

    resp = _read(client)

    assert resp.status_code == 200
    assert resp.json()["data"]["inference"]["status"] == "ok"


def test_the_report_after_the_horizon_is_the_report_with_no_seal(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    unsealed = _read(client)
    assert unsealed.status_code == 200
    analyze._CACHE.clear()
    _seal(monkeypatch, now=HORIZON + timedelta(days=30))

    opened = _read(client)

    assert opened.status_code == 200
    assert opened.json()["data"] == unsealed.json()["data"]


@pytest.mark.parametrize("blank", [None, "", "  "])
def test_an_unset_horizon_is_no_gate(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, blank: str | None
) -> None:
    _freeze(monkeypatch, datetime(2000, 1, 1, tzinfo=UTC))
    if blank is not None:
        monkeypatch.setenv(HOLDOUT_READOUT_AFTER_ENV, blank)

    resp = _read(client)

    assert resp.status_code == 200
    assert "inference" in resp.json()["data"]


def test_a_malformed_horizon_refuses_the_outcome(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv(HOLDOUT_READOUT_AFTER_ENV, "after the holidays")
    _freeze(monkeypatch, datetime(2099, 1, 1, tzinfo=UTC))

    resp = _read(client)

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "config_error"
    assert body["setting"] == HOLDOUT_READOUT_AFTER_ENV
    assert "data" not in body


def test_a_cached_report_is_not_served_once_the_seal_is_up(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """The gate runs before the cache lookup, so a warm entry cannot leak."""
    _seal(monkeypatch, now=HORIZON)
    warm = _read(client)
    assert warm.status_code == 200
    assert _read(client).json()["cached"] is True

    monkeypatch.setenv(HOLDOUT_READOUT_AFTER_ENV, "2999-01-01")
    resp = _read(client)

    assert resp.status_code == 409
    assert "data" not in resp.json()


def test_a_sealed_read_fills_no_cache_entry(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _seal(monkeypatch, now=BEFORE)

    _read(client)

    assert not analyze._CACHE


def test_the_report_list_still_names_holdout_before_the_horizon(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _seal(monkeypatch, now=BEFORE)

    resp = client.get("/api/v1/analyze")

    assert resp.status_code == 200
    rows = {row["name"]: row for row in resp.json()["reports"]}
    assert "holdout" in rows
    assert "sealed" in " ".join(rows["holdout"]["description"].split())


def test_a_report_that_is_not_the_holdout_is_not_sealed(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _seal(monkeypatch, now=BEFORE)
    ungated = [
        name for name, entry in analyze._REPORTS.items() if not entry.readout_gated
    ]
    assert ungated, "the roster must hold a report that is not sealed"

    resp = client.get(f"/api/v1/analyze/{ungated[0]}")

    assert resp.status_code != 409


def test_the_reports_tab_shows_a_409_message_and_reads_only_401_and_403_as_auth() -> (
    None
):
    """The refusal text reaches the person: 409 is no auth problem to the tab.

    ``fetchReportJSON`` turns a non-ok response into the failure text from the
    body's ``message``; ``throwIfAuthRefused`` takes only 401 and 403 for a
    key problem. A 403 here would hide the horizon behind a key banner.
    """
    page = (Path(analyze.__file__).parent.parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    fetcher = page[page.index("async function fetchReportJSON") :]
    fetcher = fetcher[: fetcher.index("async function runReport")]
    assert "j.message" in fetcher
    assert "throwIfAuthRefused(res, key)" in fetcher
    guard = page[page.index("async function throwIfAuthRefused") :]
    guard = guard[: guard.index("\n        }\n")]
    statuses = {int(n) for n in re.findall(r"res\.status === (\d+)", guard)}
    assert statuses == {401, 403}
    assert "Failed to run report" in page
