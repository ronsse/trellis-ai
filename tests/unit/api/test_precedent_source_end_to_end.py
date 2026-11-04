"""A real learning promotion, read back below and above its source's label (#1080).

Other suites seed the precedent event and node by hand. This one stores them
the way production does (``tests/promoted_precedent.py``), relabels the source
``restricted`` afterwards (the case the promotion-time rule cannot see), and
reads the precedent back through the REST surfaces that serve it outside a
pack. The MCP twin is ``tests/unit/mcp/test_precedent_source_end_to_end.py``.

``TestPacksAreUnchanged`` is the serving-freeze check: the label verbs a caller
may not apply to a non-document id
(``tests/unit/api/test_label_refused_off_document.py``) leave what a pack serves
exactly as it was, for a client with a clearance limit and one without.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.off_document_ids import OFF_DOCUMENT_KINDS, VERBS, seed_ids
from tests.promoted_precedent import (
    SOURCE_EXCERPT,
    SOURCE_ID,
    SOURCE_TITLE,
    promote,
    relabel_source_restricted,
)
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

READ = ["read"]
RESTRICTED = ["read", "restricted"]


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


class TestPrecedentOfARelabelledSource:
    def test_the_promotion_names_its_source_before_the_relabel(
        self, registry: StoreRegistry, client: TestClient
    ) -> None:
        """The premise: what is stored does carry the source id."""
        precedent_id = promote(registry)
        resp = client.get("/api/v1/precedents", headers=_headers(registry, READ))
        [row] = resp.json()["precedents"]
        assert row["entity_id"] == precedent_id
        assert row["payload"]["source_item_id"] == SOURCE_ID
        node = registry.knowledge.graph_store.get_node(precedent_id)
        assert node is not None
        assert node["properties"]["source_item_id"] == SOURCE_ID

    def test_rest_listing_withholds_the_row_below_the_label(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        precedent_id = promote(registry)
        relabel_source_restricted(registry)
        below = client.get("/api/v1/precedents", headers=_headers(registry, READ))
        assert below.status_code == 200, below.text
        assert below.json()["count"] == 0
        assert SOURCE_ID not in below.text
        above = client.get("/api/v1/precedents", headers=_headers(registry, RESTRICTED))
        [row] = above.json()["precedents"]
        assert row["entity_id"] == precedent_id
        assert row["payload"]["source_item_id"] == SOURCE_ID

    def test_rest_entity_read_blanks_the_source_pointer_below_the_label(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        precedent_id = promote(registry)
        relabel_source_restricted(registry)
        url = f"/api/v1/entities/{precedent_id}"
        below = client.get(url, headers=_headers(registry, READ))
        assert below.status_code == 200, below.text
        assert "source_item_id" not in below.json()["entity"]["properties"]
        above = client.get(url, headers=_headers(registry, RESTRICTED))
        assert above.json()["entity"]["properties"]["source_item_id"] == SOURCE_ID

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "A promotion stores the source's title and opening excerpt as text "
            "on the node and on the document at the precedent's id. The "
            "promotion-time rule omits both for a source that is labelled "
            "then; a relabel afterwards leaves them. Not part of the "
            "source_item_id pointer; drop this marker with the fix."
        ),
    )
    @pytest.mark.parametrize("route", ["entities", "documents"])
    def test_rest_read_serves_nothing_derived_from_the_source(
        self, client: TestClient, registry: StoreRegistry, route: str
    ) -> None:
        precedent_id = promote(registry)
        relabel_source_restricted(registry)
        below = client.get(
            f"/api/v1/{route}/{precedent_id}", headers=_headers(registry, READ)
        )
        assert below.status_code == 200, below.text
        for needle in (SOURCE_ID, SOURCE_TITLE, SOURCE_EXCERPT):
            assert needle not in below.text, needle


def _served(body: dict[str, Any]) -> dict[str, Any]:
    """What a pack puts in front of a client, minus per-call float drift.

    Keyword scores differ in the eighth decimal between two identical calls on
    an unchanged store (checked by ``test_the_projection_is_stable_between_calls``),
    so they are not part of "what was served".
    """
    return {
        "count": body["count"],
        "items": [
            {
                key: item[key]
                for key in (
                    "item_id",
                    "item_type",
                    "excerpt",
                    "rank",
                    "included",
                    "selection_reason",
                    "strategy_source",
                    "metadata",
                )
            }
            for item in body["items"]
        ],
        "advisories": body["advisories"],
        "withholding": body["withholding"],
        "axes": body["axes"],
    }


def _pack(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/packs", json={"intent": "failover runbook"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return _served(resp.json())


class TestPacksAreUnchanged:
    BODY = "failover runbook zebra ledger: rotate the vault seal keys. " * 4

    @pytest.fixture
    def populated(self, registry: StoreRegistry) -> dict[str, str]:
        store = registry.knowledge.document_store
        store.put("rb-open", self.BODY, {"title": "open runbook"})
        store.put("rb-secret", self.BODY + " secret", {**label("restricted")})
        promote(registry)
        relabel_source_restricted(registry)
        return seed_ids(registry)

    @pytest.mark.parametrize("scopes", [READ, RESTRICTED], ids=["read", "restricted"])
    def test_the_projection_is_stable_between_calls(
        self,
        client: TestClient,
        registry: StoreRegistry,
        populated: dict[str, str],
        scopes: list[str],
    ) -> None:
        headers = _headers(registry, scopes)
        first = _pack(client, headers)
        assert first["items"], "an empty pack proves nothing"
        assert _pack(client, headers) == first

    @pytest.mark.parametrize("scopes", [READ, RESTRICTED], ids=["read", "restricted"])
    def test_refused_label_writes_leave_a_pack_exactly_as_it_was(
        self,
        client: TestClient,
        registry: StoreRegistry,
        populated: dict[str, str],
        scopes: list[str],
    ) -> None:
        headers = _headers(registry, scopes)
        before = _pack(client, headers)
        assert "rb-open" in {item["item_id"] for item in before["items"]}

        writer = _headers(registry, ["mutate", "restricted"])
        for operation, args in VERBS.items():
            for kind in OFF_DOCUMENT_KINDS:
                resp = client.post(
                    "/api/v1/commands/batch",
                    json={
                        "commands": [
                            {
                                "operation": operation,
                                "args": {"doc_id": populated[kind], **args},
                            }
                        ],
                        "strategy": "sequential",
                    },
                    headers=writer,
                )
                [result] = resp.json()["results"]
                assert result["status"] == "rejected", (operation, kind, result)

        assert _pack(client, headers) == before
