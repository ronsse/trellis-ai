"""An id the SDK puts in a URL path reaches the server as that id.

Entity, trace and pack ids are any string. Interpolated raw, ``tbl#col`` was
requested as ``tbl`` (a ``#`` starts the URL fragment, a ``?`` the query), so
the caller was handed another record's content with a 200. These tests pin
three things, for the sync and async clients alike:

* :func:`path_id` itself, against written-out expectations;
* the bytes each id-taking method puts on the wire (``MockTransport``), so a
  new hostile character is a row in a table and not a new test;
* a round trip over the real routes, with a sibling row present so that a
  request that lands on the wrong row cannot pass as the right one.

Which methods take an id is not listed here: ``test_path_id_rule.py`` finds
them in the source and requires this file's roster to match.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import pytest_asyncio

from trellis.testing import in_memory_async_client, in_memory_client
from trellis_sdk._http import path_id
from trellis_sdk.async_client import AsyncTrellisClient
from trellis_sdk.client import TrellisClient
from trellis_sdk.exceptions import TrellisError

#: Ids a caller can legitimately hold. Each one breaks a raw interpolation
#: differently: ``#`` and ``?`` end the path, ``%`` is read back as an escape,
#: ``/`` adds segments, ``..`` is collapsed away by the HTTP library.
HOSTILE_IDS = [
    "tbl#col",
    "tbl?col=1",
    "50%25off",
    "50%off",
    "a b",
    "a/b",
    "a/../b",
    "uc://cat/sch/t",
    "..",
    ".",
    "café/ü",
    "a+b",
    "x&y=z;w",
    "[v]",
    'q"uote',
    "back\\slash",
]

#: ids that were already safe in a path: their wire form must not change.
UNCHANGED_IDS = ["abc", "01HZABCDEF", "entity:abc", "a-b_c.d~e", "a@b", "a,b", "a=b"]

#: method name -> (path before the id, path after it). One row per id-taking
#: method; ``test_path_id_rule.py`` requires it to match the source.
ROSTER = {
    "get_trace": ("/api/v1/traces/", ""),
    "get_entity": ("/api/v1/entities/", ""),
    "record_feedback": ("/api/v1/packs/", "/feedback"),
}

_FEEDBACK_BODY = {
    "status": "ok",
    "pack_id": "p",
    "feedback_id": "f",
    "feedback": "positive",
    "event_log_in_sync": True,
    "event_log_emitted": True,
    "event_log_skipped_as_duplicate": False,
}


def _respond(method: str, request: httpx.Request) -> httpx.Response:
    if method == "record_feedback":
        return httpx.Response(200, json=_FEEDBACK_BODY)
    return httpx.Response(404, json={"detail": "not found"})


async def _invoke(kind: str, method: str, id_: str) -> httpx.Request:
    """Call *method* with *id_* on a *kind* client; return what it sent."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _respond(method, request)

    transport = httpx.MockTransport(handler)
    if kind == "sync":
        sync = TrellisClient(
            http=httpx.Client(transport=transport, base_url="http://testserver"),
            verify_version=False,
        )
        _call(sync, method, id_)
        sync.close()
    else:
        asynchronous = AsyncTrellisClient(
            http=httpx.AsyncClient(transport=transport, base_url="http://testserver"),
            verify_version=False,
        )
        await _call_async(asynchronous, method, id_)
        await asynchronous.close()
    assert len(seen) == 1
    return seen[0]


def _call(client: TrellisClient, method: str, id_: str) -> Any:
    if method == "record_feedback":
        return client.record_feedback(id_, rating=0.5)
    return getattr(client, method)(id_)


async def _call_async(client: AsyncTrellisClient, method: str, id_: str) -> Any:
    if method == "record_feedback":
        return await client.record_feedback(id_, rating=0.5)
    return await getattr(client, method)(id_)


