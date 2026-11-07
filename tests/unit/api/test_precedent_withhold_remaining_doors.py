"""The REST doors #1211 left open judge a promoted precedent's source too (#1080).

``GET /documents`` (list and ``q`` search), ``GET /graph/search`` (the entity
listing) and ``GET /graph/history`` (a node's versions) served a precedent whose
source was labelled above the key. They now ask the one predicate
(:class:`~trellis.retrieve.precedents.PrecedentClearance`) the by-id reads, search
and the pack ask. Each door has the same three cases:

* a key below the source's label: the precedent is withheld (never rewritten);
* a key cleared for it: the precedent is served;
* a store with no label anywhere: the door answers exactly as it did before the
  gate, compared field for field against the same call with the predicate off.

Everything promotes through the real promotion path onto real SQLite stores.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.test_precedent_listing_clearance import (
    BODY,
    LEAKS,
    promote,
    relabel,
)
from trellis.auth import generate_api_key
from trellis.retrieve.precedents import PrecedentClearance
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Callable

SOURCE = "doc-source"
OPEN = "doc-open"
#: A key below the source's label, and one cleared for it.
BELOW = ["read"]
CLEARED = ["read", "confidential"]


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    reg.knowledge.graph_store.upsert_node("entity://shared", "entity", {"name": "x"})
    reg.knowledge.document_store.put(
        OPEN, "an open note about runbooks", {"title": "Open"}
    )
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    return TestClient(create_app())


@pytest.fixture
def open_precedent(registry: StoreRegistry) -> str:
    """A precedent promoted from a source that carries no label at all."""
    registry.knowledge.document_store.put(SOURCE, BODY, {"title": "Source"})
    entity_id = promote(registry, "cand:one", SOURCE)
    row = registry.knowledge.document_store.get(entity_id)
    assert row is not None
    assert all(leak in row["content"] for leak in LEAKS), row["content"]
    return entity_id


@pytest.fixture
def precedent(registry: StoreRegistry, open_precedent: str) -> str:
    """The same precedent once its source is labelled ``confidential``."""
    relabel(registry, SOURCE, "confidential")
    return open_precedent


def _headers(registry: StoreRegistry, scopes: list[str]) -> dict[str, str]:
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


# -- one call per door; each returns the response body and the precedent's ids --


def _documents(client, headers, entity_id: str, **params: Any) -> dict[str, Any]:
    resp = client.get("/api/v1/documents", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _document_ids(body: dict[str, Any]) -> list[str]:
    return [d["doc_id"] for d in body["documents"]]


def _list(client, headers, entity_id):
    return _documents(client, headers, entity_id)


def _search(client, headers, entity_id):
    return _documents(client, headers, entity_id, q="ZEBRAWORD")


def _entity_listing(client, headers, entity_id):
    resp = client.get(
        "/api/v1/graph/search", params={"q": "learning", "limit": 50}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _history(client, headers, entity_id):
    resp = client.get(
        "/api/v1/graph/history", params={"entity_id": entity_id}, headers=headers
    )
    assert resp.status_code in {200, 404}, resp.text
    return {"status_code": resp.status_code, "body": resp.json()}


def _served_ids(door: str, body: dict[str, Any]) -> list[str]:
    if door in {"list", "search"}:
        return _document_ids(body)
    if door == "entities":
        return [r["entity_id"] for r in body["results"]]
    return [body["body"]["entity_id"]] if body["status_code"] == 200 else []


DOORS: dict[str, Callable[..., dict[str, Any]]] = {
    "list": _list,
    "search": _search,
    "entities": _entity_listing,
    "history": _history,
}


@pytest.mark.parametrize("door", sorted(DOORS))
class TestEveryDoor:
    def test_a_key_cleared_for_the_source_is_served(
        self, client, registry, precedent, door
    ):
        body = DOORS[door](client, _headers(registry, CLEARED), precedent)

        assert precedent in _served_ids(door, body)

    def test_a_key_below_the_sources_label_is_not_served(
        self, client, registry, precedent, door
    ):
        # The premise: the same precedent is served to a key that may read it.
        assert precedent in _served_ids(
            door, DOORS[door](client, _headers(registry, CLEARED), precedent)
        )

        below = DOORS[door](client, _headers(registry, BELOW), precedent)

        assert precedent not in _served_ids(door, below)
        assert not any(leak in repr(below) for leak in LEAKS), below

    def test_an_unlabelled_store_answers_as_it_did_before_the_gate(
        self, client, registry, open_precedent, door, monkeypatch
    ):
        headers = _headers(registry, BELOW)
        with_gate = DOORS[door](client, headers, open_precedent)
        assert open_precedent in _served_ids(door, with_gate), "an empty answer"

        monkeypatch.setattr(PrecedentClearance, "verdict_of", lambda *_a, **_k: None)
        without_gate = DOORS[door](client, headers, open_precedent)

        assert with_gate == without_gate


class TestDocumentListing:
    def test_total_and_count_do_not_include_the_withheld_precedent(
        self, client, registry, precedent
    ):
        below = _list(client, _headers(registry, BELOW), precedent)
        cleared = _list(client, _headers(registry, CLEARED), precedent)

        assert _document_ids(below) == [OPEN]
        assert (below["count"], below["total"]) == (1, 1)
        assert below["total_exact"] is True
        assert set(_document_ids(cleared)) == {OPEN, SOURCE, precedent}
        assert cleared["total"] == 3

    def test_a_page_is_not_shortened_by_the_withheld_precedent(
        self, client, registry, precedent
    ):
        """Paging walks the readable rows, so one hidden row cuts no page."""
        registry.knowledge.document_store.put("doc-open-2", "more", {})
        headers = _headers(registry, BELOW)

        pages = [
            _document_ids(_documents(client, headers, precedent, limit=1, offset=n))
            for n in range(3)
        ]

        assert sorted(pages[0] + pages[1]) == [OPEN, "doc-open-2"]
        assert len(pages[0]) == len(pages[1]) == 1
        assert pages[2] == []

    def test_a_restricted_key_reads_the_store_directly(
        self, client, registry, precedent
    ):
        body = _list(client, _headers(registry, ["read", "restricted"]), precedent)

        assert precedent in _document_ids(body)

    def test_a_source_lookup_that_raises_withholds_the_precedent(
        self, client, registry, open_precedent, monkeypatch
    ):
        docs = registry.knowledge.document_store
        real_get = docs.get

        def get(doc_id: str, *args: Any, **kwargs: Any) -> Any:
            if doc_id == SOURCE:
                msg = "store unavailable"
                raise RuntimeError(msg)
            return real_get(doc_id, *args, **kwargs)

        headers = _headers(registry, BELOW)
        assert open_precedent in _document_ids(_list(client, headers, open_precedent))
        monkeypatch.setattr(docs, "get", get)

        assert open_precedent not in _document_ids(
            _list(client, headers, open_precedent)
        )


EXTERNAL = ["read", "external"]


class TestPublicOnlyKey:
    """An ``external`` key reads only what is labelled ``public``, by its own branch.

    A promotion writes the precedent's row with no label, so it reads
    ``internal`` and a public-only key never meets it. The branch matters once
    something labels that row ``public`` while its source stays above: the row's
    own label then passes the plain gate and only the source can say no.
    """

    @pytest.fixture
    def public_row(self, registry, open_precedent) -> str:
        relabel(registry, open_precedent, "public")
        relabel(registry, SOURCE, "public")
        return open_precedent

    @pytest.mark.parametrize("params", [{}, {"q": "ZEBRAWORD"}], ids=["list", "q"])
    def test_a_public_row_over_a_public_source_is_served(
        self, client, registry, public_row, params
    ):
        body = _documents(client, _headers(registry, EXTERNAL), public_row, **params)

        assert public_row in _document_ids(body)

    @pytest.mark.parametrize("params", [{}, {"q": "ZEBRAWORD"}], ids=["list", "q"])
    def test_a_public_row_over_a_source_above_public_is_withheld(
        self, client, registry, public_row, params
    ):
        relabel(registry, SOURCE, "confidential")
        headers = _headers(registry, EXTERNAL)

        body = _documents(client, headers, public_row, **params)

        assert public_row not in _document_ids(body)
        assert not any(leak in repr(body) for leak in LEAKS), body
        assert body["total"] == len(body["documents"])

    @pytest.mark.parametrize("params", [{}, {"q": "ZEBRAWORD"}], ids=["list", "q"])
    def test_an_all_public_store_answers_as_it_did_before_the_gate(
        self, client, registry, public_row, params, monkeypatch
    ):
        headers = _headers(registry, EXTERNAL)
        with_gate = _documents(client, headers, public_row, **params)
        assert public_row in _document_ids(with_gate), "an empty answer"

        monkeypatch.setattr(PrecedentClearance, "verdict_of", lambda *_a, **_k: None)

        assert _documents(client, headers, public_row, **params) == with_gate


class TestEntityListing:
    def test_an_unrelated_node_is_served_beside_the_withheld_precedent(
        self, client, registry, precedent
    ):
        resp = client.get(
            "/api/v1/graph/search", headers=_headers(registry, BELOW)
        ).json()

        ids = [r["entity_id"] for r in resp["results"]]
        assert "entity://shared" in ids
        assert precedent not in ids

    def test_a_restricted_key_reads_every_node(self, client, registry, precedent):
        resp = client.get(
            "/api/v1/graph/search",
            headers=_headers(registry, ["read", "restricted"]),
        ).json()

        assert precedent in [r["entity_id"] for r in resp["results"]]


class TestHistory:
    def test_a_withheld_precedent_answers_as_a_missing_entity(
        self, client, registry, precedent
    ):
        below = client.get(
            "/api/v1/graph/history",
            params={"entity_id": precedent},
            headers=_headers(registry, BELOW),
        )
        missing = client.get(
            "/api/v1/graph/history",
            params={"entity_id": "precedent://learning/nothing-here"},
            headers=_headers(registry, BELOW),
        )

        assert below.status_code == missing.status_code == 404
        assert not any(leak in below.text for leak in LEAKS)

    def test_every_version_is_withheld_with_the_current_one(
        self, client, registry, precedent
    ):
        """A second version exists; neither is served below the label."""
        node = registry.knowledge.graph_store.get_node(precedent)
        assert node is not None
        registry.knowledge.graph_store.upsert_node(
            precedent, node["node_type"], {**node["properties"], "note": "v2"}
        )
        cleared = client.get(
            "/api/v1/graph/history",
            params={"entity_id": precedent},
            headers=_headers(registry, CLEARED),
        ).json()
        assert cleared["count"] >= 2

        below = client.get(
            "/api/v1/graph/history",
            params={"entity_id": precedent},
            headers=_headers(registry, BELOW),
        )

        assert below.status_code == 404

    def test_a_node_outside_the_precedent_namespace_is_untouched(
        self, client, registry, precedent
    ):
        resp = client.get(
            "/api/v1/graph/history",
            params={"entity_id": "entity://shared"},
            headers=_headers(registry, BELOW),
        )

        assert resp.status_code == 200
