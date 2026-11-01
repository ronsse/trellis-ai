"""One predicate for "public" on every external surface, and a bound on the cost.

**The predicate.** An ``external`` key is served a document only when its stored
label is the exact public record. The list, the by-id read, ``/search``,
``/documents?q`` and a pack all apply it, so a record that merely reads
``public`` (bare frontmatter, another classifier, tags, a jurisdiction) is
served by none of them. Relabelling to ``public`` rewrites such a record, or is
refused with a reason when it would discard tags; it never reports success on a
no-op.

**The bound.** Public reads (a pack, ``/search``, ``/documents`` for an external
key) are run by at most ``MAX_CONCURRENT_PUBLIC_READS`` callers at once. Over
the limit the call is refused at once, 429 with ``Retry-After``, and a key that
reads more than ``public`` is never counted.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.retrieve import public_rank
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

EXACT = {
    "sensitivity": "public",
    "regulatory_tags": [],
    "jurisdiction": [],
    "classified_by": ["caller"],
    "classification_version": "1",
}

#: ``doc_id -> stored record`` for documents that read ``public`` and are not the
#: exact record.
NEAR_MISSES: dict[str, dict[str, Any]] = {
    "near-bare": {"sensitivity": "public"},
    "near-import": {**EXACT, "classified_by": ["import"]},
    "near-juris": {**EXACT, "jurisdiction": ["EU"]},
    "near-tags": {**EXACT, "regulatory_tags": ["gdpr"]},
}

WORD = "failover"


class World:
    def __init__(self, registry: StoreRegistry, client: TestClient) -> None:
        self.registry = registry
        self.client = client

    def key(self, scopes: list[str]) -> dict[str, str]:
        token, record = generate_api_key("k-" + "-".join(scopes), scopes)
        self.registry.operational.api_key_store.create(record)
        return {"Authorization": f"Bearer {token}"}

    def served(self, headers: dict[str, str]) -> dict[str, set[str]]:
        """The ids each surface serves to ``headers``'s key."""
        c = self.client
        listing = c.get("/api/v1/documents", params={"limit": 100}, headers=headers)
        query = c.get("/api/v1/documents", params={"q": WORD}, headers=headers)
        search = c.get("/api/v1/search", params={"q": WORD}, headers=headers)
        pack = c.post("/api/v1/packs", json={"intent": WORD}, headers=headers)
        for response in (listing, query, search, pack):
            assert response.status_code == 200, response.text
        ids = ["exact", *NEAR_MISSES]
        by_id = {
            i
            for i in ids
            if c.get(f"/api/v1/documents/{i}", headers=headers).status_code == 200
        }
        return {
            "by-id": by_id,
            "list": {d["doc_id"] for d in listing.json()["documents"]},
            "documents-q": {d["doc_id"] for d in query.json()["documents"]},
            "search": {r["doc_id"] for r in search.json()["results"]},
            "pack": {i["item_id"] for i in pack.json()["items"]},
        }


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    docs = reg.knowledge.document_store
    docs.put("exact", f"{WORD} exact text", {"title": "Exact", **label("public")})
    for doc_id, record in NEAR_MISSES.items():
        docs.put(doc_id, f"{WORD} {doc_id} text", {DATA_CLASSIFICATION_KEY: record})
    docs.put("plain", f"{WORD} plain text", {})
    try:
        yield World(reg, TestClient(create_app(), raise_server_exceptions=False))
    finally:
        reg.close()
        app_module._registry = None


EXTERNAL = ["read", "external"]
WRITER = ["read", "ingest", "mutate", "admin"]


class TestOnePredicateOnEverySurface:
    def test_only_the_exact_record_is_served_by_every_surface(
        self, world: World
    ) -> None:
        served = world.served(world.key(EXTERNAL))
        assert served == {name: {"exact"} for name in served}

    def test_a_read_key_reads_all_of_them(self, world: World) -> None:
        """The control: the documents are there, a key above public reads them."""
        served = world.served(world.key(["read"]))
        assert {"exact", *NEAR_MISSES} <= served["list"]
        assert {"exact", *NEAR_MISSES} <= served["by-id"]


class TestRelabellingOverRest:
    def _relabel(self, world: World, doc_id: str, level: str = "public") -> Any:
        return world.client.post(
            f"/api/v1/documents/{doc_id}/sensitivity",
            json={"sensitivity": level},
            headers=world.key(WRITER),
        )

    def test_a_bare_record_is_rewritten_and_then_served_everywhere(
        self, world: World
    ) -> None:
        response = self._relabel(world, "near-bare")
        assert response.status_code == 200, response.text
        assert "unchanged" not in response.text
        served = world.served(world.key(EXTERNAL))
        assert served == {name: {"exact", "near-bare"} for name in served}

    def test_another_classifiers_record_is_rewritten(self, world: World) -> None:
        assert self._relabel(world, "near-import").status_code == 200
        assert "near-import" in world.served(world.key(EXTERNAL))["search"]

    @pytest.mark.parametrize(
        ("doc_id", "named"),
        [("near-juris", "jurisdiction"), ("near-tags", "regulatory_tags")],
    )
    def test_a_record_with_tags_is_refused_naming_them_and_stays_unserved(
        self, world: World, doc_id: str, named: str
    ) -> None:
        response = self._relabel(world, doc_id)
        assert response.status_code == 400
        assert named in response.text
        assert "Nothing was written" in response.text
        served = world.served(world.key(EXTERNAL))
        assert served == {name: {"exact"} for name in served}

    def test_the_exact_record_is_an_honest_no_op(self, world: World) -> None:
        response = self._relabel(world, "exact")
        assert response.status_code == 200
        assert "unchanged" in response.text

    def test_a_document_with_no_label_is_made_servable(self, world: World) -> None:
        assert self._relabel(world, "plain").status_code == 200
        assert "plain" in world.served(world.key(EXTERNAL))["pack"]

    def test_a_non_public_relabel_claims_no_rewrite(self, world: World) -> None:
        """An unlabelled document relabelled ``internal`` keeps its fields."""
        response = self._relabel(world, "plain", "internal")
        assert response.status_code == 200, response.text
        assert "Classification set: plain internal -> internal" in response.text
        assert "rewritten" not in response.text

    def test_a_relabel_to_public_says_when_it_rewrote_the_record(
        self, world: World
    ) -> None:
        response = self._relabel(world, "near-bare")
        assert "record rewritten to the exact public record" in response.text


