"""What an ``external`` key receives over REST does not depend on what it may not read.

Three synthetic stores hold the same public documents (see
``tests/unit/retrieve/public_corpus.py``); the largest also holds documents a
public caller may not read, every one naming the query words, enough of them to
outrank the public documents on every axis. A pack must answer the same from all
three; a direct read (search, document list, document by id) must answer the
same from the two that differ only in what the caller may not read. Both are
compared apart from ids drawn per build and clocks.

A ``read`` key is run through the same calls as the control: if it answered the
same from every store the corpora would not differ and a pass would prove
nothing.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.public_corpus import (
    EMBED_FN_PATH,
    GATED_IDS,
    GATED_WORDS,
    HIDDEN_WORDS,
    PLANTED_VALUES,
    PUBLIC_IDS,
    QUERY,
    STALE_WORD,
    UNREADABLE_WORDS,
    build_world,
    differences,
    leaked,
    mask,
)
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.ingest_corpus.models import chunk_doc_id, is_chunk_doc_id
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

OPS_FILTER = {"domain": {"in": ["ops"]}}

FLAT_BODIES: list[dict[str, Any]] = [
    {"intent": QUERY},
    {"intent": QUERY, "max_items": 1},
    {"intent": QUERY, "max_items": 2},
    {"intent": QUERY, "max_items": 3},
    {"intent": QUERY, "max_tokens": 120},
    {"intent": QUERY, "domain": "ops"},
    {"intent": QUERY, "tag_filters": OPS_FILTER},
    {"intent": "no document holds these words"},
    *({"intent": f"{QUERY} {word}"} for word in HIDDEN_WORDS),
    *({"intent": word} for word in HIDDEN_WORDS),
]

SECTIONED_BODIES: list[dict[str, Any]] = [
    {"intent": QUERY, "sections": [{"name": "All"}]},
    {"intent": QUERY, "sections": [{"name": "One", "max_items": 1}, {"name": "Two"}]},
    {
        "intent": QUERY,
        "sections": [{"name": "Runbooks", "content_types": ["runbook"]}],
    },
    {
        "intent": f"{QUERY} bulkword",
        "sections": [{"name": "All"}, {"name": "More", "max_items": 2}],
    },
    {"intent": QUERY, "sections": [{"name": "Tight", "max_tokens": 80}]},
]

SEARCH_PARAMS: list[dict[str, Any]] = [
    {"q": "failover"},
    {"q": "failover", "limit": 1},
    {"q": "failover", "limit": 2},
    {"q": "failover", "limit": 3},
    {"q": "failover", "limit": 10},
    {"q": "failover runbook"},
    {"q": "failover", "domain": "ops"},
    {"q": "failover", "include_chunks": True},
    {"q": "chunkonlyword", "include_chunks": True},
    *({"q": word} for word in HIDDEN_WORDS),
]

CHUNK_URL = "/api/v1/documents/" + quote(chunk_doc_id("pub-parent", 0), safe="")

DOCUMENT_GETS = [
    "/api/v1/documents",
    "/api/v1/documents?limit=1",
    "/api/v1/documents?limit=2",
    "/api/v1/documents?q=failover",
    "/api/v1/documents?q=bulkword",
    "/api/v1/documents?include_chunks=true",
    "/api/v1/documents?q=failover&include_chunks=true",
    "/api/v1/documents?q=chunkonlyword",
    "/api/v1/documents?q=chunkonlyword&include_chunks=true",
    "/api/v1/documents/pub-drain",
    "/api/v1/documents/pub-stale",
    "/api/v1/documents/pub-parent",
    CHUNK_URL,
    "/api/v1/documents/h-unlabelled",
    "/api/v1/documents/h-restricted",
    "/api/v1/documents/h-old",
    "/api/v1/documents/h-bulk-00",
    "/api/v1/documents/not-a-document",
    "/api/version",
    "/readyz",
]


def _without_echoed_query(value: Any) -> Any:
    """``value`` without the ``query`` and ``intent`` a response echoes back."""
    if isinstance(value, dict):
        return {
            k: _without_echoed_query(v)
            for k, v in value.items()
            if k not in ("query", "intent")
        }
    if isinstance(value, list):
        return [_without_echoed_query(v) for v in value]
    return value


def _capture_one(client: TestClient, headers: dict[str, str], call: tuple) -> Any:
    method, url, kwargs = call
    resp = client.request(method, url, headers=headers, **kwargs)
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text
    return {"status": resp.status_code, "body": mask(body)}


def _calls() -> dict[str, tuple]:
    calls: dict[str, tuple] = {}
    for i, body in enumerate(FLAT_BODIES):
        calls[f"flat-{i}"] = ("POST", "/api/v1/packs", {"json": body})
    for i, body in enumerate(SECTIONED_BODIES):
        calls[f"sectioned-{i}"] = ("POST", "/api/v1/packs/sectioned", {"json": body})
    for i, params in enumerate(SEARCH_PARAMS):
        calls[f"search-{i}"] = ("GET", "/api/v1/search", {"params": params})
    for url in DOCUMENT_GETS:
        calls[f"get {url}"] = ("GET", url, {})
    return calls


def _walk_get_operations(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    """Every GET operation the app declares, called with generic arguments."""
    spec = client.get("/openapi.json").json()
    out: dict[str, Any] = {}
    for path, ops in sorted(spec["paths"].items()):
        op = ops.get("get")
        if op is None:
            continue
        url = path
        params: dict[str, Any] = {}
        for p in op.get("parameters", []):
            if p["in"] == "path":
                name = p["name"]
                value = (
                    "pub-drain"
                    if "doc" in name
                    else "h-unlabelled"
                    if "entity" in name or "node" in name
                    else "x"
                )
                url = url.replace("{" + name + "}", value)
            elif p.get("required"):
                params[p["name"]] = (
                    1 if p.get("schema", {}).get("type") == "integer" else "failover"
                )
        out[f"GET {path}"] = _capture_one(
            client, headers, ("GET", url, {"params": params})
        )
    return out


def _capture(tmp_path, monkeypatch, *, kind: str, scopes: list[str]) -> dict[str, Any]:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_ENABLE_EMBED_ON_INGEST", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    try:
        app_module._registry = reg
        build_world(reg, kind)
        token, record = generate_api_key("k", scopes)
        reg.operational.api_key_store.create(record)
        headers = {"Authorization": f"Bearer {token}"}
        client = TestClient(create_app())
        out = {name: _capture_one(client, headers, c) for name, c in _calls().items()}
        out.update(_walk_get_operations(client, headers))
        return out
    finally:
        reg.close()
        app_module._registry = None


WORLD_KEYS = (
    ("read,external", "base"),
    ("read,external", "gated"),
    ("read,external", "full"),
    ("read", "base"),
    ("read", "full"),
)


@pytest.fixture(scope="module")
def captures(tmp_path_factory):
    """The same calls answered by each world, for an ``external`` and a ``read`` key."""
    root = tmp_path_factory.mktemp("differential")
    monkeypatch = pytest.MonkeyPatch()
    try:
        return {
            (scopes, kind): _capture(
                root / f"{scopes}-{kind}",
                monkeypatch,
                kind=kind,
                scopes=scopes.split(","),
            )
            for scopes, kind in WORLD_KEYS
        }
    finally:
        monkeypatch.undo()


PACK_CALLS = [n for n in _calls() if n.startswith(("flat-", "sectioned-"))]
READ_CALLS = [n for n in _calls() if not n.startswith(("flat-", "sectioned-"))]


def _capture_packs(tmp_path, monkeypatch, *, scopes: list[str], seeded: bool) -> Any:
    """Every pack call against the ``base`` world, with or without a graph seed.

    The seed is a node the intent resolves to (``domain:failover``), so the graph
    axis runs for a caller that may read the graph and stays off for one that
    may not. It is a graph row, not a document, so ``external`` may not read it.
    """
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_ENABLE_EMBED_ON_INGEST", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    try:
        app_module._registry = reg
        build_world(reg, "base")
        if seeded:
            reg.knowledge.graph_store.upsert_node(
                node_id="domain:failover",
                node_type="domain",
                properties={"name": "failover"},
            )
        token, record = generate_api_key("k", scopes)
        reg.operational.api_key_store.create(record)
        headers = {"Authorization": f"Bearer {token}"}
        client = TestClient(create_app())
        calls = _calls()
        return {name: _capture_one(client, headers, calls[name]) for name in PACK_CALLS}
    finally:
        reg.close()
        app_module._registry = None


@pytest.fixture(scope="module")
def seed_captures(tmp_path_factory):
    root = tmp_path_factory.mktemp("graph-seed")
    monkeypatch = pytest.MonkeyPatch()
    try:
        return {
            (scopes, seeded): _capture_packs(
                root / f"{scopes}-{seeded}",
                monkeypatch,
                scopes=scopes.split(","),
                seeded=seeded,
            )
            for scopes in ("read,external", "read")
            for seeded in (False, True)
        }
    finally:
        monkeypatch.undo()


class TestAGraphSeedTheKeyMayNotReadDoesNotShowThrough:
    """Whether the graph holds a node the intent names is graph content.

    An ordinary key's pack reports it (the graph axis runs, or stays off) and may
    serve what it finds. An ``external`` key's pack must read the same either way.
    """

    @pytest.mark.parametrize("name", PACK_CALLS)
    def test_a_pack_answers_alike_with_and_without_the_seed(
        self, seed_captures, name
    ) -> None:
        a = seed_captures[("read,external", False)][name]
        b = seed_captures[("read,external", True)][name]
        assert not differences(a, b), (name, differences(a, b))

    def test_the_control_key_does_see_the_seed(self, seed_captures) -> None:
        """Without this the seed could change nothing and the test above is vacuous."""
        a = seed_captures[("read", False)]["flat-0"]
        b = seed_captures[("read", True)]["flat-0"]
        assert differences(a, b)


#: Request fields and response fields that echo what the caller itself sent.
_ECHOES = ("intent", "query", "q")


def _without_echoes(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_echoes(v) for k, v in value.items() if k not in _ECHOES}
    if isinstance(value, list):
        return [_without_echoes(v) for v in value]
    return value


class TestDifferential:
    @pytest.mark.parametrize("name", PACK_CALLS)
    def test_a_pack_answers_alike_from_every_store(self, captures, name) -> None:
        full = captures[("read,external", "full")][name]
        for kind in ("base", "gated"):
            other = captures[("read,external", kind)][name]
            assert not differences(other, full), (kind, name, differences(other, full))

    @pytest.mark.parametrize("name", READ_CALLS)
    def test_a_direct_read_answers_alike_when_only_unreadable_documents_differ(
        self, captures, name
    ) -> None:
        a = captures[("read,external", "gated")][name]
        b = captures[("read,external", "full")][name]
        assert not differences(a, b), (name, differences(a, b))

    def test_every_get_operation_answers_alike(self, captures) -> None:
        a = captures[("read,external", "gated")]
        b = captures[("read,external", "full")]
        walked = sorted(k for k in a if k.startswith("GET /"))
        assert len(walked) > 30, walked
        bad = {k: differences(a[k], b[k]) for k in walked if differences(a[k], b[k])}
        assert not bad, bad

    def test_the_control_key_does_see_the_difference(self, captures) -> None:
        """Without this the corpora could be identical and the rest vacuous."""
        a = captures[("read", "base")]
        b = captures[("read", "full")]
        differing = [
            n
            for n in ("flat-0", "flat-1", "search-0", "get /api/v1/documents")
            if differences(a[n], b[n])
        ]
        assert len(differing) == 4, differing

    def test_no_hidden_text_reaches_an_external_key(self, captures) -> None:
        # Bodies only: a call's name echoes the words it was sent, and a 404
        # echoes the id in the path the caller itself named.
        cap = captures[("read,external", "full")]
        dump = json.dumps(_without_echoes([v["body"] for v in cap.values()]))
        assert not leaked(dump, UNREADABLE_WORDS)
        listed = [n for n in cap if not n.startswith("get /api/v1/documents/")]
        ids = json.dumps(_without_echoes([cap[n]["body"] for n in listed]))
        assert not leaked(ids, ("h-unlabelled", "h-internal", "h-bulk", "ent-hidden"))
        packs = json.dumps(_without_echoes([cap[n]["body"] for n in PACK_CALLS]))
        assert not leaked(packs, (*GATED_WORDS, *GATED_IDS))

    def test_a_pack_is_not_empty(self, captures) -> None:
        body = captures[("read,external", "full")]["flat-0"]["body"]
        assert {i["item_id"] for i in body["items"]} == set(PUBLIC_IDS) - {
            "pub-parent"
        } | {"pub-parent"}


class TestWhatIsServed:
    def test_a_stale_vector_row_is_never_the_text_served(self, captures) -> None:
        dump = json.dumps(captures[("read,external", "full")])
        assert not leaked(dump, [STALE_WORD])
        pack = captures[("read,external", "full")]["flat-0"]["body"]
        stale = [i for i in pack["items"] if i["item_id"] == "pub-stale"]
        assert stale
        assert "cleaned text" in stale[0]["excerpt"]

    def test_no_chunk_is_served_or_named(self, captures) -> None:
        cap = captures[("read,external", "full")]
        bodies = _without_echoed_query([c["body"] for c in cap.values()])
        assert not leaked(json.dumps(bodies), ["chunkonlyword"])
        named = json.dumps({k: v for k, v in cap.items() if k != f"get {CHUNK_URL}"})
        assert not leaked(named, [chunk_doc_id("pub-parent", 0)])

    @pytest.mark.parametrize(
        ("asked", "plain"),
        [
            ("/api/v1/documents?include_chunks=true", "/api/v1/documents"),
            (
                "/api/v1/documents?q=failover&include_chunks=true",
                "/api/v1/documents?q=failover",
            ),
            (
                "/api/v1/documents?q=chunkonlyword&include_chunks=true",
                "/api/v1/documents?q=chunkonlyword",
            ),
        ],
    )
    def test_asking_for_chunks_changes_nothing_and_is_echoed_false(
        self, captures, asked: str, plain: str
    ) -> None:
        cap = captures[("read,external", "full")]
        got = cap[f"get {asked}"]["body"]
        assert got["include_chunks"] is False
        assert got == cap[f"get {plain}"]["body"]

    def test_asking_for_chunks_of_search_changes_nothing(self, captures) -> None:
        cap = captures[("read,external", "full")]
        names = [
            n
            for n, c in zip(
                (f"search-{i}" for i in range(len(SEARCH_PARAMS))),
                SEARCH_PARAMS,
                strict=True,
            )
            if c.get("include_chunks")
        ]
        assert names
        for name in names:
            assert cap[name]["body"]["include_chunks"] is False

    def test_a_chunk_id_answers_as_a_missing_id(self, captures) -> None:
        cap = captures[("read,external", "full")]
        got, missing = (
            cap[f"get {CHUNK_URL}"],
            cap["get /api/v1/documents/not-a-document"],
        )
        assert got["status"] == missing["status"] == 404
        assert json.dumps(got).replace(chunk_doc_id("pub-parent", 0), "ID") == (
            json.dumps(missing).replace("not-a-document", "ID")
        )

    def test_a_hidden_id_answers_as_a_missing_id(self, captures) -> None:
        cap = captures[("read,external", "full")]
        missing = json.dumps(cap["get /api/v1/documents/not-a-document"]).replace(
            "not-a-document", "ID"
        )
        for doc in ("h-unlabelled", "h-restricted", "h-bulk-00"):
            got = json.dumps(cap[f"get /api/v1/documents/{doc}"]).replace(doc, "ID")
            assert got == missing, doc

    def test_a_public_document_the_pack_withholds_is_still_readable_by_id(
        self, captures
    ) -> None:
        """Pins the corpus: ``h-old`` is public, so a direct read serves it."""
        cap = captures[("read,external", "full")]
        assert cap["get /api/v1/documents/h-old"]["status"] == 200

    def test_the_search_page_is_filled_from_public_documents_only(
        self, captures
    ) -> None:
        readable = set(PUBLIC_IDS) | set(GATED_IDS)
        for name, want in (("search-1", 1), ("search-2", 2), ("search-3", 3)):
            body = captures[("read,external", "full")][name]["body"]
            ids = [r["doc_id"] for r in body["results"]]
            assert len(ids) == want, (name, ids)
            assert set(ids) <= readable, (name, ids)

    def test_a_public_pack_names_no_planted_metadata(self, captures) -> None:
        dump = json.dumps(captures[("read,external", "full")])
        assert not leaked(dump, PLANTED_VALUES)


class TestAgainstTheOrdinaryPath:
    def test_a_public_pack_serves_what_a_read_key_is_served_less_chunks(
        self, captures
    ) -> None:
        """The separate public pipeline must not drift from the ordinary one.

        In a store holding only public documents an unbounded pack serves the
        same documents on both paths, apart from the chunk rows the public path
        never serves. The *order* is not compared: the public path scores the
        public rows alone (BM25 and cosine computed locally), so it is allowed to
        rank them differently from the ordinary path's whole-index scores.
        """
        unbounded = [
            f"flat-{i}"
            for i, body in enumerate(FLAT_BODIES)
            if "max_items" not in body and "max_tokens" not in body
        ]
        assert len(unbounded) >= 10, unbounded
        for name in unbounded:
            ordinary = [
                i["item_id"]
                for i in captures[("read", "base")][name]["body"]["items"]
                if not is_chunk_doc_id(i["item_id"])
            ]
            public = [
                i["item_id"]
                for i in captures[("read,external", "base")][name]["body"]["items"]
            ]
            assert sorted(public) == sorted(ordinary), name


def _residual_probe(tmp_path, monkeypatch, *, hidden: bool) -> dict[str, list[str]]:
    """The order three public documents come back in, with or without hidden ones."""
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_EMBEDDING_FN", raising=False)
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    try:
        app_module._registry = reg
        pub = label("public")
        docs = reg.knowledge.document_store
        for doc_id, text in (
            ("res-a", "alpha alpha alpha alpha filler words go here and there today"),
            ("res-b", "beta beta beta beta filler words go here and there today"),
            ("res-c", "alpha beta filler words go here and there today"),
        ):
            docs.put(doc_id, text, {"title": doc_id, **pub})
        if hidden:
            for i in range(40):
                docs.put(f"hid-{i}", f"alpha alpha filler {i}", {"title": f"hid {i}"})
        token, record = generate_api_key("k", ["read", "external"])
        reg.operational.api_key_store.create(record)
        headers = {"Authorization": f"Bearer {token}"}
        client = TestClient(create_app())
        found = client.get(
            "/api/v1/search", params={"q": "alpha beta"}, headers=headers
        )
        pack = client.post(
            "/api/v1/packs", json={"intent": "alpha beta"}, headers=headers
        )
        return {
            "search": [r["doc_id"] for r in found.json()["results"]],
            "pack": [i["item_id"] for i in pack.json()["items"]],
        }
    finally:
        reg.close()
        app_module._registry = None


def test_the_order_of_public_documents_does_not_move_with_the_hidden_corpus(
    tmp_path,
) -> None:
    """The keyword axis once ranked by the store's whole-corpus BM25.

    Hidden documents that repeat the query words moved the order of the public
    ones. The public path now reads public rows by predicate and scores them
    locally (``trellis.retrieve.public_rank``), so the order and which documents
    fit a limit are functions of the public documents alone.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        without = _residual_probe(tmp_path / "a", monkeypatch, hidden=False)
        with_hidden = _residual_probe(tmp_path / "b", monkeypatch, hidden=True)
    finally:
        monkeypatch.undo()
    assert without == with_hidden