class TestPathId:
    @pytest.mark.parametrize(
        ("raw", "encoded"),
        [
            ("tbl#col", "tbl%23col"),
            ("tbl?col", "tbl%3Fcol"),
            ("a/b", "a%2Fb"),
            ("50%off", "50%25off"),
            ("50%25off", "50%2525off"),
            ("a b", "a%20b"),
            ("a/../b", "a%2F..%2Fb"),
            ("café", "caf%C3%A9"),
            # Dot segments are removed by the HTTP library before it sends,
            # and quoting leaves a dot alone, so a bare one is spelled out.
            (".", "%2E"),
            ("..", "%2E%2E"),
            ("...", "..."),
            ("a.b", "a.b"),
            # Already legal in a path segment: left as they were.
            ("entity:abc", "entity:abc"),
            ("a+b", "a+b"),
            ("a-b_c~d", "a-b_c~d"),
        ],
    )
    def test_encodes_one_segment(self, raw: str, encoded: str) -> None:
        assert path_id(raw) == encoded

    @pytest.mark.parametrize("raw", HOSTILE_IDS + UNCHANGED_IDS)
    def test_decodes_back_to_the_id(self, raw: str) -> None:
        assert unquote(path_id(raw)) == raw


class TestWireForm:
    """What every id-taking method sends, on both clients, from one table."""

    @pytest.mark.parametrize("kind", ["sync", "async"])
    @pytest.mark.parametrize("method", sorted(ROSTER))
    @pytest.mark.parametrize("id_", HOSTILE_IDS)
    async def test_id_is_one_path_segment(
        self, kind: str, method: str, id_: str
    ) -> None:
        before, after = ROSTER[method]
        request = await _invoke(kind, method, id_)

        # Nothing of the id leaked into the query string or the fragment.
        assert request.url.query == b""
        assert request.url.fragment == ""
        raw = request.url.raw_path.decode("ascii")
        assert raw.startswith(before)
        assert raw.endswith(after)
        segment = raw[len(before) : len(raw) - len(after)]
        assert "/" not in segment
        assert segment not in {".", ".."}
        assert unquote(segment) == id_

    @pytest.mark.parametrize("kind", ["sync", "async"])
    @pytest.mark.parametrize("method", sorted(ROSTER))
    @pytest.mark.parametrize("id_", UNCHANGED_IDS)
    async def test_safe_ids_keep_their_wire_form(
        self, kind: str, method: str, id_: str
    ) -> None:
        before, after = ROSTER[method]
        request = await _invoke(kind, method, id_)
        assert request.url.raw_path.decode("ascii") == f"{before}{id_}{after}"


# -- round trip over the real routes ------------------------------------------

#: id -> an id a raw interpolation would have been read as. The sibling is
#: stored too, so landing on it is a visible wrong answer and not a not-found.
ENTITY_SIBLINGS = {
    "tbl#col": "tbl",
    "tbl?col": "tbl",
    "50%25off": "50%off",
    "50%off": "50",
    "a b": "a",
    "a/../b": "b",
    "uc://cat/sch/t": "uc:",
    "..": "",
    ".": "",
    "a+b": "a b",
    "x&y=z;w": "x",
}

TRACE_SIBLINGS = {
    "t#1": "t",
    "t?1": "t",
    "50%25off": "50%off",
    "a b": "a",
    "..": "",
}


#: Starlette's sync ``TestClient`` decodes a request's path twice (it unquotes
#: ``httpx``'s already-decoded ``url.path``), so an id that is itself an escape
#: sequence cannot round-trip through it. ``httpx.ASGITransport``, the async
#: leg, and a real server decode once; the async class below covers these ids.
SYNC_TESTCLIENT_DOUBLE_DECODES = {"50%25off"}


def _registry() -> Any:
    import trellis_api.app as app_module

    return app_module._registry


def _seed_entities() -> None:
    store = _registry().knowledge.graph_store
    for id_, sibling in ENTITY_SIBLINGS.items():
        store.upsert_node(id_, "concept", {"name": f"own:{id_}"})
        if sibling:
            store.upsert_node(sibling, "concept", {"name": f"own:{sibling}"})


