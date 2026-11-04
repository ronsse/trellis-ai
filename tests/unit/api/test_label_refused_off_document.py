"""REST doors refuse a sensitivity label on an id that is not a live document (#1131).

``readable_ids`` and every read gate judge an id by the document row at that id,
and an id with no row has no verdict. A label is therefore a property of a
document alone, and a write that would put one anywhere else (a graph node, a
trace, a deleted document) is refused instead of being stored where no read
path looks. Both label verbs are checked on the two routes that reach them and
on the batch door.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.off_document_ids import (
    LIVE,
    OFF_DOCUMENT_KINDS,
    VERBS,
    assert_nothing_written,
    rejections,
    seed_ids,
    stored_label,
)
from trellis.auth import generate_api_key
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[StoreRegistry]:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    return TestClient(create_app())


def _headers(registry: StoreRegistry, scopes: list[str]) -> dict[str, str]:
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("scopes", [["mutate"], ["mutate", "restricted"], ["admin"]])
@pytest.mark.parametrize("kind", OFF_DOCUMENT_KINDS)
def test_set_sensitivity_route_refuses_an_id_that_is_not_a_document(
    client: TestClient, registry: StoreRegistry, kind: str, scopes: list[str]
) -> None:
    ids = seed_ids(registry)
    resp = client.post(
        f"/api/v1/documents/{ids[kind]}/sensitivity",
        json={"sensitivity": "internal"},
        headers=_headers(registry, scopes),
    )
    assert resp.status_code == 400, resp.text
    assert_nothing_written(registry, ids)


@pytest.mark.parametrize("kind", OFF_DOCUMENT_KINDS)
def test_regulatory_tags_route_refuses_an_id_that_is_not_a_document(
    client: TestClient, registry: StoreRegistry, kind: str
) -> None:
    ids = seed_ids(registry)
    resp = client.post(
        f"/api/v1/documents/{ids[kind]}/regulatory-tags",
        json={"tags": ["pii"]},
        headers=_headers(registry, ["mutate", "restricted"]),
    )
    assert resp.status_code == 400, resp.text
    assert_nothing_written(registry, ids)


@pytest.mark.parametrize("operation", sorted(VERBS))
@pytest.mark.parametrize("kind", OFF_DOCUMENT_KINDS)
def test_batch_door_rejects_an_id_that_is_not_a_document_and_audits_it(
    client: TestClient, registry: StoreRegistry, kind: str, operation: str
) -> None:
    ids = seed_ids(registry)
    resp = client.post(
        "/api/v1/commands/batch",
        json={
            "commands": [
                {
                    "operation": operation,
                    "args": {"doc_id": ids[kind], **VERBS[operation]},
                }
            ],
            "strategy": "sequential",
        },
        headers=_headers(registry, ["mutate", "restricted"]),
    )
    assert resp.status_code == 200, resp.text
    [result] = resp.json()["results"]
    assert result["status"] == "rejected", result
    assert_nothing_written(registry, ids)
    audited = rejections(registry)
    assert [e.payload.get("reason") for e in audited] == ["target_not_found"], [
        e.payload for e in audited
    ]


def test_a_live_document_still_takes_both_label_verbs(
    client: TestClient, registry: StoreRegistry
) -> None:
    seed_ids(registry)
    headers = _headers(registry, ["mutate", "restricted"])
    resp = client.post(
        f"/api/v1/documents/{LIVE}/sensitivity",
        json={"sensitivity": "confidential"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert stored_label(registry, LIVE) == "confidential"
    resp = client.post(
        f"/api/v1/documents/{LIVE}/regulatory-tags",
        json={"tags": ["pii"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
