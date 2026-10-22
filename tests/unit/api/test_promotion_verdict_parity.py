"""One promotion run, one verdict: the CLI and the REST route cannot disagree.

``trellis curate promote-learning`` and ``POST /api/v1/learning/promotions``
both read the executor's outcomes through
:func:`trellis.learning.scoring.promotion_verdict`. Each scenario here scripts
what ``submit_learning_promotion`` answers, feeds the same candidates and
decisions to both surfaces, and checks that the status, the promoted count and
every row's status match, and that both equal the shared function's answer over
the scripted outcomes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.structlog_isolation import IsolatedCliRunner
from trellis.learning.scoring import (
    precedent_entity_id_for_candidate,
    promotion_landed,
    promotion_verdict,
)
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import admin
from trellis_cli import curate as curate_cli
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

_OK = "success"

LANDED: dict[str, Any] = {
    "status": "promoted",
    "node_id": "node-1",
    "edges": [{"edge_kind": "precedent_applies_to", "target_id": "t", "status": _OK}],
    "precedent_event_status": _OK,
}
EDGE_REJECTED: dict[str, Any] = {
    **LANDED,
    "edges": [
        {"edge_kind": "precedent_applies_to", "target_id": "t", "status": "rejected"}
    ],
}
EVENT_FAILED: dict[str, Any] = {**LANDED, "precedent_event_status": "failed"}
ENTITY_FAILED: dict[str, Any] = {"status": "entity_failed", "message": "boom"}
REFUSED: dict[str, Any] = {"status": "refused_demoted", "message": "demoted"}

#: Planner answers for approved rows that never reach the executor.
GHOST = "ghost"  # names no candidate -> ``missing_candidate``
NOISE = "noise"  # not a promotion -> ``skipped_non_promotable``
BLANK = ""  # an approved row naming nothing: never an outcome
_PLANNER_ROWS = {
    GHOST: {"status": "missing_candidate"},
    NOISE: {"status": "skipped_non_promotable"},
}

# scenario -> the id each approved decision names, in order. A promotable id
# ``c-N`` is answered with the Nth scripted outcome; GHOST and NOISE never
# reach the executor.
_SCENARIOS: dict[str, list[Any]] = {
    "no_rows": [],
    "landed": [LANDED],
    "two_landed": [LANDED, LANDED],
    "refused": [REFUSED],
    "landed_then_refused": [LANDED, REFUSED],
    "ghost": [GHOST],
    "non_promotable": [NOISE],
    "edge_rejected": [EDGE_REJECTED],
    "event_failed": [EVENT_FAILED],
    "entity_failed": [ENTITY_FAILED],
    "refused_then_failed": [REFUSED, ENTITY_FAILED],
    "failed_then_refused": [ENTITY_FAILED, REFUSED],
    "failed_ghost_and_landed": [LANDED, GHOST, EDGE_REJECTED],
    "refused_ghost_and_landed": [LANDED, GHOST, REFUSED],
    "blank_id_is_no_outcome": [BLANK, LANDED],
    "blank_id_alone": [BLANK],
}


def _candidate(candidate_id: str, recommendation_type: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "intent_family": "fam",
        "recommendation_type": recommendation_type,
        "item_id": f"doc:{candidate_id}",
        "item_type": "document",
        "title": f"title {candidate_id}",
        "precedent_name": f"Learning: {candidate_id}",
        "precedent_properties": {"source_of_truth": "reviewed_promotion"},
        "target_entity_ids": [],
        "supporting_run_ids": ["run-1"],
        "phases": [],
        "domain_systems": [],
    }


def _plan(steps: list[Any]) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """``(candidates, decided ids, scripted submit answers by entity id)``."""
    candidates = [_candidate(NOISE, "investigate_noise")]
    ids: list[str] = []
    scripted: dict[str, Any] = {}
    for n, step in enumerate(steps):
        if isinstance(step, str):
            ids.append(step)
            continue
        cid = f"c-{n}"
        candidates.append(_candidate(cid, "promote_guidance"))
        scripted[precedent_entity_id_for_candidate(cid)] = step
        ids.append(cid)
    return candidates, ids, scripted


def _expected_outcomes(steps: list[Any]) -> list[dict[str, Any]]:
    """What the verdict reads: scripted answers plus the planner's own rows."""
    return [_PLANNER_ROWS[s] if isinstance(s, str) else s for s in steps if s != BLANK]


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for var in ("TRELLIS_API_KEY", "TRELLIS_AUTH_MODE"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "cli" / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "cli"))
    monkeypatch.setenv("TRELLIS_LEARNING_ARTIFACTS_DIR", str(tmp_path / "learning"))
    # The CLI caches its registry per process; each test has its own stores.
    _reset_registry()
    yield
    _reset_registry()