def _trace(trace_id: str) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "source": "agent",
        "intent": f"own:{trace_id}",
        "steps": [],
        "context": {"agent_id": "test-agent", "domain": "test"},
    }


class TestRoundTripSync:
    @pytest.fixture
    def client(self, tmp_path: Path):
        with in_memory_client(tmp_path / "stores") as c:
            yield c

    @pytest.mark.parametrize(
        "id_", [i for i in ENTITY_SIBLINGS if i not in SYNC_TESTCLIENT_DOUBLE_DECODES]
    )
    def test_get_entity_returns_that_entity(self, client: TrellisClient, id_: str):
        _seed_entities()
        entity = client.get_entity(id_)
        assert entity is not None
        assert entity["node_id"] == id_
        assert entity["properties"]["name"] == f"own:{id_}"

    @pytest.mark.parametrize(
        "id_", [i for i in TRACE_SIBLINGS if i not in SYNC_TESTCLIENT_DOUBLE_DECODES]
    )
    def test_get_trace_returns_that_trace(self, client: TrellisClient, id_: str):
        for trace_id in {id_, TRACE_SIBLINGS[id_]} - {""}:
            client.ingest_trace(_trace(trace_id))
        trace = client.get_trace(id_)
        assert trace is not None
        assert trace["trace_id"] == id_
        assert trace["intent"] == f"own:{id_}"

    def test_get_trace_with_a_slash_is_not_another_trace(self, client: TrellisClient):
        # ``/traces/{trace_id}`` is not a ``:path`` route, so ``t/1`` may read
        # as not found; it must not read as the trace ``t``.
        client.ingest_trace(_trace("t"))
        trace = client.get_trace("t/1")
        assert trace is None or trace["trace_id"] == "t/1"

    @pytest.mark.parametrize("id_", ["p#1", "p?1", "50%off", "p 1", "p/1"])
    def test_record_feedback_reaches_that_pack(self, client: TrellisClient, id_: str):
        # ``/packs/{pack_id}`` is not a ``:path`` route, so an id with ``/``
        # may be refused; what it must not do is land on another pack.
        try:
            response = client.record_feedback(id_, rating=0.5)
        except TrellisError:
            assert "/" in id_
            return
        assert response.pack_id == id_


class TestRoundTripAsync:
    @pytest_asyncio.fixture
    async def client(self, tmp_path: Path):
        async with in_memory_async_client(tmp_path / "stores") as c:
            yield c

    @pytest.mark.parametrize("id_", list(ENTITY_SIBLINGS))
    async def test_get_entity_returns_that_entity(
        self, client: AsyncTrellisClient, id_: str
    ):
        _seed_entities()
        entity = await client.get_entity(id_)
        assert entity is not None
        assert entity["node_id"] == id_
        assert entity["properties"]["name"] == f"own:{id_}"

    @pytest.mark.parametrize("id_", list(TRACE_SIBLINGS))
    async def test_get_trace_returns_that_trace(
        self, client: AsyncTrellisClient, id_: str
    ):
        for trace_id in {id_, TRACE_SIBLINGS[id_]} - {""}:
            await client.ingest_trace(_trace(trace_id))
        trace = await client.get_trace(id_)
        assert trace is not None
        assert trace["trace_id"] == id_
        assert trace["intent"] == f"own:{id_}"

    async def test_get_trace_with_a_slash_is_not_another_trace(
        self, client: AsyncTrellisClient
    ):
        await client.ingest_trace(_trace("t"))
        trace = await client.get_trace("t/1")
        assert trace is None or trace["trace_id"] == "t/1"

    @pytest.mark.parametrize("id_", ["p#1", "p?1", "50%off", "p 1", "p/1"])
    async def test_record_feedback_reaches_that_pack(
        self, client: AsyncTrellisClient, id_: str
    ):
        try:
            response = await client.record_feedback(id_, rating=0.5)
        except TrellisError:
            assert "/" in id_
            return
        assert response.pack_id == id_
