"""The REST graph doors withhold a node labelled above the key (#1029).

``GET /graph/search``, ``GET /entities/{id}`` and ``GET /graph/history`` ask
:meth:`~trellis.retrieve.precedents.PrecedentClearance.verdict_of` about every
node they serve, and it now reads the node's own ``data_classification``
property, the label a pack already honours. ``total`` on ``/graph/search`` still
counts a labelled node: that is a known gap, pinned here so closing it is a
deliberate change.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import generate_api_key
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

LABELLED = "entity://ledger"
PLAIN = "entity://plain"
EDGE = "feeds_into"
#: Matches both nodes' names.
QUERY = "Table"
#: A key below the label, and one cleared for it.
BELOW = ["read"]
CLEARED = ["read", "restricted"]
_LABEL = {DATA_CLASSIFICATION_KEY: {"sensitivity": "restricted"}}


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    return TestClient(create_app())


def _seed(registry: StoreRegistry, *, label: bool) -> None:
    graph = registry.knowledge.graph_store
    secret: dict[str, Any] = {"name": "Ledger Table"}
    if label:
        secret.update(_LABEL)
    graph.upsert_node(LABELLED, "entity", secret)
    graph.upsert_node(PLAIN, "entity", {"name": "Plain Table"})
    graph.upsert_edge(PLAIN, LABELLED, EDGE)


@pytest.fixture
def labelled(registry: StoreRegistry) -> StoreRegistry:
    _seed(registry, label=True)
    return registry


@pytest.fixture
def unlabelled(registry: StoreRegistry) -> StoreRegistry:
    _seed(registry, label=False)
    return registry


def _headers(registry: StoreRegistry, scopes: list[str]) -> dict[str, str]:
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


def _search(client, registry, scopes) -> dict[str, Any]:
    resp = client.get(
        "/api/v1/graph/search",
        params={"q": QUERY, "limit": 50},
        headers=_headers(registry, scopes),
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _ids(body: dict[str, Any]) -> set[str]:
    return {r["entity_id"] for r in body["results"]}


def _entity(client, registry, scopes, entity_id):
    return client.get(
        f"/api/v1/entities/{entity_id}", headers=_headers(registry, scopes)
    )


def test_graph_search_leaves_out_a_labelled_node(client, labelled) -> None:
    assert _ids(_search(client, labelled, BELOW)) == {PLAIN}
    assert _ids(_search(client, labelled, CLEARED)) == {PLAIN, LABELLED}


def test_search_total_still_counts_a_labelled_node_known_gap(client, labelled) -> None:
    """Known gap: a key below the label can learn that some node matches."""
    below = _search(client, labelled, BELOW)
    assert _ids(below) == {PLAIN}
    assert below["total"] == 2


def test_entity_read_of_a_labelled_node_is_not_found(client, labelled) -> None:
    below = _entity(client, labelled, BELOW, LABELLED)
    missing = _entity(client, labelled, BELOW, "entity://nothing-here")
    cleared = _entity(client, labelled, CLEARED, LABELLED)

    assert below.status_code == 404
    assert missing.status_code == 404
    assert "Ledger" not in below.text
    assert cleared.status_code == 200
    assert "Ledger Table" in cleared.text


def test_entity_read_omits_a_labelled_neighbour_and_its_edge(client, labelled) -> None:
    below = _entity(client, labelled, BELOW, PLAIN)
    cleared = _entity(client, labelled, CLEARED, PLAIN)

    assert below.status_code == 200, below.text
    assert "Ledger Table" not in below.text
    assert LABELLED not in below.text
    assert EDGE not in below.text
    assert "Ledger Table" in cleared.text
    assert EDGE in cleared.text


def test_history_is_barred_when_any_version_is_labelled(client, labelled) -> None:
    # A newer version without the key: the older labelled one still bars it.
    labelled.knowledge.graph_store.upsert_node(
        LABELLED, "entity", {"name": "Ledger Table renamed"}
    )
    versions = labelled.knowledge.graph_store.get_node_history(LABELLED)
    assert len(versions) >= 2, "the premise: two versions"

    def _history(scopes):
        return client.get(
            "/api/v1/graph/history",
            params={"entity_id": LABELLED},
            headers=_headers(labelled, scopes),
        )

    below = _history(BELOW)
    assert below.status_code == 404, below.text
    assert "Ledger" not in below.text
    assert _history(CLEARED).status_code == 200


def test_an_unlabelled_store_answers_the_same_at_every_clearance(
    client, unlabelled
) -> None:
    below = _search(client, unlabelled, BELOW)
    assert _ids(below) == {PLAIN, LABELLED}, "an empty answer proves nothing"
    assert below == _search(client, unlabelled, CLEARED)
