"""REST document doors refuse a ``trace-summary:`` id (#1054).

``POST /evidence``, ``POST /documents`` and ``POST /commands/batch`` (an
``evidence.ingest`` command) all end in ``EvidenceIngestHandler``, which refuses
a caller-chosen id under the trace embed worker's prefix. Each answers its usual
refused-write error with nothing stored; ``POST /vectors`` has its own check
(``test_sensitivity_rest_writes.py``).

Defective subject: drop the prefix check in ``_evidence_document_id`` and each
refusal below answers 200 with the row written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

RESERVED_ID = "trace-summary:trace-0001"
CONTROL_IDS = ["trace-summary", "Trace-Summary:x", "my-trace-summary:x"]
WORKER_CLAIM = "worker:embed-traces"


@pytest.fixture
def registry(tmp_path: Path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_AUTH_MODE", raising=False)


def _evidence(doc_id: str) -> dict[str, Any]:
    return {
        "evidence_id": doc_id,
        "evidence_type": "snippet",
        "source_origin": "test",
        "content": "a decoy a caller filed under a summary id",
    }


def _document(doc_id: str) -> dict[str, Any]:
    return {"doc_id": doc_id, "content": "a decoy a caller filed under a summary id"}


def _batch(doc_id: str, **top: Any) -> dict[str, Any]:
    return {
        "commands": [
            {
                "operation": "evidence.ingest",
                "args": {
                    "evidence": {
                        "doc_id": doc_id,
                        "content": "a decoy a caller filed under a summary id",
                    }
                },
                "target_id": doc_id,
            }
        ],
        **top,
    }


def _assert_nothing_stored(registry: StoreRegistry, doc_id: str) -> None:
    assert registry.knowledge.document_store.get(doc_id) is None
    assert registry.knowledge.vector_store.get(doc_id) is None


def _batch_result(resp: Any) -> dict[str, Any]:
    assert resp.status_code == 200, resp.text
    return resp.json()["results"][0]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/evidence", _evidence(RESERVED_ID)),
        ("/api/v1/documents", _document(RESERVED_ID)),
    ],
    ids=["evidence", "documents"],
)
def test_a_document_route_refuses_a_summary_id(
    client: TestClient, registry: StoreRegistry, path: str, body: dict[str, Any]
) -> None:
    resp = client.post(path, json=body)

    assert resp.status_code == 400, resp.text
    assert "trace embed worker" in resp.text
    _assert_nothing_stored(registry, RESERVED_ID)


def test_commands_batch_refuses_a_summary_id(
    client: TestClient, registry: StoreRegistry
) -> None:
    result = _batch_result(
        client.post("/api/v1/commands/batch", json=_batch(RESERVED_ID))
    )

    assert result["status"] in ("rejected", "failed")
    assert "trace embed worker" in result["message"]
    _assert_nothing_stored(registry, RESERVED_ID)


def test_commands_batch_with_a_forged_worker_claim_is_still_refused(
    client: TestClient, registry: StoreRegistry
) -> None:
    """With auth off the body's ``requested_by`` is taken as the command's own."""
    result = _batch_result(
        client.post(
            "/api/v1/commands/batch",
            json=_batch(RESERVED_ID, requested_by=WORKER_CLAIM),
        )
    )

    assert result["status"] in ("rejected", "failed")
    _assert_nothing_stored(registry, RESERVED_ID)


@pytest.mark.parametrize("doc_id", CONTROL_IDS)
def test_an_id_that_only_resembles_the_prefix_is_written_on_every_route(
    client: TestClient, registry: StoreRegistry, doc_id: str
) -> None:
    assert client.post("/api/v1/evidence", json=_evidence(doc_id)).status_code == 200
    assert registry.knowledge.document_store.get(doc_id) is not None

    other = f"{doc_id}-b"
    assert client.post("/api/v1/documents", json=_document(other)).status_code == 200
    assert registry.knowledge.document_store.get(other) is not None

    third = f"{doc_id}-c"
    assert (
        _batch_result(client.post("/api/v1/commands/batch", json=_batch(third)))[
            "status"
        ]
        == "success"
    )
    assert registry.knowledge.document_store.get(third) is not None
