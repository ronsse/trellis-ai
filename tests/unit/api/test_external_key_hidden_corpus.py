"""An ``external`` key's answers do not move with documents it may not read.

``test_external_key_differential.py`` holds hidden documents that outrank the
public ones. This file widens the hidden corpus along the axes a ranking read
can leak through, one world each, and compares *whole payloads* (apart from ids
drawn per build and clocks) against the world with no hidden document at all:

* a hidden document holding a word a public document also holds (order and
  ``limit`` membership on ``/search``, a pack and ``/documents?q``);
* hidden documents that match nothing, at 0, 1, 5 and 40 (document frequency
  and the dedup winner between the two axes must not move);
* a hidden row whose ``auto_importance`` is malformed;
* 2500 hidden rows (more than the ceiling on rows one read holds);
* a hidden document whose vector is the query's own (it would top a semantic
  read that ranked the whole store).

A ``read`` key is run through the same calls in the worlds that must differ:
if it answered the same from every world, the corpora would not differ and a
pass would prove nothing. The last class pins what a corrupt hidden row does
and says why it is not compared.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.api.test_external_key_differential import (
    _capture_one,
    _walk_get_operations,
)
from tests.unit.retrieve.public_corpus import (
    EMBED_FN_PATH,
    QUERY,
    _put,
    differences,
    seed_public,
)
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

CALLS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "pack": ("POST", "/api/v1/packs", {"json": {"intent": QUERY}}),
    "pack-1": ("POST", "/api/v1/packs", {"json": {"intent": QUERY, "max_items": 1}}),
    "pack-2": ("POST", "/api/v1/packs", {"json": {"intent": QUERY, "max_items": 2}}),
    "pack-word": ("POST", "/api/v1/packs", {"json": {"intent": "failover"}}),
    "pack-word-1": (
        "POST",
        "/api/v1/packs",
        {"json": {"intent": "failover", "max_items": 1}},
    ),
    "sectioned": (
        "POST",
        "/api/v1/packs/sectioned",
        {
            "json": {
                "intent": QUERY,
                "sections": [{"name": "All"}, {"name": "One", "max_items": 1}],
            }
        },
    ),
    "search": ("GET", "/api/v1/search", {"params": {"q": "failover"}}),
    "search-1": ("GET", "/api/v1/search", {"params": {"q": "failover", "limit": 1}}),
    "search-2": ("GET", "/api/v1/search", {"params": {"q": "failover", "limit": 2}}),
    "search-runbook-1": (
        "GET",
        "/api/v1/search",
        {"params": {"q": "runbook", "limit": 1}},
    ),
    "documents-q": ("GET", "/api/v1/documents", {"params": {"q": "failover"}}),
    "documents-q-1": (
        "GET",
        "/api/v1/documents",
        {"params": {"q": "failover", "limit": 1}},
    ),
    "documents": ("GET", "/api/v1/documents", {}),
}

#: Real bodies for the POST routes that answer a query; every other POST
#: operation the app declares is called with an empty body.
POST_BODIES: dict[str, dict[str, Any]] = {
    "/api/v1/packs": {"intent": QUERY, "max_items": 2},
    "/api/v1/packs/sectioned": {
        "intent": QUERY,
        "sections": [{"name": "All"}, {"name": "One", "max_items": 1}],
    },
}


def _hidden_public_word(reg: StoreRegistry) -> None:
    _put(reg, "h-word", "failover " * 40, {"title": "Hidden"})


def _nonmatching(count: int) -> Callable[[StoreRegistry], None]:
    def seed(reg: StoreRegistry) -> None:
        for i in range(count):
            _put(reg, f"h-non-{i:03d}", f"gardening notes about tomatoes {i} " * 3, {})

    return seed


def _malformed_importance(reg: StoreRegistry) -> None:
    # The last is a number above the decay threshold with no
    # ``importance_scored_at``, which the importance read refuses.
    for value in ("not-a-number", {"score": "x"}, [1, 2], 0.9):
        _put(
            reg,
            f"h-bad-{type(value).__name__}",
            "failover runbook " * 6,
            {"auto_importance": value, **label("internal")},
        )


def _vector_is_the_query(reg: StoreRegistry) -> None:
    _put(reg, "h-vec", QUERY, {"title": "Hidden"})


def _bulk(reg: StoreRegistry) -> None:
    store = reg.knowledge.document_store
    for i in range(2500):
        store.put(f"h-bulk-{i:04d}", "failover runbook failover runbook", {})
    _put(reg, "h-bulk-vec", QUERY, {})


#: ``name -> (seed, whether a ``read`` key must see the difference)``.
WORLDS: dict[str, tuple[Callable[[StoreRegistry], None], bool]] = {
    "base": (lambda reg: None, False),
    "word": (_hidden_public_word, True),
    "non-0": (_nonmatching(0), False),
    "non-1": (_nonmatching(1), False),
    "non-5": (_nonmatching(5), False),
    "non-40": (_nonmatching(40), False),
    "malformed": (_malformed_importance, False),
    "vector": (_vector_is_the_query, True),
    "bulk": (_bulk, True),
}


def _walk_post_operations(
    client: TestClient, headers: dict[str, str]
) -> dict[str, Any]:
    """Every POST operation the app declares, with a real body where it has one."""
    spec = client.get("/openapi.json").json()
    out: dict[str, Any] = {}
    for path, ops in sorted(spec["paths"].items()):
        if "post" not in ops or "{" in path:
            continue
        out[f"POST {path}"] = _capture_one(
            client, headers, ("POST", path, {"json": POST_BODIES.get(path, {})})
        )
    return out


def _capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed: Callable[[StoreRegistry], None],
    scopes: list[str],
    *,
    raise_server_exceptions: bool = True,
    walk: bool = True,
) -> dict[str, Any]:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_ENABLE_EMBED_ON_INGEST", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    try:
        app_module._registry = reg
        seed_public(reg)
        seed(reg)
        token, record = generate_api_key("k", scopes)
        reg.operational.api_key_store.create(record)
        headers = {"Authorization": f"Bearer {token}"}
        client = TestClient(
            create_app(), raise_server_exceptions=raise_server_exceptions
        )
        out = {name: _capture_one(client, headers, c) for name, c in CALLS.items()}
        if walk:
            out.update(_walk_get_operations(client, headers))
            out.update(_walk_post_operations(client, headers))
        return out
    finally:
        reg.close()
        app_module._registry = None


@pytest.fixture(scope="module")
def captures(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, str], Any]:
    root = tmp_path_factory.mktemp("hidden-corpus")
    monkeypatch = pytest.MonkeyPatch()
    try:
        out: dict[tuple[str, str], Any] = {}
        for name, (seed, control) in WORLDS.items():
            out[("external", name)] = _capture(
                root / f"external-{name}", monkeypatch, seed, ["read", "external"]
            )
            if control or name == "base":
                out[("read", name)] = _capture(
                    root / f"read-{name}", monkeypatch, seed, ["read"]
                )
        return out
    finally:
        monkeypatch.undo()


class TestHiddenCorpusDoesNotMoveAnExternalAnswer:
    @pytest.mark.parametrize("world", [w for w in WORLDS if w != "base"])
    def test_every_payload_is_the_same_as_without_the_hidden_documents(
        self, captures: dict[tuple[str, str], Any], world: str
    ) -> None:
        base = captures[("external", "base")]
        other = captures[("external", world)]
        moved = {
            name: differences(base[name], other[name])
            for name in base
            if differences(base[name], other[name])
        }
        assert moved == {}

    def test_the_walk_holds_a_post_operation_with_a_real_body(
        self, captures: dict[tuple[str, str], Any]
    ) -> None:
        walked = captures[("external", "base")]
        for path in POST_BODIES:
            assert f"POST {path}" in walked, path
            assert walked[f"POST {path}"]["status"] == 200, path

    def test_the_base_world_answers_with_public_documents(
        self, captures: dict[tuple[str, str], Any]
    ) -> None:
        base = captures[("external", "base")]
        assert base["search"]["status"] == 200
        assert base["search"]["body"]["count"] >= 3
        assert base["pack"]["body"]["items"]
        assert base["documents-q"]["body"]["count"] >= 3


class TestControl:
    """The worlds differ: an internal reader sees what an external one must not."""

    @pytest.mark.parametrize(
        "world", [name for name, (_, control) in WORLDS.items() if control]
    )
    def test_a_read_key_answers_differently_when_hidden_documents_are_added(
        self, captures: dict[tuple[str, str], Any], world: str
    ) -> None:
        base = captures[("read", "base")]
        other = captures[("read", world)]
        assert any(differences(base[n], other[n]) for n in CALLS), world

    def test_a_hidden_document_holding_a_public_word_moves_every_read_surface(
        self, captures: dict[tuple[str, str], Any]
    ) -> None:
        base = captures[("read", "base")]
        other = captures[("read", "word")]
        for call in ("search-1", "pack-word-1", "documents-q-1"):
            assert differences(base[call], other[call]), call


class TestACorruptHiddenRow:
    """What one corrupt hidden row does, observed rather than assumed.

    A hidden row whose stored ``metadata_json`` is not JSON is a store fault.
    The SQLite document store cannot read such a row at all (a listing parses
    every row it returns, a filtered query evaluates ``json_extract`` over every
    row it visits), so a read that touches it raises whoever the row belongs to
    and the surface answers 500. It is a residual of the design, not a ranking
    read: the hidden row is never served, but a caller can tell that an
    unparseable row exists. The condition is a row the store itself cannot
    parse, which no ``put`` writes. It is pinned here, left out of the equality
    comparison above (an exception has no payload to compare), and named in
    ``docs/agent-guide/operations.md`` with that condition.
    """

    @pytest.fixture(scope="class")
    def corrupt(self, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
        def seed(reg: StoreRegistry) -> None:
            _put(reg, "h-corrupt", "failover runbook corruptword", {})
            conn = reg.knowledge.document_store._get_conn()
            conn.execute(
                "UPDATE documents SET metadata_json = ? WHERE doc_id = ?",
                ("{not json", "h-corrupt"),
            )
            conn.commit()

        monkeypatch = pytest.MonkeyPatch()
        try:
            return _capture(
                tmp_path_factory.mktemp("corrupt"),
                monkeypatch,
                seed,
                ["read", "external"],
                raise_server_exceptions=False,
                walk=False,
            )
        finally:
            monkeypatch.undo()

    def test_the_hidden_row_is_never_served(self, corrupt: dict[str, Any]) -> None:
        blob = repr({n: c["body"] for n, c in corrupt.items() if c["status"] == 200})
        assert "h-corrupt" not in blob
        assert "corruptword" not in blob

    def test_every_read_that_touches_the_row_fails_whole(
        self, corrupt: dict[str, Any]
    ) -> None:
        assert {
            name: call["status"] for name, call in corrupt.items()
        } == dict.fromkeys(CALLS, 500)
