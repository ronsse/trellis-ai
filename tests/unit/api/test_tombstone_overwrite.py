"""REST surfaces and a redaction tombstone (#1034).

``POST /documents`` and the generic command route carry no way to say "I mean
to bring a redacted id back": both are refused through their existing error
paths, with nothing written.

TODO(#1029 part A): ``POST /evidence`` gains a ``reingest_redacted`` body field
once that route lands; its test belongs here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.redaction_tombstone import ORIGINAL_TEXT, make_tombstone
from trellis.core.document_write import REINGEST_STAMP_KEY, is_redaction_tombstone
from trellis.core.vector_metadata import resolve_vector_store
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

DOC_ID = "doc-gone"
NEW_TEXT = "Quartz harbor rewritten text"


@pytest.fixture
def registry(tmp_path: Path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    make_tombstone(reg.knowledge.document_store, resolve_vector_store(reg), DOC_ID)
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


def _batch(evidence: dict) -> dict:
    return {
        "commands": [
            {
                "operation": "evidence.ingest",
                "args": {"evidence": evidence},
                "target_id": DOC_ID,
            }
        ]
    }


def test_post_documents_over_a_tombstone_is_a_400_and_the_row_stays_blank(
    client: TestClient, registry: StoreRegistry
) -> None:
    """Defective subject: drop the tombstone check in ``put_document`` and the
    route answers 200 with the redacted id written back."""
    resp = client.post(
        "/api/v1/documents", json={"doc_id": DOC_ID, "content": NEW_TEXT}
    )

    assert resp.status_code == 400, resp.text
    assert "--reingest-redacted" in resp.text
    assert ORIGINAL_TEXT not in resp.text
    assert is_redaction_tombstone(registry.knowledge.document_store.get(DOC_ID))


def test_commands_batch_without_the_flag_is_rejected_and_nothing_is_written(
    client: TestClient, registry: StoreRegistry
) -> None:
    resp = client.post(
        "/api/v1/commands/batch",
        json=_batch({"doc_id": DOC_ID, "content": NEW_TEXT}),
    )

    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["status"] == "rejected"
    assert "--reingest-redacted" in result["message"]
    assert is_redaction_tombstone(registry.knowledge.document_store.get(DOC_ID))


def test_commands_batch_with_the_flag_writes_and_stamps(
    client: TestClient, registry: StoreRegistry
) -> None:
    resp = client.post(
        "/api/v1/commands/batch",
        json=_batch({"doc_id": DOC_ID, "content": NEW_TEXT, "reingest_redacted": True}),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["status"] == "success", resp.text
    row = registry.knowledge.document_store.get(DOC_ID)
    assert row["content"] == NEW_TEXT
    assert REINGEST_STAMP_KEY in row["metadata"]
