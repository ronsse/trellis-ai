"""REST surfaces and a redaction tombstone (#1034).

``POST /evidence`` and ``POST /documents`` take an optional boolean
``reingest_redacted`` ("I mean to bring a redacted id back"); without it a write
over a tombstone is refused through the route's existing error path with nothing
written, and the refusal names the field. The generic command route carries the
same flag as ``evidence.reingest_redacted``. The flag is no way past the
clearance rules: a labelled tombstone, or a tombstoned chunk of a labelled
parent, is still refused to a key that may not read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.redaction_tombstone import ORIGINAL_TEXT, make_tombstone, tombstone_metadata
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.core.document_write import (
    REINGEST_STAMP_KEY,
    is_redaction_tombstone,
    put_document,
)
from trellis.core.vector_metadata import resolve_vector_store
from trellis.ingest_corpus.models import chunk_doc_id
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


@dataclass(frozen=True)
class Route:
    """One REST write route that takes ``reingest_redacted``."""

    name: str
    path: str
    id_field: str
    scopes: tuple[str, ...]
    extra: dict[str, Any]

    def body(self, doc_id: str = DOC_ID, **fields: Any) -> dict[str, Any]:
        return {self.id_field: doc_id, "content": NEW_TEXT, **self.extra, **fields}


ROUTES = [
    Route(
        "evidence",
        "/api/v1/evidence",
        "evidence_id",
        ("read", "ingest", "mutate"),
        {"evidence_type": "snippet", "source_origin": "test"},
    ),
    Route("documents", "/api/v1/documents", "doc_id", ("mutate",), {}),
]
BY_ROUTE = pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.name)


@BY_ROUTE
def test_an_unflagged_write_over_a_tombstone_is_a_400_naming_the_field(
    client: TestClient, registry: StoreRegistry, route: Route
) -> None:
    """Defective subject: drop the tombstone check in ``put_document`` and the
    route answers 200 with the redacted id written back."""
    resp = client.post(route.path, json=route.body())

    assert resp.status_code == 400, resp.text
    assert "--reingest-redacted" in resp.text
    assert "`reingest_redacted: true` in the body of POST /api/v1/evidence" in resp.text
    assert "POST /api/v1/documents" in resp.text
    assert "mutate scope" in resp.text
    assert ORIGINAL_TEXT not in resp.text
    assert is_redaction_tombstone(registry.knowledge.document_store.get(DOC_ID))


@BY_ROUTE
def test_a_flagged_write_over_a_tombstone_writes_and_stamps(
    client: TestClient, registry: StoreRegistry, route: Route
) -> None:
    """Defective subject: drop the flag before the command is built and the
    route answers 400, as it did before the field existed."""
    before = datetime.now(UTC)
    resp = client.post(route.path, json=route.body(reingest_redacted=True))

    assert resp.status_code == 200, resp.text
    row = registry.knowledge.document_store.get(DOC_ID)
    assert row["content"] == NEW_TEXT
    assert not is_redaction_tombstone(row)
    # The stamp the generic command route's flagged path writes: an ISO time.
    stamp = datetime.fromisoformat(row["metadata"][REINGEST_STAMP_KEY])
    assert before <= stamp <= datetime.now(UTC)


@BY_ROUTE
@pytest.mark.parametrize("value", ["true", "yes", 1, 0, None, [True]], ids=repr)
def test_a_flag_that_is_not_a_boolean_is_a_422_and_nothing_is_written(
    client: TestClient, registry: StoreRegistry, route: Route, value: object
) -> None:
    """Defective subject: drop the type check and a truthy value reaches the
    handler (400) while a falsy one reads as no flag (also 400)."""
    over_tombstone = client.post(route.path, json=route.body(reingest_redacted=value))
    fresh = client.post(
        route.path, json=route.body("doc-fresh", reingest_redacted=value)
    )

    for resp in (over_tombstone, fresh):
        assert resp.status_code == 422, resp.text
        assert "reingest_redacted must be a boolean" in resp.text
    store = registry.knowledge.document_store
    assert is_redaction_tombstone(store.get(DOC_ID))
    assert store.get("doc-fresh") is None


@BY_ROUTE
def test_the_flag_over_a_live_document_is_an_ordinary_write(
    client: TestClient, registry: StoreRegistry, route: Route
) -> None:
    """Setting it on a row that was never redacted changes nothing: no stamp."""
    store = registry.knowledge.document_store
    for doc_id, flagged in (("doc-live-a", True), ("doc-live-b", False)):
        store.put(doc_id, "Walnut meadow earlier text", {})
        fields = {"reingest_redacted": True} if flagged else {}
        resp = client.post(route.path, json=route.body(doc_id, **fields))
        assert resp.status_code == 200, resp.text
        row = store.get(doc_id)
        assert row["content"] == NEW_TEXT
        assert REINGEST_STAMP_KEY not in row["metadata"]

    fresh = client.post(
        route.path, json=route.body("doc-fresh", reingest_redacted=True)
    )
    assert fresh.status_code == 200, fresh.text
    assert REINGEST_STAMP_KEY not in store.get("doc-fresh")["metadata"]


@BY_ROUTE
def test_a_false_flag_is_the_unflagged_write(
    client: TestClient, registry: StoreRegistry, route: Route
) -> None:
    resp = client.post(route.path, json=route.body(reingest_redacted=False))

    assert resp.status_code == 400, resp.text
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


@pytest.mark.parametrize("value", [None, 0, "", "yes", 1, [True]], ids=repr)
def test_commands_batch_refuses_any_set_non_boolean_flag(
    client: TestClient, registry: StoreRegistry, value: object
) -> None:
    """The two REST routes answer these 422; this door carries the value to the
    handler, which refuses it, so ``null``, ``0`` and ``""`` do not read as an
    absent flag (#1049). Defective subject: carry the flag only when truthy and
    the first three are refused as an unflagged write instead, with the other
    message."""
    resp = client.post(
        "/api/v1/commands/batch",
        json=_batch(
            {"doc_id": DOC_ID, "content": NEW_TEXT, "reingest_redacted": value}
        ),
    )

    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["status"] == "rejected"
    assert "evidence.reingest_redacted must be a boolean" in result["message"]
    assert is_redaction_tombstone(registry.knowledge.document_store.get(DOC_ID))


def test_commands_batch_treats_a_false_flag_as_the_unflagged_write(
    client: TestClient, registry: StoreRegistry
) -> None:
    resp = client.post(
        "/api/v1/commands/batch",
        json=_batch(
            {"doc_id": DOC_ID, "content": NEW_TEXT, "reingest_redacted": False}
        ),
    )

    result = resp.json()["results"][0]
    assert result["status"] == "rejected"
    assert "--reingest-redacted" in result["message"]
    assert is_redaction_tombstone(registry.knowledge.document_store.get(DOC_ID))


# ── the flag is no way past the clearance rules ─────────────────────────

LABELLED = "doc-gone-labelled"
PARENT = "doc-parent-labelled"
CHUNK = chunk_doc_id(PARENT, 0)


@pytest.fixture
def keyed(client: TestClient, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch):
    """The client with auth required and tombstones a key may not be cleared for.

    A redaction blanks every metadata key but a class label, so a real
    tombstone carries no sensitivity label of its own. Two shapes still sit
    above a key's clearance: a tombstone a label was stamped on after the
    fact, and a tombstoned chunk whose parent is labelled (a chunk is judged
    by the higher of its own label and its parent's).
    """
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    store = registry.knowledge.document_store
    vectors = resolve_vector_store(registry)
    put_document(
        store,
        vectors,
        LABELLED,
        "",
        {**tombstone_metadata(), **label("restricted")},
        preserve_updated_at=False,
    )
    store.put(PARENT, "Parent text", label("restricted"))
    make_tombstone(store, vectors, CHUNK)
    assert is_redaction_tombstone(store.get(LABELLED))
    assert is_redaction_tombstone(store.get(CHUNK))
    return TestClient(create_app())


def _headers(registry: StoreRegistry, scopes: list[str]) -> dict[str, str]:
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


@BY_ROUTE
@pytest.mark.parametrize("doc_id", [LABELLED, CHUNK], ids=["labelled", "chunk"])
def test_a_flagged_write_by_a_key_below_the_label_is_still_refused(
    keyed: TestClient, registry: StoreRegistry, route: Route, doc_id: str
) -> None:
    """The route's overwrite check answers first (409). Skip it for a flagged
    write and the handler's own clearance check still refuses (400), so this
    pins the route's answer, not the only barrier."""
    store = registry.knowledge.document_store
    held = store.get(doc_id)

    resp = keyed.post(
        route.path,
        json=route.body(doc_id, reingest_redacted=True),
        headers=_headers(registry, list(route.scopes)),
    )

    assert resp.status_code == 409, resp.text
    assert store.get(doc_id) == held
    assert is_redaction_tombstone(store.get(doc_id))


@pytest.mark.parametrize(
    ("scopes", "status"),
    [(["read", "ingest"], 403), (["read", "ingest", "mutate"], 200)],
    ids=["ingest-only", "ingest-and-mutate"],
)
def test_the_flag_on_post_evidence_needs_the_mutate_scope(
    keyed: TestClient, registry: StoreRegistry, scopes: list[str], status: int
) -> None:
    """Undoing a redaction is not an ingest: an ``ingest``-only key, which
    capture hooks and sync jobs hold, may not set the flag. Defective subject:
    drop the scope check and the ingest-only key rewrites the tombstone and
    mints the stamp promotion trusts."""
    store = registry.knowledge.document_store
    evidence = ROUTES[0]

    resp = keyed.post(
        evidence.path,
        json=evidence.body(reingest_redacted=True),
        headers=_headers(registry, scopes),
    )

    assert resp.status_code == status, resp.text
    row = store.get(DOC_ID)
    if status == 403:
        assert "mutate scope" in resp.text
        assert is_redaction_tombstone(row)
    else:
        assert row["content"] == NEW_TEXT
        assert REINGEST_STAMP_KEY in row["metadata"]


def test_an_ingest_only_key_without_the_flag_is_the_tombstone_refusal(
    keyed: TestClient, registry: StoreRegistry
) -> None:
    """The scope check is on the flag alone: unflagged, the key gets the 400."""
    evidence = ROUTES[0]
    resp = keyed.post(
        evidence.path, json=evidence.body(), headers=_headers(registry, ["ingest"])
    )

    assert resp.status_code == 400, resp.text
    assert is_redaction_tombstone(registry.knowledge.document_store.get(DOC_ID))


@BY_ROUTE
def test_a_cleared_key_may_not_lower_a_labelled_tombstones_label(
    keyed: TestClient, registry: StoreRegistry, route: Route
) -> None:
    """The sensitivity rule holds for a flagged write: it carries no label here."""
    store = registry.knowledge.document_store
    held = store.get(LABELLED)

    resp = keyed.post(
        route.path,
        json=route.body(LABELLED, reingest_redacted=True),
        headers=_headers(registry, ["admin"]),
    )

    assert resp.status_code == 409, resp.text
    assert store.get(LABELLED) == held


@BY_ROUTE
def test_a_cleared_key_re_ingests_a_labelled_tombstone_at_its_label(
    keyed: TestClient, registry: StoreRegistry, route: Route
) -> None:
    resp = keyed.post(
        route.path,
        json=route.body(LABELLED, reingest_redacted=True, sensitivity="restricted"),
        headers=_headers(registry, ["admin"]),
    )

    assert resp.status_code == 200, resp.text
    row = registry.knowledge.document_store.get(LABELLED)
    assert row["content"] == NEW_TEXT
    assert row["metadata"]["data_classification"]["sensitivity"] == "restricted"
    assert REINGEST_STAMP_KEY in row["metadata"]
