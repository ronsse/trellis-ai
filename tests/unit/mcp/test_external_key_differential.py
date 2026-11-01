"""What an ``external`` key receives over MCP does not depend on what it may not read.

The same three synthetic stores as the REST differential
(``tests/unit/retrieve/public_corpus.py``): the same public documents, a ``gated``
world that also holds public documents a pack withholds, and a ``full`` world
that also holds documents the key may not read, every one naming the query
words and enough of them to outrank the public ones on every axis. A pack must
read the same from all three; ``get_items`` must read the same from the two that
differ only in what the key may not read. Ids drawn per build and clocks are
masked before comparing, nothing else is.

A ``read`` key is the control: it must see the difference, or the stores are not
different and a pass proves nothing.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import mint, unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import as_http
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
    leaked,
)
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.mcp.auth import TrellisApiKeyVerifier
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from pathlib import Path

get_context = unwrap_tool(server_mod.get_context)
get_sectioned_context = unwrap_tool(server_mod.get_sectioned_context)
search = unwrap_tool(server_mod.search)
get_items = unwrap_tool(server_mod.get_items)

CHUNK = chunk_doc_id("pub-parent", 0)

#: Per-build ids and clocks in a rendered pack.
_ULID = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")
_CLOCK = re.compile(r"\b\d+(\.\d+)?\s?ms\b|\b\d{4}-\d\d-\d\dT[\d:.+Z-]+")


def _mask(text: str) -> str:
    return _CLOCK.sub("<clock>", _ULID.sub("<id>", text))


def _pack_calls() -> dict[str, Any]:
    calls: dict[str, Any] = {
        "ctx": lambda: get_context(QUERY),
        "ctx-domain": lambda: get_context(QUERY, domain="ops"),
        "ctx-tight": lambda: get_context(QUERY, max_tokens=120),
        "ctx-index": lambda: get_context(QUERY, index=True),
        "ctx-session": lambda: get_context(QUERY, session_id="sess-1"),
        "ctx-session-again": lambda: get_context(QUERY, session_id="sess-1"),
        "ctx-refresh": lambda: get_context(QUERY, session_id="sess-1", refresh=True),
        "ctx-sections": lambda: get_context(QUERY, sections=[{"name": "All"}]),
        "ctx-sections-typed": lambda: get_context(
            QUERY, sections=[{"name": "Runbooks", "content_types": ["runbook"]}]
        ),
        "sectioned": lambda: get_sectioned_context(QUERY, [{"name": "All"}]),
        "sectioned-two": lambda: get_sectioned_context(
            QUERY, [{"name": "One", "max_items": 1}, {"name": "Two"}]
        ),
        "search": lambda: search(QUERY),
        "search-1": lambda: search(QUERY, limit=1),
        "search-2": lambda: search(QUERY, limit=2),
        "search-index": lambda: search(QUERY, index=True),
        "ctx-nothing": lambda: get_context("no document holds these words"),
    }
    for word in HIDDEN_WORDS:
        calls[f"ctx-{word}"] = lambda word=word: get_context(f"{QUERY} {word}")
        calls[f"ctx-only-{word}"] = lambda word=word: get_context(word)
        calls[f"search-{word}"] = lambda word=word: search(word)
    return calls


def _read_calls() -> dict[str, Any]:
    ids = [
        ["pub-drain"],
        ["pub-stale"],
        ["pub-parent"],
        [CHUNK],
        ["pub-drain", "pub-parent", CHUNK, "h-unlabelled", "not-a-document"],
        ["h-unlabelled"],
        ["h-internal"],
        ["h-restricted"],
        ["h-bulk-00"],
        ["not-a-document"],
        ["ent-hidden"],
        ["h-archived"],
    ]
    return {f"items {i}": (lambda i=i: get_items(i)) for i in ids}


PACK_CALLS = list(_pack_calls())
READ_CALLS = list(_read_calls())


async def _external_scopes(registry: StoreRegistry) -> list[str]:
    token = mint(registry, ["read", "external"], name="partner")
    verifier = TrellisApiKeyVerifier(lambda: registry.operational.api_key_store)
    access = await verifier.verify_token(token)
    assert access is not None
    return list(access.scopes)


def _capture(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: str,
    scopes: list[str] | None,
    seed_graph: bool = False,
) -> dict[str, str]:
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    monkeypatch.delenv("TRELLIS_ENABLE_EMBED_ON_INGEST", raising=False)
    reg = StoreRegistry(stores_dir=root / "stores")
    previous = server_mod._registry
    server_mod._registry = reg
    try:
        build_world(reg, kind)
        if seed_graph:
            # A graph node the intent resolves to: the graph axis runs for a
            # caller that may read the graph, and not for one that may not.
            reg.knowledge.graph_store.upsert_node(
                node_id="domain:failover",
                node_type="domain",
                properties={"name": "failover"},
            )
        resolved = scopes if scopes is not None else asyncio.run(_external_scopes(reg))
        out: dict[str, str] = {}
        with as_http(resolved):
            for name, call in {**_pack_calls(), **_read_calls()}.items():
                out[name] = _mask(call())
        return out
    finally:
        server_mod._registry = previous
        reg.close()


@pytest.fixture(scope="module")
def captures(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[tuple[str, str], dict[str, str]]:
    """The same calls answered by each world, for an ``external`` and a ``read`` key."""
    root = tmp_path_factory.mktemp("mcp-differential")
    monkeypatch = pytest.MonkeyPatch()
    try:
        return {
            (who, kind): _capture(
                root / f"{who}-{kind}", monkeypatch, kind=kind, scopes=scopes
            )
            for who, kind, scopes in (
                ("external", "base", None),
                ("external", "gated", None),
                ("external", "full", None),
                ("read", "base", ["read"]),
                ("read", "full", ["read"]),
            )
        }
    finally:
        monkeypatch.undo()


@pytest.fixture(scope="module")
def seed_captures(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[tuple[str, bool], dict[str, str]]:
    root = tmp_path_factory.mktemp("mcp-graph-seed")
    monkeypatch = pytest.MonkeyPatch()
    try:
        return {
            (who, seeded): _capture(
                root / f"{who}-{seeded}",
                monkeypatch,
                kind="base",
                scopes=scopes,
                seed_graph=seeded,
            )
            for who, scopes in (("external", None), ("read", ["read"]))
            for seeded in (False, True)
        }
    finally:
        monkeypatch.undo()


class TestAGraphSeedTheKeyMayNotReadDoesNotShowThrough:
    """Whether the graph holds a node the intent names is graph content."""

    @pytest.mark.parametrize("name", PACK_CALLS)
    def test_a_pack_reads_alike_with_and_without_the_seed(
        self, seed_captures, name
    ) -> None:
        a = seed_captures[("external", False)][name]
        b = seed_captures[("external", True)][name]
        assert a == b, name

    def test_the_control_key_does_see_the_seed(self, seed_captures) -> None:
        """Without this the seed could change nothing and the test above is vacuous."""
        a = seed_captures[("read", False)]["ctx"]
        b = seed_captures[("read", True)]["ctx"]
        assert a != b


class TestDifferential:
    @pytest.mark.parametrize("name", PACK_CALLS)
    def test_a_pack_reads_alike_from_every_store(self, captures, name) -> None:
        full = captures[("external", "full")][name]
        for kind in ("base", "gated"):
            assert captures[("external", kind)][name] == full, (kind, name)

    @pytest.mark.parametrize("name", READ_CALLS)
    def test_get_items_reads_alike_when_only_unreadable_documents_differ(
        self, captures, name
    ) -> None:
        a = captures[("external", "gated")][name]
        b = captures[("external", "full")][name]
        assert a == b, name

    def test_the_control_key_does_see_the_difference(self, captures) -> None:
        base, full = captures[("read", "base")], captures[("read", "full")]
        differing = [n for n in ("ctx", "search", "ctx-domain") if base[n] != full[n]]
        assert len(differing) == 3, differing

    def test_no_surface_states_a_withheld_count(self, captures) -> None:
        for name, text in captures[("external", "full")].items():
            assert "Withheld" not in text, name
            assert "withheld" not in text.lower(), name

    def test_no_hidden_text_reaches_an_external_key(self, captures) -> None:
        cap = captures[("external", "full")]
        # A tool's output echoes the words of the question it was asked, and
        # ``get_items`` the ids it was handed, so a call whose question names a
        # hidden word is left out of this, and an id is looked for in packs only.
        asked_with = [n for n in cap if any(w in n for w in HIDDEN_WORDS)]
        text = "\n".join(t for n, t in cap.items() if n not in asked_with)
        packs = "\n".join(
            t for n, t in cap.items() if n in PACK_CALLS and n not in asked_with
        )
        assert not leaked(text, (*UNREADABLE_WORDS, STALE_WORD, "chunkonlyword"))
        assert not leaked(text, PLANTED_VALUES)
        assert not leaked(packs, (*GATED_WORDS, *GATED_IDS))
        assert not leaked(packs, ("h-unlabelled", "h-bulk", "ent-hidden", CHUNK))

    def test_a_stale_vector_row_is_never_the_text_served(self, captures) -> None:
        cap = captures[("external", "full")]
        assert "cleaned text" in cap["ctx"]
        assert not leaked("\n".join(cap.values()), [STALE_WORD])

    def test_a_public_pack_is_not_empty(self, captures) -> None:
        for doc_id in PUBLIC_IDS:
            assert doc_id in captures[("external", "full")]["ctx"], doc_id

    def test_repeating_a_session_changes_nothing_for_a_public_key(
        self, captures
    ) -> None:
        """Session dedup is neither read nor written: a repeat reads as the first."""
        cap = captures[("external", "full")]
        assert cap["ctx-session"] == cap["ctx-session-again"] == cap["ctx-refresh"]

    def test_a_chunk_id_reads_as_a_missing_id(self, captures) -> None:
        cap = captures[("external", "full")]
        chunk = cap[f"items {[CHUNK]}"].replace(CHUNK, "ID")
        missing = cap["items ['not-a-document']"].replace("not-a-document", "ID")
        assert chunk == missing
        hidden = cap["items ['h-unlabelled']"].replace("h-unlabelled", "ID")
        assert hidden == missing
