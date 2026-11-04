"""``POST /api/v1/traces`` repairs the two commonest trace-shape mistakes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

_NEEDLE = "needle-do-not-log"
_REPO = "secret-org/secret-repo"


@pytest.fixture
def registry(tmp_path: Path):
    stores = tmp_path / "stores"
    stores.mkdir()
    reg = StoreRegistry(stores_dir=stores)
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


def _body(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source": "agent",
        "intent": "probe",
        "steps": [],
        "context": {},
    }
    base.update(overrides)
    return base


def _coerced(reg: StoreRegistry) -> list:
    return reg.operational.event_log.get_events(
        event_type=EventType.WRITE_COERCED, limit=100
    )


class TestRestRepairs:
    def test_a_string_result_is_stored_as_a_summary_object(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        body = _body(steps=[{"step_type": "t", "name": "a", "result": _NEEDLE}])

        resp = client.post("/api/v1/traces", json=body)

        assert resp.status_code == 200, resp.text
        stored = registry.operational.trace_store.get(resp.json()["trace_id"])
        assert stored is not None
        assert stored.steps[0].result == {"summary": _NEEDLE}

    def test_context_repo_is_stored_in_top_level_metadata(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post("/api/v1/traces", json=_body(context={"repo": _REPO}))

        assert resp.status_code == 200, resp.text
        stored = registry.operational.trace_store.get(resp.json()["trace_id"])
        assert stored is not None
        assert stored.metadata["repo"] == _REPO
        assert "repo" not in stored.context.model_dump()

    def test_one_event_names_the_surface_and_carries_no_content(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        body = _body(
            context={"repo": _REPO},
            steps=[{"step_type": "t", "name": "a", "result": _NEEDLE}],
        )

        resp = client.post("/api/v1/traces", json=body)

        (event,) = _coerced(registry)
        assert event.source == "api:ingest-trace"
        assert event.payload == {
            "tool": "api:ingest-trace",
            "repairs": [
                {"path": "steps[0].result", "rule": "str_to_summary"},
                {"path": "context.repo", "rule": "moved_to_metadata"},
            ],
            "trace_id": resp.json()["trace_id"],
        }
        raw = json.dumps(event.payload)
        assert _NEEDLE not in raw
        assert _REPO not in raw

    def test_the_response_shape_is_unchanged(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        clean = client.post("/api/v1/traces", json=_body()).json()
        repaired = client.post(
            "/api/v1/traces", json=_body(context={"repo": _REPO})
        ).json()

        assert set(clean) == set(repaired)

    def test_a_clean_trace_emits_nothing(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post("/api/v1/traces", json=_body())

        assert resp.status_code == 200
        assert _coerced(registry) == []

    @pytest.mark.parametrize(
        "body",
        [
            _body(steps=[{"step_type": "t", "name": "a", "result": 7}]),
            _body(context={"repo": "a/b"}, metadata={"repo": "c/d"}),
            _body(context={"repo": "a/b", "colour": "blue"}),
        ],
        ids=["non-string-result", "conflicting-metadata-repo", "mixed-unrepairable"],
    )
    def test_what_is_not_repairable_still_rejects_and_emits_no_coercion(
        self, client: TestClient, registry: StoreRegistry, body: dict[str, object]
    ) -> None:
        resp = client.post("/api/v1/traces", json=body)

        assert resp.status_code == 422
        assert _coerced(registry) == []
        assert registry.operational.trace_store.count() == 0

    def test_the_mixed_rejection_names_only_the_unrepairable_error(
        self, client: TestClient
    ) -> None:
        body = _body(
            context={"repo": "a/b", "colour": "blue"},
            steps=[{"step_type": "t", "name": "a", "result": "done"}],
        )

        resp = client.post("/api/v1/traces", json=body)

        text = resp.text
        assert resp.status_code == 422
        assert "colour" in text
        assert "result" not in text
