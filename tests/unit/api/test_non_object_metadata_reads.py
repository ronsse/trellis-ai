"""A stored row whose ``metadata`` is JSON but not an object fails no direct read.

``metadata_json`` of ``[1]``, ``5``, ``"str"`` or ``true`` parses, so the store
returns it, and the gate used to raise on it (``dict(5)``): every list, search
and by-id read by a key below ``restricted`` answered 500 while one such row
existed, whoever the row belonged to. Such a row carries no label the gate can
read, so it reads ``restricted``: a ``read`` or ``confidential`` key is served
every other row and not this one (a by-id read answers 404, as a missing id
does), a ``restricted`` or ``admin`` key is served it like every row, and an
``external`` key never was. The shapes ``dict()`` converts (``null``, an empty
value, a list of pairs) were never refused and are pinned as they were.

The by-id read of a row the store cannot return (bytes that are not UTF-8, an
integer past the parser's digit limit) is pinned in ``test_external_key_by_id_oracle``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

RESTRICTED_RECORD = {"data_classification": {"sensitivity": "restricted"}}

#: ``row id -> raw metadata_json`` for every shape under test.
ROWS: dict[str, str] = {
    "bad-list": "[1]",
    "bad-number": "5",
    "bad-string": '"str"',
    "bad-true": "true",
    # ``dict()`` refuses none of these, so they read as they always did.
    "old-null": "null",
    "old-empty-list": "[]",
    "old-empty-string": '""',
    "old-zero": "0",
    "old-false": "false",
    "old-pairs-public": json.dumps(
        [["data_classification", {"sensitivity": "public"}]]
    ),
    "old-pairs-restricted": json.dumps(list(RESTRICTED_RECORD.items())),
}

#: Every row ``dict()`` refuses.
REFUSED = {"bad-list", "bad-number", "bad-string", "bad-true"}

SCOPES = {
    "internal": ["read"],
    "confidential": ["read", "confidential"],
    "restricted": ["read", "restricted"],
    "admin": ["admin"],
    "external": ["read", "external"],
}


class World:
    def __init__(self, registry: StoreRegistry, client: TestClient) -> None:
        self.registry = registry
        self.client = client
        self._headers: dict[str, dict[str, str]] = {}

    def headers(self, who: str) -> dict[str, str]:
        if who not in self._headers:
            token, record = generate_api_key(who, SCOPES[who])
            self.registry.operational.api_key_store.create(record)
            self._headers[who] = {"Authorization": f"Bearer {token}"}
        return self._headers[who]

    def listed(self, who: str, **params: Any) -> set[str]:
        response = self.client.get(
            "/api/v1/documents",
            params={"limit": 100, **params},
            headers=self.headers(who),
        )
        assert response.status_code == 200, (who, params, response.text)
        return {d["doc_id"] for d in response.json()["documents"]}

    def searched(self, who: str, **params: Any) -> set[str]:
        response = self.client.get(
            "/api/v1/search",
            params={"q": "alpha", **params},
            headers=self.headers(who),
        )
        assert response.status_code == 200, (who, response.text)
        return {r["doc_id"] for r in response.json()["results"]}

    def by_id(self, who: str, doc_id: str) -> Any:
        return self.client.get(f"/api/v1/documents/{doc_id}", headers=self.headers(who))


def _insert_raw(registry: StoreRegistry, doc_id: str, raw: Any) -> None:
    """Store the row normally so search indexes it, then leave ``raw`` as metadata."""
    store = registry.knowledge.document_store
    store.put(doc_id, "alpha text", {})
    conn = store._get_conn()
    conn.execute(
        "UPDATE documents SET metadata_json = ? WHERE doc_id = ?", (raw, doc_id)
    )
    conn.commit()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    monkeypatch.setattr("trellis_api.middleware.logger", MagicMock())
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = registry
    try:
        store = registry.knowledge.document_store
        store.put("pub", "alpha text", dict(label("public")))
        store.put("plain", "alpha text", {})
        for doc_id, raw in ROWS.items():
            _insert_raw(registry, doc_id, raw)
        yield World(registry, TestClient(create_app(), raise_server_exceptions=False))
    finally:
        registry.close()
        app_module._registry = None


class TestARowWhoseMetadataIsNotAnObject:
    @pytest.mark.parametrize("who", ["internal", "confidential"])
    def test_a_key_below_restricted_is_served_every_other_row(
        self, world: World, who: str
    ) -> None:
        expected = {"plain", "pub"} | {
            r for r in ROWS if r.startswith("old-") and r != "old-pairs-restricted"
        }
        assert world.listed(who) == expected
        assert world.listed(who, q="alpha") == expected
        assert world.searched(who) == expected

    @pytest.mark.parametrize("who", ["restricted", "admin"])
    def test_a_restricted_key_is_served_every_row(self, world: World, who: str) -> None:
        every = {"plain", "pub", *ROWS}
        assert world.listed(who) == every
        assert world.listed(who, q="alpha") == every
        assert world.searched(who) == every

    def test_an_external_key_is_served_the_public_row_only(self, world: World) -> None:
        assert world.listed("external") == {"pub"}
        assert world.listed("external", q="alpha") == {"pub"}
        assert world.searched("external") == {"pub"}

    @pytest.mark.parametrize("who", ["internal", "confidential"])
    @pytest.mark.parametrize("doc_id", sorted(ROWS))
    def test_by_id_a_key_below_restricted_is_refused_what_it_cannot_read(
        self, world: World, who: str, doc_id: str
    ) -> None:
        hidden = REFUSED | {"old-pairs-restricted"}
        response = world.by_id(who, doc_id)
        assert response.status_code == (404 if doc_id in hidden else 200), doc_id
        if doc_id in hidden:
            assert response.json() == world.by_id(who, "no-such-id").json() | {
                "detail": f"Document not found: {doc_id}"
            }

    @pytest.mark.parametrize("who", ["restricted", "admin"])
    @pytest.mark.parametrize("doc_id", sorted(ROWS))
    def test_by_id_a_restricted_key_reads_every_row(
        self, world: World, who: str, doc_id: str
    ) -> None:
        response = world.by_id(who, doc_id)
        assert response.status_code == 200, doc_id
        assert response.json()["document"]["doc_id"] == doc_id

    @pytest.mark.parametrize("doc_id", sorted(ROWS))
    def test_by_id_an_external_key_is_refused_every_one(
        self, world: World, doc_id: str
    ) -> None:
        assert world.by_id("external", doc_id).status_code == 404

    @pytest.mark.parametrize("doc_id", sorted(REFUSED))
    def test_the_label_a_restricted_key_is_shown_says_the_row_is_corrupt(
        self, world: World, doc_id: str
    ) -> None:
        shown = world.by_id("restricted", doc_id).json()["document"]["label"]
        assert shown == {
            "sensitivity": "restricted",
            "stored": False,
            "corrupt": True,
            "external_readable": False,
        }
        listed = world.client.get(
            "/api/v1/documents", params={"limit": 100}, headers=world.headers("admin")
        ).json()["documents"]
        assert {d["doc_id"]: d["label"] for d in listed}[doc_id] == shown

    @pytest.mark.parametrize(
        "doc_id",
        [
            "old-null",
            "old-empty-list",
            "old-empty-string",
            "old-zero",
            "old-false",
            "old-pairs-public",
            "old-pairs-restricted",
        ],
    )
    def test_the_label_of_a_row_dict_converts_is_unchanged(
        self, world: World, doc_id: str
    ) -> None:
        """``dict()`` converts these, so the label reads no bag: ``internal``.

        A list of pairs is among them. The gate judges it by its converted
        content (``old-pairs-restricted`` is withheld from a ``read`` key), but
        the label has only ever described a row whose metadata is an object, and
        it still does.
        """
        shown = world.by_id("restricted", doc_id).json()["document"]["label"]
        assert shown == {
            "sensitivity": "internal",
            "stored": False,
            "corrupt": False,
            "external_readable": False,
        }
        listed = world.client.get(
            "/api/v1/documents", params={"limit": 100}, headers=world.headers("admin")
        ).json()["documents"]
        assert {d["doc_id"]: d["label"] for d in listed}[doc_id] == shown


class TestAChunkOfARowWhoseMetadataIsNotAnObject:
    """A chunk reads as its parent does, and the parent reads ``restricted``."""

    @staticmethod
    def _seed(world: World, raw: str) -> None:
        _insert_raw(world.registry, "par", raw)
        _insert_raw(world.registry, "par#chunk-0", json.dumps({"parent_doc_id": "par"}))

    @pytest.mark.parametrize("raw", sorted(["5", "[1]", '"str"', "true"]))
    @pytest.mark.parametrize("who", ["internal", "confidential"])
    def test_a_key_below_restricted_is_served_neither(
        self, world: World, raw: str, who: str
    ) -> None:
        self._seed(world, raw)
        listed = world.listed(who, include_chunks="true")
        assert "plain" in listed
        assert not {"par", "par#chunk-0"} & listed
        assert not {"par", "par#chunk-0"} & world.listed(
            who, include_chunks="true", q="alpha"
        )
        assert "plain" in world.searched(who, include_chunks="true")
        assert not {"par", "par#chunk-0"} & world.searched(who, include_chunks="true")
        for doc_id in ("par", "par#chunk-0"):
            assert world.by_id(who, doc_id.replace("#", "%23")).status_code == 404

    @pytest.mark.parametrize("raw", sorted(["5", "[1]", '"str"', "true"]))
    @pytest.mark.parametrize("who", ["restricted", "admin"])
    def test_a_restricted_key_is_served_both(
        self, world: World, raw: str, who: str
    ) -> None:
        self._seed(world, raw)
        assert {"par", "par#chunk-0"} <= world.listed(who, include_chunks="true")
        assert {"par", "par#chunk-0"} <= world.searched(who, include_chunks="true")
        for doc_id in ("par", "par#chunk-0"):
            assert world.by_id(who, doc_id.replace("#", "%23")).status_code == 200

    @pytest.mark.parametrize("raw", ["null", "[]", '""'])
    def test_a_parent_dict_converts_leaves_its_chunk_as_it_was(
        self, world: World, raw: str
    ) -> None:
        self._seed(world, raw)
        assert {"par", "par#chunk-0"} <= world.listed("internal", include_chunks="true")
        assert world.by_id("internal", "par%23chunk-0").status_code == 200

    def test_the_label_of_such_a_chunk_is_restricted_and_not_corrupt(
        self, world: World
    ) -> None:
        """The parent is the corrupt row; its chunk reads as a hidden parent's."""
        self._seed(world, "5")
        shown = world.by_id("restricted", "par%23chunk-0").json()["document"]["label"]
        assert shown == {
            "sensitivity": "restricted",
            "stored": False,
            "corrupt": False,
            "external_readable": False,
        }

    def test_an_empty_list_on_a_chunk_under_a_restricted_parent_is_not_corrupt(
        self, world: World
    ) -> None:
        """Its own value is not the fault, and it never reported one."""
        _insert_raw(world.registry, "par", json.dumps(RESTRICTED_RECORD))
        _insert_raw(world.registry, "par#chunk-0", "[]")
        shown = world.by_id("restricted", "par%23chunk-0").json()["document"]["label"]
        assert shown == {
            "sensitivity": "restricted",
            "stored": False,
            "corrupt": False,
            "external_readable": False,
        }
