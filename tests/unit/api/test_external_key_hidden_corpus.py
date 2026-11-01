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
* 3000 hidden rows beside 2200 public ones (more public rows than one read
  holds), in three worlds: none, and one that repeats each of the two words;
* a hidden document whose vector is the query's own (it would top a semantic
  read that ranked the whole store).

A ``read`` key is run through the same calls in the worlds that must differ:
if it answered the same from every world, the corpora would not differ and a
pass would prove nothing. The corrupt-row class stores each shape of unreadable
``metadata_json`` with and without the queried words and pins that the status
never depends on them, and what every call does instead. The last class holds
the ceiling: which public rows are in reach is a function of public rows alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

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
    calls: dict[str, tuple[str, str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    # A 500 logs its traceback, and the unconfigured structlog default renders
    # every frame's locals through Rich: seconds per call, which is all a world
    # that answers 500 would spend. The statuses and bodies are the subject.
    for module in ("trellis_api.middleware", "trellis.retrieve.pack_builder"):
        monkeypatch.setattr(f"{module}.logger", MagicMock())
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
        out = {
            name: _capture_one(client, headers, c)
            for name, c in (calls or CALLS).items()
        }
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


#: The raw ``metadata_json`` a store fault could leave on a row: not JSON, and
#: JSON that is not an object.
CORRUPT_RAW = {
    "not-json": "{not json",
    "list": "[1]",
    "null": "null",
    "string": '"str"',
    "number": "5",
}

#: A row holding the queried words, and the same row without them.
WITH_THE_WORD = "tomatoes failover runbook"
WITHOUT_THE_WORD = "tomatoes"

#: Every read an external key can make that touches the public rows, with the
#: word the corrupt row holds in the queries.
CORRUPT_CALLS: dict[str, tuple[str, str, dict[str, Any]]] = {
    **CALLS,
    "documents-q-runbook": (
        "GET",
        "/api/v1/documents",
        {"params": {"q": "runbook"}},
    ),
    "pack-absent": ("POST", "/api/v1/packs", {"json": {"intent": "absentwordq"}}),
    "search-absent": ("GET", "/api/v1/search", {"params": {"q": "absentwordq"}}),
}

#: Measured: every one of those calls answers 500 for every corrupt shape, so a
#: stored row the SQLite filter cannot read takes down every public read, a
#: query for a word nobody holds (``pack-absent``, ``search-absent``) included.
#: Written out, not derived, so a change in either direction is a decision.
EXPECTED_FAILING_CALLS: dict[str, list[str]] = {
    shape: [
        "documents",
        "documents-q",
        "documents-q-1",
        "documents-q-runbook",
        "pack",
        "pack-1",
        "pack-2",
        "pack-absent",
        "pack-word",
        "pack-word-1",
        "search",
        "search-1",
        "search-2",
        "search-absent",
        "search-runbook-1",
        "sectioned",
    ]
    for shape in CORRUPT_RAW
}


def _corrupt_world(raw: str, content: str) -> Callable[[StoreRegistry], None]:
    def seed(reg: StoreRegistry) -> None:
        _put(reg, "h-corrupt", content, {})
        conn = reg.knowledge.document_store._get_conn()
        conn.execute(
            "UPDATE documents SET metadata_json = ? WHERE doc_id = ?",
            (raw, "h-corrupt"),
        )
        conn.commit()

    return seed


class TestACorruptHiddenRowDoesNotAnswerByTheQueriedWord:
    """A corrupt hidden row is not a membership oracle over its own text.

    The keyword axis reads the public rows and matches them itself, so a query
    word is never looked up among hidden rows. A row whose ``metadata_json`` is
    corrupt therefore answers the same whether or not its content holds the
    queried word: for every call, the status is the same with and without the
    word, and a 200 is the same payload as the world with no such row.

    What is left, and is pinned below rather than hidden: the SQLite store's
    *filtered* listing (the one the label predicate runs through) cannot read a
    row whose ``metadata_json`` is not JSON (``json_extract`` raises ``malformed
    JSON``) or is valid JSON that is not an object (``[1]``, ``null``, ``"str"``,
    ``5``: the store's own filter function raises). Every public read then
    answers 500, whoever the row belongs to and whatever it holds, a query for a
    word nobody holds included. A caller can tell that such a row exists, never
    what it holds, and the answer is the same for every query word. The
    unfiltered listing and ``search`` tolerate valid non-object JSON, so a key
    that reads more than ``public`` answers 200 for the same store.
    """

    @pytest.fixture(scope="class")
    def outcomes(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> dict[tuple[str, str], dict[str, Any]]:
        monkeypatch = pytest.MonkeyPatch()
        root = tmp_path_factory.mktemp("corrupt")
        out: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            for name, raw in CORRUPT_RAW.items():
                for tag, content in (
                    ("with", WITH_THE_WORD),
                    ("without", WITHOUT_THE_WORD),
                ):
                    out[(name, tag)] = _capture(
                        root / f"{name}-{tag}",
                        monkeypatch,
                        _corrupt_world(raw, content),
                        ["read", "external"],
                        raise_server_exceptions=False,
                        walk=False,
                        calls=CORRUPT_CALLS,
                    )
            out[("none", "base")] = _capture(
                root / "base",
                monkeypatch,
                lambda reg: None,
                ["read", "external"],
                raise_server_exceptions=False,
                walk=False,
                calls=CORRUPT_CALLS,
            )
            return out
        finally:
            monkeypatch.undo()

    @pytest.mark.parametrize("name", sorted(CORRUPT_RAW))
    def test_every_status_is_the_same_with_and_without_the_word(
        self, outcomes: dict[tuple[str, str], dict[str, Any]], name: str
    ) -> None:
        with_word = {c: r["status"] for c, r in outcomes[(name, "with")].items()}
        without = {c: r["status"] for c, r in outcomes[(name, "without")].items()}
        assert with_word == without

    @pytest.mark.parametrize("name", sorted(CORRUPT_RAW))
    def test_every_whole_answer_is_the_same_with_and_without_the_word(
        self, outcomes: dict[tuple[str, str], dict[str, Any]], name: str
    ) -> None:
        """Bodies too, not only statuses: the 500 envelope says nothing of the row."""
        for call, result in outcomes[(name, "with")].items():
            other = outcomes[(name, "without")][call]
            assert not differences(result, other), (name, call)
            blob = repr(result["body"])
            assert "tomatoes" not in blob
            assert "h-corrupt" not in blob

    @pytest.mark.parametrize("name", sorted(CORRUPT_RAW))
    def test_every_answered_call_is_the_world_without_the_row(
        self, outcomes: dict[tuple[str, str], dict[str, Any]], name: str
    ) -> None:
        base = outcomes[("none", "base")]
        for tag in ("with", "without"):
            for call, result in outcomes[(name, tag)].items():
                if result["status"] == 200:
                    assert not differences(base[call], result), (name, tag, call)

    @pytest.mark.parametrize("name", sorted(CORRUPT_RAW))
    def test_the_row_and_its_word_are_never_served(
        self, outcomes: dict[tuple[str, str], dict[str, Any]], name: str
    ) -> None:
        for tag in ("with", "without"):
            blob = repr(
                {
                    c: r["body"]
                    for c, r in outcomes[(name, tag)].items()
                    if r["status"] == 200
                }
            )
            assert "h-corrupt" not in blob
            assert "tomatoes" not in blob

    def test_what_is_left_is_a_store_fault_visible_as_a_500(
        self, outcomes: dict[tuple[str, str], dict[str, Any]]
    ) -> None:
        """Pin which calls answer 500 for each corrupt shape, word or none."""
        failing = {
            name: sorted(
                c for c, r in outcomes[(name, "with")].items() if r["status"] == 500
            )
            for name in CORRUPT_RAW
        }
        assert failing == EXPECTED_FAILING_CALLS


class TestThePublicCeilingIsAFunctionOfPublicRows:
    """Past the ceiling, hidden rows do not choose which public rows are in reach.

    2200 public documents (1100 holding one word, 1100 another) is more than one
    read holds (the newest ``PUBLIC_ROW_CEILING``), so the ceiling bites. Three
    hidden corpora of 3000 rows are compared: none, one repeating the first word
    and one repeating the second. A ranking read done by the store would rank
    the hidden rows into its document frequencies and let them choose which
    public rows fill the page. Here the answers are identical, ``total_exact`` is
    ``false`` at the ceiling and the count is the ceiling.
    """

    PUBLIC_PER_WORD = 1100

    @pytest.fixture(scope="class")
    def answers(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> dict[str, dict[str, Any]]:
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        root = tmp_path_factory.mktemp("ceiling")
        out: dict[str, dict[str, Any]] = {}
        try:
            for world, word in (("none", None), ("alpha", "alphaw"), ("beta", "betaw")):
                reg = StoreRegistry(stores_dir=root / world)
                app_module._registry = reg
                try:
                    docs = reg.knowledge.document_store
                    for i in range(self.PUBLIC_PER_WORD):
                        docs.put(
                            f"p-a-{i:04d}",
                            f"alphaw common text {i}",
                            dict(label("public")),
                        )
                        docs.put(
                            f"p-b-{i:04d}",
                            f"betaw common text {i}",
                            dict(label("public")),
                        )
                    if word:
                        for i in range(3000):
                            docs.put(f"h-{i:04d}", f"{word} other stuff {i}", {})
                    token, record = generate_api_key("k", ["read", "external"])
                    reg.operational.api_key_store.create(record)
                    headers = {"Authorization": f"Bearer {token}"}
                    client = TestClient(create_app())
                    query = {"q": "alphaw betaw", "limit": 50}
                    search = client.get("/api/v1/search", params=query, headers=headers)
                    listed = client.get(
                        "/api/v1/documents", params=query, headers=headers
                    )
                    pack = client.post(
                        "/api/v1/packs",
                        json={"intent": "alphaw betaw", "max_items": 20},
                        headers=headers,
                    )
                    assert search.status_code == listed.status_code == 200
                    assert pack.status_code == 200
                    out[world] = {
                        "search": [r["doc_id"] for r in search.json()["results"]],
                        "documents": [d["doc_id"] for d in listed.json()["documents"]],
                        "total": listed.json()["total"],
                        "total_exact": listed.json()["total_exact"],
                        "pack": [i["item_id"] for i in pack.json()["items"]],
                    }
                finally:
                    reg.close()
                    app_module._registry = None
            return out
        finally:
            monkeypatch.undo()

    @pytest.mark.parametrize("world", ["alpha", "beta"])
    def test_the_answers_are_the_same_with_and_without_hidden_rows(
        self, answers: dict[str, dict[str, Any]], world: str
    ) -> None:
        assert answers[world] == answers["none"]

    def test_the_ceiling_bites_and_the_count_says_it_is_not_exact(
        self, answers: dict[str, dict[str, Any]]
    ) -> None:
        none = answers["none"]
        assert none["total"] == 2000
        assert none["total_exact"] is False
        assert len(none["search"]) == 50
        assert none["search"]
        assert none["documents"]
        assert none["pack"]
