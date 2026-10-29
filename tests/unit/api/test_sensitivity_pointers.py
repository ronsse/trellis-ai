"""No REST read surface names a supersession id the key cannot read (#1029).

A served loser stores ``lifecycle.superseded_by``, a served successor stores
``supersedes_doc_id`` and a served UPDATE addendum stores ``updates_doc_id``; any
of them may name a document above the caller's clearance.
Every surface that serves stored metadata drops such an id from the served copy,
the store judging the named document, never the query or the candidate pool.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

import trellis.retrieve.sensitivity as sensitivity_mod
import trellis.retrieve.strategies as strategies_mod
import trellis_api.app as app_module
from tests.unit.retrieve.test_sensitivity import _lifecycle, label
from trellis.auth import generate_api_key
from trellis.retrieve.sensitivity import SUPERSESSION_POINTER_PATHS
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

QUERY = "failover runbook"

# Distinct bodies: near-identical ones are collapsed by the pack's semantic dedup.
MATCHING = {
    "doc-open": "failover runbook, zeroth edition: drain the write queue. " * 4,
    "doc-old": "failover runbook, first edition: pause the nightly export jobs. " * 4,
    "doc-stale": "failover runbook, second edition: raise the replica lag alarm. " * 4,
    "doc-fine": "failover runbook, third edition: rehearse the dns cutover. " * 4,
    "doc-new-a": "failover runbook, fourth edition: verify quorum after promotion. "
    * 4,
    "doc-new-b": "failover runbook, fifth edition: archive the replication slots. " * 4,
    "doc-new-fine": "failover runbook, sixth edition: page the secondary first. " * 4,
    "doc-upd": "failover runbook, seventh edition: confirm the standby lag. " * 4,
    "doc-upd-fine": "failover runbook, eighth edition: flush the warm caches. " * 4,
}
# What the hidden and readable pointer targets hold. Only ``doc-secret`` matches
# the query: the others are never candidates, so a pool test could not see them.
TARGETS = {
    "doc-secret": "failover runbook zebra ledger: rotate the vault seal keys. " * 4,
    "doc-newer": "unrelated replacement text about queue depth dashboards",
    "doc-prior-secret": "unrelated archive note about retired capacity plans",
    "doc-fresh": "unrelated replacement text about cache warming schedules",
    "doc-prior": "unrelated archive note about retired pager rotations",
    "doc-base-secret": "unrelated annex note about retired storage quotas",
}
HIDDEN = ("doc-secret", "doc-newer", "doc-prior-secret", "doc-base-secret")
SERVED = tuple(MATCHING)

#: What each row stores. ``doc-old``/``doc-stale`` are losers of a hidden
#: successor (one matching the query, one not); ``doc-new-a``/``doc-new-b`` are
#: successors of a hidden loser; ``doc-upd`` is an UPDATE addendum to a hidden
#: document; ``doc-fine``/``doc-new-fine``/``doc-upd-fine`` name readable ids.
POINTERS: dict[str, dict[str, Any]] = {
    "doc-old": _lifecycle("superseded", "doc-secret"),
    "doc-stale": _lifecycle("superseded", "doc-newer"),
    "doc-fine": _lifecycle("superseded", "doc-fresh"),
    "doc-new-a": {"supersedes_doc_id": "doc-secret", "reconciliation": "supersede"},
    "doc-new-b": {
        "supersedes_doc_id": "doc-prior-secret",
        "reconciliation": "supersede",
    },
    "doc-new-fine": {"supersedes_doc_id": "doc-prior", "reconciliation": "supersede"},
    "doc-upd": {"updates_doc_id": "doc-base-secret", "reconciliation": "update"},
    "doc-upd-fine": {"updates_doc_id": "doc-prior", "reconciliation": "update"},
}
SURFACES = ["packs", "sectioned", "document", "list", "list_q", "search"]


def _seed(registry, *, labelled: bool = True) -> None:
    store = registry.knowledge.document_store
    for doc_id, body in {**MATCHING, **TARGETS}.items():
        meta: dict[str, Any] = {"title": doc_id, **POINTERS.get(doc_id, {})}
        if labelled and doc_id in HIDDEN:
            meta.update(label("restricted"))
        store.put(doc_id, body, meta)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    _seed(reg)
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    return TestClient(create_app())


def _headers(registry, scopes):
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


def _fetch(client, headers, surface) -> tuple[str, dict[str, dict[str, Any]]]:
    """``(response text, {doc_id: served metadata})`` for one surface."""
    if surface in ("packs", "sectioned"):
        path, body = (
            ("packs", {"intent": QUERY})
            if surface == "packs"
            else (
                "packs/sectioned",
                {"intent": QUERY, "sections": [{"name": "All"}]},
            )
        )
        resp = client.post(f"/api/v1/{path}", json=body, headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        items = (
            data["items"]
            if surface == "packs"
            else [i for s in data["sections"] for i in s["items"]]
        )
        return resp.text, {i["item_id"]: i["metadata"] for i in items}
    if surface == "document":
        texts: list[str] = []
        served: dict[str, dict[str, Any]] = {}
        for doc_id in SERVED:
            resp = client.get(f"/api/v1/documents/{doc_id}", headers=headers)
            assert resp.status_code == 200, resp.text
            texts.append(resp.text)
            served[doc_id] = resp.json()["document"]["metadata"]
        return "\n".join(texts), served
    if surface == "search":
        resp = client.get(
            "/api/v1/search", params={"q": QUERY, "limit": 50}, headers=headers
        )
        rows = resp.json()["results"]
        key = "doc_id"
    else:
        params = {"q": QUERY} if surface == "list_q" else {}
        resp = client.get("/api/v1/documents", params=params, headers=headers)
        rows = resp.json()["documents"]
        key = "doc_id"
    assert resp.status_code == 200, resp.text
    return resp.text, {r[key]: r["metadata"] for r in rows}


@pytest.fixture
def frozen_recency_clock(registry, monkeypatch):
    """Every recency decay in a pack reads one instant, taken after the seed.

    Recency decay reads the wall clock once per item, not once per request. The
    fixture's documents are written in a burst, so those with the same token
    count tie on keyword score to ~1e-10 and order on age alone; the write gaps
    are a fraction of a millisecond, the same size as the per-item clock
    skew a busy box adds. Two identical requests can then swap two tied items,
    and with them the body each carries. A frozen clock removes the skew.
    """
    frozen = datetime.now(UTC)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

    monkeypatch.setattr(strategies_mod, "datetime", _Frozen)


#: Fields two identical pack requests do not share: the id minted per request
#: and the timing. Scores are not here: with ``frozen_recency_clock`` they are
#: exact, so a drift in one is a failure rather than noise.
_VOLATILE = frozenset({"pack_id", "duration_ms"})


def _scrub(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _scrub(v) for k, v in node.items() if k not in _VOLATILE}
    if isinstance(node, list):
        return [_scrub(v) for v in node]
    return node


def _stable(text: str) -> str:
    """``text`` without ``_VOLATILE`` fields; every other byte is compared as served."""
    try:
        body = json.loads(text)
    except ValueError:
        return text  # the by-id surface joins several JSON bodies
    return json.dumps(_scrub(body), sort_keys=True)


def _expected_served(surface: str) -> set[str]:
    """The documents a reader of everything is served on ``surface``.

    A pack withholds ``doc-old`` as superseded: its successor is readable to
    that reader and matches the query, so it is in the same pool.
    """
    return set(SERVED) - ({"doc-old"} if surface in ("packs", "sectioned") else set())


def _pointer_values(metadata: dict[str, Any]) -> dict[tuple[str, ...], Any]:
    out: dict[tuple[str, ...], Any] = {}
    for path in SUPERSESSION_POINTER_PATHS:
        node: Any = metadata
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        out[path] = node
    return out


class TestAHiddenPointerIsDropped:
    @pytest.mark.parametrize("surface", SURFACES)
    @pytest.mark.parametrize("scopes", [["read"], ["read", "confidential"]])
    def test_no_hidden_id_reaches_the_response_and_the_rest_is_served(
        self, client, registry, surface, scopes
    ):
        text, served = _fetch(client, _headers(registry, scopes), surface)
        for hidden in HIDDEN:
            assert hidden not in text, f"{hidden} leaked on {surface}"
        assert set(SERVED) <= set(served), "every visible document is served"
        # Forward pointer: the loser reads superseded, by nothing nameable.
        for loser in ("doc-old", "doc-stale"):
            assert served[loser]["lifecycle"] == {"state": "superseded"}
        # Reverse pointer: the successor keeps its other keys.
        for successor in ("doc-new-a", "doc-new-b"):
            assert "supersedes_doc_id" not in served[successor]
            assert served[successor]["reconciliation"] == "supersede"
        # The UPDATE addendum: the pointer goes, the marker stays.
        assert "updates_doc_id" not in served["doc-upd"]
        assert served["doc-upd"]["reconciliation"] == "update"
        # A readable id is still named.
        assert served["doc-fine"]["lifecycle"]["superseded_by"] == "doc-fresh"
        assert served["doc-new-fine"]["supersedes_doc_id"] == "doc-prior"
        assert served["doc-upd-fine"]["updates_doc_id"] == "doc-prior"

    @pytest.mark.parametrize("surface", SURFACES)
    def test_a_target_that_does_not_match_the_query_is_still_concealed(
        self, client, registry, surface
    ):
        """The store judges: these three hidden targets match nothing."""
        text, served = _fetch(client, _headers(registry, ["read"]), surface)
        for hidden in ("doc-newer", "doc-prior-secret", "doc-base-secret"):
            assert hidden not in served
            assert hidden not in text

    @pytest.mark.parametrize("surface", SURFACES)
    def test_a_store_error_reads_as_unreadable(
        self, client, registry, monkeypatch, surface
    ):
        store = registry.knowledge.document_store
        real_get = store.get

        def flaky(doc_id, *args, **kwargs):
            if doc_id in ("doc-fresh", "doc-prior"):
                msg = "store down"
                raise RuntimeError(msg)
            return real_get(doc_id, *args, **kwargs)

        monkeypatch.setattr(store, "get", flaky)
        _, served = _fetch(client, _headers(registry, ["read"]), surface)
        # The listings serve the two targets as rows of their own, so the check
        # is on the metadata of the rows that named them.
        assert served["doc-fine"]["lifecycle"] == {"state": "superseded"}
        assert "supersedes_doc_id" not in served["doc-new-fine"]
        assert "updates_doc_id" not in served["doc-upd-fine"]
        assert "doc-fresh" not in json.dumps(served["doc-fine"])
        assert "doc-prior" not in json.dumps(served["doc-new-fine"])
        assert "doc-prior" not in json.dumps(served["doc-upd-fine"])


class TestNothingChangesForAReaderOfEverything:
    @pytest.fixture(params=["admin", "restricted", "auth-off"])
    def headers(self, request, registry, monkeypatch):
        if request.param == "auth-off":
            monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
            return {}
        scopes = ["admin"] if request.param == "admin" else ["read", "restricted"]
        return _headers(registry, scopes)

    @pytest.mark.parametrize("surface", SURFACES)
    def test_every_pointer_is_served_as_stored(
        self, client, registry, headers, surface
    ):
        store = registry.knowledge.document_store
        text, served = _fetch(client, headers, surface)
        assert _expected_served(surface) <= set(served)
        for doc_id in _expected_served(surface):
            stored = store.get(doc_id)["metadata"]
            if surface in ("packs", "sectioned"):
                assert _pointer_values(served[doc_id]) == _pointer_values(stored)
            else:
                assert served[doc_id] == stored
        assert "doc-secret" in text

    @pytest.mark.usefixtures("frozen_recency_clock")
    @pytest.mark.parametrize("surface", SURFACES)
    def test_the_response_is_byte_identical_with_the_concealment_removed(
        self, client, registry, headers, monkeypatch, surface
    ):
        """No read, no copy: the helper is a no-op for a caller who reads all."""
        asked: list[Any] = []
        real = sensitivity_mod.readable_ids
        monkeypatch.setattr(
            sensitivity_mod,
            "readable_ids",
            lambda *a, **k: asked.append(a) or real(*a, **k),
        )
        with_helper, _ = _fetch(client, headers, surface)
        assert asked == []
        monkeypatch.setattr(
            sensitivity_mod, "conceal_document_rows", lambda docs, *_a: list(docs)
        )
        import trellis_api.routes.explore as explore_mod
        import trellis_api.routes.retrieve as retrieve_mod

        monkeypatch.setattr(
            explore_mod, "conceal_document_rows", lambda docs, *_a: list(docs)
        )
        monkeypatch.setattr(
            retrieve_mod, "conceal_document_rows", lambda docs, *_a: list(docs)
        )
        without_helper, _ = _fetch(client, headers, surface)
        assert _stable(with_helper) == _stable(without_helper)


class TestAnUnlabelledStoreIsUnchanged:
    @pytest.mark.parametrize("surface", SURFACES)
    def test_a_read_key_is_served_every_pointer_as_stored(
        self, client, registry, surface
    ):
        _seed(registry, labelled=False)
        store = registry.knowledge.document_store
        _, served = _fetch(client, _headers(registry, ["read"]), surface)
        for doc_id in POINTERS:
            if surface in ("packs", "sectioned") and doc_id == "doc-old":
                continue  # its successor is now readable, so it is superseded away
            stored = store.get(doc_id)["metadata"]
            assert _pointer_values(served[doc_id]) == _pointer_values(stored)


class TestTheStoredRecordIsNeverRewritten:
    def test_every_surface_leaves_the_rows_as_they_were(self, client, registry):
        store = registry.knowledge.document_store

        def snapshot() -> str:
            rows = store.list_documents(limit=500, offset=0, include_chunks=True)
            return json.dumps(sorted(rows, key=lambda r: r["doc_id"]), sort_keys=True)

        before = snapshot()
        headers = _headers(registry, ["read"])
        for surface in SURFACES:
            _fetch(client, headers, surface)
        assert snapshot() == before
        for doc_id, pointers in POINTERS.items():
            stored = store.get(doc_id)["metadata"]
            assert pointers.items() <= stored.items()


class TestOneJudgementPerResponse:
    """The distinct ids a response names are judged in one batched call."""

    EXPECTED: ClassVar[list[str]] = [
        "doc-base-secret",
        "doc-fresh",
        "doc-newer",
        "doc-prior",
        "doc-prior-secret",
        "doc-secret",
    ]

    @pytest.fixture
    def spies(self, registry, monkeypatch):
        calls: list[list[str]] = []
        gets: list[str] = []
        real = sensitivity_mod.readable_ids
        store = registry.knowledge.document_store
        real_get = store.get

        def spy(document_store, ids, clearance):
            calls.append(list(ids))
            return real(document_store, ids, clearance)

        def counting_get(doc_id, *args, **kwargs):
            gets.append(doc_id)
            return real_get(doc_id, *args, **kwargs)

        monkeypatch.setattr(sensitivity_mod, "readable_ids", spy)
        monkeypatch.setattr(store, "get", counting_get)
        return calls, gets

    @pytest.mark.parametrize(
        "surface", ["packs", "sectioned", "list", "list_q", "search"]
    )
    def test_a_response_makes_one_call_and_reads_each_id_once(
        self, client, registry, spies, surface
    ):
        calls, gets = spies
        _fetch(client, _headers(registry, ["read"]), surface)
        assert calls == [self.EXPECTED]
        # ``doc-secret`` is named by two rows and read once.
        assert [g for g in gets if g in self.EXPECTED] == self.EXPECTED

    def test_a_by_id_read_judges_only_the_id_it_names(self, client, registry, spies):
        calls, gets = spies
        resp = client.get(
            "/api/v1/documents/doc-old", headers=_headers(registry, ["read"])
        )
        assert resp.status_code == 200
        assert calls == [["doc-secret"]]
        assert gets.count("doc-secret") == 1

    @pytest.mark.parametrize(
        "surface", ["packs", "sectioned", "list", "list_q", "search"]
    )
    def test_a_reader_of_everything_makes_no_call_and_no_read(
        self, client, registry, spies, surface
    ):
        calls, gets = spies
        _fetch(client, _headers(registry, ["admin"]), surface)
        assert calls == []
        assert [g for g in gets if g in self.EXPECTED] == []
