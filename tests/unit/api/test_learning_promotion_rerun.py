"""``POST /learning/promotions`` re-submitted: nothing is written twice (#983).

The Review queue's Approve button and an API client both post decisions that
may already have landed. A row whose precedent is already current, complete and
unchanged answers ``already_promoted``, adds no SCD-2 version and no second
``PRECEDENT_PROMOTED`` event, and is counted apart from ``promoted_count``.
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
from trellis.learning.scoring import precedent_entity_id_for_candidate
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import admin


def _candidate(candidate_id: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "intent_family": "fam",
        "recommendation_type": "promote_guidance",
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


@pytest.fixture
def registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[StoreRegistry]:
    for var in ("TRELLIS_API_KEY", "TRELLIS_AUTH_MODE"):
        monkeypatch.delenv(var, raising=False)
    artifacts = tmp_path / "learning"
    artifacts.mkdir()
    (artifacts / "intent_learning_candidates.json").write_text(
        json.dumps({"candidates": [_candidate("landed")]}), encoding="utf-8"
    )
    monkeypatch.setenv("TRELLIS_LEARNING_ARTIFACTS_DIR", str(artifacts))
    (tmp_path / "stores").mkdir()
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    @asynccontextmanager
    async def noop_lifespan(_app):
        yield

    api = FastAPI(lifespan=noop_lifespan)
    api.include_router(admin.router, prefix="/api/v1")
    return TestClient(api)


def _post(client: TestClient, rationale: str = "r") -> dict[str, Any]:
    resp = client.post(
        "/api/v1/learning/promotions",
        json={
            "decisions": [
                {"candidate_id": "landed", "approved": True, "rationale": rationale}
            ]
        },
    )
    assert resp.status_code == 200
    return resp.json()


def _writes(registry: StoreRegistry) -> tuple[int, int]:
    entity_id = precedent_entity_id_for_candidate("landed")
    return (
        len(registry.knowledge.graph_store.get_node_history(entity_id)),
        len(
            registry.operational.event_log.get_events(
                event_type=EventType.PRECEDENT_PROMOTED, entity_id=entity_id
            )
        ),
    )


def test_a_resubmitted_decision_is_already_promoted_and_writes_nothing(
    client: TestClient, registry: StoreRegistry
) -> None:
    first = _post(client)
    assert first["status"] == "ok"
    assert [r["status"] for r in first["results"]] == ["promoted"]
    assert (first["promoted_count"], first["already_promoted_count"]) == (1, 0)
    assert _writes(registry) == (1, 1)

    second = _post(client)

    assert second["status"] == "ok"
    row = second["results"][0]
    assert row["status"] == "already_promoted"
    assert row["entity_id"] == precedent_entity_id_for_candidate("landed")
    assert row["node_id"] is None
    assert "nothing was written" in row["message"]
    assert (second["promoted_count"], second["already_promoted_count"]) == (0, 1)
    assert _writes(registry) == (1, 1)


def test_a_resubmission_with_a_new_rationale_is_still_written(
    client: TestClient, registry: StoreRegistry
) -> None:
    _post(client, rationale="first")

    second = _post(client, rationale="second")

    assert [r["status"] for r in second["results"]] == ["promoted"]
    assert second["already_promoted_count"] == 0
    assert _writes(registry) == (2, 2)