def _hold_slots(count: int) -> tuple[list[threading.Thread], threading.Event]:
    """``count`` other threads each holding one public-read slot until released."""
    release = threading.Event()
    held = [threading.Event() for _ in range(count)]

    def hold(entered: threading.Event) -> None:
        with public_rank.public_read_slot():
            entered.set()
            release.wait(10)

    threads = [threading.Thread(target=hold, args=(e,)) for e in held]
    for t in threads:
        t.start()
    for e in held:
        assert e.wait(5)
    return threads, release


PUBLIC_READS: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", "/api/v1/search", {"params": {"q": WORD}}),
    ("GET", "/api/v1/documents", {"params": {"q": WORD}}),
    ("GET", "/api/v1/documents", {}),
    ("POST", "/api/v1/packs", {"json": {"intent": WORD}}),
    (
        "POST",
        "/api/v1/packs/sectioned",
        {"json": {"intent": WORD, "sections": [{"name": "All"}]}},
    ),
]
IDS = ["search", "documents-q", "documents", "packs", "packs-sectioned"]


class TestTheConcurrencyBound:
    def test_the_limit_is_two_by_default(self) -> None:
        assert public_rank.MAX_CONCURRENT_PUBLIC_READS == 2

    @pytest.mark.parametrize(("method", "path", "kw"), PUBLIC_READS, ids=IDS)
    def test_a_read_over_the_limit_is_a_429_with_retry_after(
        self, world: World, method: str, path: str, kw: dict[str, Any]
    ) -> None:
        headers = world.key(EXTERNAL)
        threads, release = _hold_slots(public_rank.MAX_CONCURRENT_PUBLIC_READS)
        try:
            response = world.client.request(method, path, headers=headers, **kw)
        finally:
            release.set()
            for t in threads:
                t.join(5)
        assert response.status_code == 429, response.text
        assert response.headers["Retry-After"] == str(
            public_rank.PUBLIC_READ_RETRY_AFTER_SECONDS
        )
        body = response.json()
        assert body["code"] == "public_read_busy"
        assert "retry" in body["message"].lower()

    @pytest.mark.parametrize(("method", "path", "kw"), PUBLIC_READS, ids=IDS)
    def test_the_next_read_is_served_once_a_slot_is_free(
        self, world: World, method: str, path: str, kw: dict[str, Any]
    ) -> None:
        headers = world.key(EXTERNAL)
        threads, release = _hold_slots(public_rank.MAX_CONCURRENT_PUBLIC_READS - 1)
        try:
            response = world.client.request(method, path, headers=headers, **kw)
        finally:
            release.set()
            for t in threads:
                t.join(5)
        assert response.status_code == 200, response.text

    @pytest.mark.parametrize(
        ("method", "path", "kw"), PUBLIC_READS[:4:3], ids=["search", "packs"]
    )
    def test_a_key_that_reads_more_than_public_is_never_refused_or_counted(
        self, world: World, method: str, path: str, kw: dict[str, Any]
    ) -> None:
        headers = world.key(["read"])
        threads, release = _hold_slots(public_rank.MAX_CONCURRENT_PUBLIC_READS)
        try:
            response = world.client.request(method, path, headers=headers, **kw)
        finally:
            release.set()
            for t in threads:
                t.join(5)
        assert response.status_code == 200, response.text

    def test_a_read_by_id_is_not_a_public_read(self, world: World) -> None:
        headers = world.key(EXTERNAL)
        threads, release = _hold_slots(public_rank.MAX_CONCURRENT_PUBLIC_READS)
        try:
            response = world.client.get("/api/v1/documents/exact", headers=headers)
        finally:
            release.set()
            for t in threads:
                t.join(5)
        assert response.status_code == 200

    def test_the_refusal_does_not_depend_on_what_is_stored(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refusal is a function of public traffic alone."""
        monkeypatch.setattr(public_rank, "MAX_CONCURRENT_PUBLIC_READS", 0)
        headers = world.key(EXTERNAL)
        before = world.client.get("/api/v1/search", params={"q": WORD}, headers=headers)
        world.registry.knowledge.document_store.put("hidden", f"{WORD} " * 50, {})
        after = world.client.get("/api/v1/search", params={"q": WORD}, headers=headers)
        assert before.status_code == after.status_code == 429
        # Everything but the per-request id is the same.
        assert {**before.json(), "request_id": None} == {
            **after.json(),
            "request_id": None,
        }
        assert before.headers["Retry-After"] == after.headers["Retry-After"]

    def test_the_spec_documents_the_429(self, world: World) -> None:
        spec = world.client.get("/openapi.json").json()
        for path, method in [
            ("/api/v1/search", "get"),
            ("/api/v1/packs", "post"),
            ("/api/v1/packs/sectioned", "post"),
            ("/api/v1/documents", "get"),
        ]:
            response = spec["paths"][path][method]["responses"]["429"]
            assert "Retry-After" in response["headers"], path