@pytest.fixture
def rest_client(tmp_path: Path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg

    @asynccontextmanager
    async def noop_lifespan(_app):
        yield

    api = FastAPI(lifespan=noop_lifespan)
    api.include_router(admin.router, prefix="/api/v1")
    yield TestClient(api)
    reg.close()
    app_module._registry = None


def _stub(scripted: dict[str, Any]):
    def submit(_executor, entity_payload, _edges, **_kwargs):
        # A copy: ``settle_promotion`` relabels in place.
        return dict(scripted[entity_payload["entity_id"]])

    return submit


def _run_both(
    steps: list[Any],
    tmp_path: Path,
    rest_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    cli_runner: IsolatedCliRunner,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates, ids, scripted = _plan(steps)
    monkeypatch.setattr(curate_cli, "submit_learning_promotion", _stub(scripted))
    monkeypatch.setattr(admin, "submit_learning_promotion", _stub(scripted))
    decisions = [
        {"candidate_id": cid, "approved": True, "rationale": "r"} for cid in ids
    ]

    candidates_path = tmp_path / "candidates.json"
    decisions_path = tmp_path / "decisions.json"
    candidates_path.write_text(json.dumps({"candidates": candidates}), encoding="utf-8")
    decisions_path.write_text(json.dumps({"decisions": decisions}), encoding="utf-8")
    cli = cli_runner.invoke(
        app,
        [
            "curate",
            "promote-learning",
            "--candidates",
            str(candidates_path),
            "--decisions",
            str(decisions_path),
            "--format",
            "json",
        ],
    )
    cli_payload = json.loads(cli.stdout.strip())

    artifacts = tmp_path / "learning"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "intent_learning_candidates.json").write_text(
        json.dumps({"candidates": candidates}), encoding="utf-8"
    )
    resp = rest_client.post(
        "/api/v1/learning/promotions", json={"decisions": decisions}
    )
    assert resp.status_code == 200
    return cli_payload, resp.json()


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_the_cli_and_the_rest_route_state_the_same_verdict(
    scenario: str,
    tmp_path: Path,
    rest_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    cli_runner: IsolatedCliRunner,
) -> None:
    steps = _SCENARIOS[scenario]
    cli, rest = _run_both(steps, tmp_path, rest_client, monkeypatch, cli_runner)

    shared = promotion_verdict(_expected_outcomes(steps))
    assert cli["status"] == rest["status"] == shared
    assert cli["promoted_count"] == rest["promoted_count"]
    assert cli["approved_count"] == rest["approved_count"]
    assert cli["named_count"] == rest["named_count"]
    # Approved rows are what was asked; named ones are what ran.
    assert rest["approved_count"] == len(steps)
    assert rest["named_count"] == len(_expected_outcomes(steps))
    assert [(r["candidate_id"], r["status"]) for r in cli["results"]] == [
        (r["candidate_id"], r["status"]) for r in rest["results"]
    ]
    # ``promoted_count`` is what landed, by the shared reading.
    assert rest["promoted_count"] == sum(
        1 for o in _expected_outcomes(steps) if promotion_landed(o)
    )


def test_the_scenarios_reach_every_verdict() -> None:
    verdicts = {promotion_verdict(_expected_outcomes(s)) for s in _SCENARIOS.values()}
    assert verdicts == {"ok", "refused", "failed"}
