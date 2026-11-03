"""What serving a pack to an ``external`` key over MCP writes, and what it leaves alone.

The MCP tools take a ``session_id``, so the session dedup that reads and writes
``PACK_ASSEMBLED`` is reachable here: an ``external`` key's pack must not read
what another caller's session recorded, and must not leave a record for another
caller's session to read.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import as_http
from tests.unit.retrieve.public_corpus import (
    EMBED_FN_PATH,
    PUBLIC_IDS,
    QUERY,
    build_world,
)
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

get_context = unwrap_tool(server_mod.get_context)
get_sectioned_context = unwrap_tool(server_mod.get_sectioned_context)
search = unwrap_tool(server_mod.search)

EXTERNAL = ["read", "external"]
ORDINARY = ["read"]
PAYLOAD_KEYS = {"pack_id", "served_item_ids", "count", "key_id"}

_PACK_ID = re.compile(r"pack_id:\*\* `([0-9A-Z]{26})`")
#: A flat pack heads an item ``## [type] `id```; a sectioned one lists it
#: ``- `id` (type)``.
_ITEM = re.compile(r"^(?:## \[[^\]]*\]|-) `([^`]+)`", re.MULTILINE)


@pytest.fixture
def world(temp_registry: StoreRegistry, monkeypatch) -> StoreRegistry:
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    monkeypatch.delenv("TRELLIS_PACK_HOLDOUT_RATE", raising=False)
    build_world(temp_registry, "full")
    return temp_registry


def _events(registry: StoreRegistry, event_type: EventType) -> list[Any]:
    return registry.operational.event_log.get_events(event_type=event_type, limit=1000)


def _ids(text: str) -> list[str]:
    return _ITEM.findall(text)


class TestEvent:
    def test_get_context_emits_one_public_served_event(self, world) -> None:
        with as_http(EXTERNAL):
            text = get_context(QUERY)
        served = _ids(text)
        assert served
        (event,) = _events(world, EventType.PUBLIC_PACK_SERVED)
        assert set(event.payload) == PAYLOAD_KEYS
        assert event.payload["pack_id"] == _PACK_ID.search(text).group(1)
        assert event.payload["served_item_ids"] == served
        assert event.payload["count"] == len(served)
        assert event.payload["key_id"] == "key-id"
        assert _events(world, EventType.PACK_ASSEMBLED) == []

    def test_get_sectioned_context_emits_one_public_served_event(self, world) -> None:
        with as_http(EXTERNAL):
            text = get_sectioned_context(
                QUERY, [{"name": "A", "max_items": 2}, {"name": "B"}]
            )
        served = _ids(text)
        assert served
        (event,) = _events(world, EventType.PUBLIC_PACK_SERVED)
        assert set(event.payload) == PAYLOAD_KEYS
        assert event.payload["served_item_ids"] == served
        assert event.payload["count"] == len(served)
        assert event.payload["key_id"] == "key-id"
        assert _events(world, EventType.PACK_ASSEMBLED) == []

    def test_search_emits_no_pack_assembled(self, world) -> None:
        with as_http(EXTERNAL):
            search(QUERY)
        assert _events(world, EventType.PACK_ASSEMBLED) == []

    def test_only_public_documents_are_ever_named(self, world) -> None:
        with as_http(EXTERNAL):
            get_context(QUERY)
            get_context("bulkword failover")
            get_context("restrictedword")
        for event in _events(world, EventType.PUBLIC_PACK_SERVED):
            assert set(event.payload["served_item_ids"]) <= set(PUBLIC_IDS)

    def test_an_ordinary_key_still_emits_pack_assembled(self, world) -> None:
        """The control: the event the external key must not emit is real."""
        with as_http(ORDINARY):
            get_context(QUERY)
        assert len(_events(world, EventType.PACK_ASSEMBLED)) == 1
        assert _events(world, EventType.PUBLIC_PACK_SERVED) == []


class TestHoldout:
    def test_an_external_pack_is_never_held_out(self, world, monkeypatch) -> None:
        monkeypatch.setenv("TRELLIS_PACK_HOLDOUT_RATE", "1.0")
        with as_http(EXTERNAL):
            assert _ids(get_context(QUERY))
            assert _ids(get_sectioned_context(QUERY, [{"name": "A"}]))

    def test_an_ordinary_pack_is_held_out_at_the_same_rate(
        self, world, monkeypatch
    ) -> None:
        """The control: at rate 1.0 the holdout does withhold a pack."""
        monkeypatch.setenv("TRELLIS_PACK_HOLDOUT_RATE", "1.0")
        with as_http(ORDINARY):
            assert _ids(get_context(QUERY)) == []


class TestSessionDedup:
    def test_an_external_pack_does_not_read_another_callers_session(
        self, world
    ) -> None:
        """An ordinary caller's pack in a session does not thin a public one."""
        with as_http(ORDINARY):
            first = get_context(QUERY, session_id="shared")
            assert _ids(first)
        with as_http(EXTERNAL):
            alone = _ids(get_context(QUERY))
            after = _ids(get_context(QUERY, session_id="shared"))
        assert alone
        assert set(after) == set(alone)

    def test_an_external_pack_leaves_no_record_for_a_session(self, world) -> None:
        """A public pack in a session does not thin an ordinary caller's.

        Compared on the public documents. The unreadable ones tie on every
        axis, and what ranks them is the age each row gets from its own clock
        reading in ``_apply_recency_decay``, so two identical calls can order
        them, and even pick among the bulk, differently.
        """
        public = set(PUBLIC_IDS)
        with as_http(ORDINARY):
            fresh = set(_ids(get_context(QUERY, session_id="untouched")))
        with as_http(EXTERNAL):
            assert _ids(get_context(QUERY, session_id="used"))
        with as_http(ORDINARY):
            after = set(_ids(get_context(QUERY, session_id="used")))
        # The control pack carries every public document, so dropping any of
        # them from the second one would show.
        assert public <= fresh
        assert public <= after

    def test_an_ordinary_session_does_dedup(self, world) -> None:
        """The control: the same two calls by an ordinary key do thin the second."""
        with as_http(ORDINARY):
            assert _ids(get_context(QUERY, session_id="s"))
            assert _ids(get_context(QUERY, session_id="s")) == []

    def test_a_repeat_in_an_external_session_is_not_thinned(self, world) -> None:
        with as_http(EXTERNAL):
            first = _ids(get_context(QUERY, session_id="s"))
            again = _ids(get_context(QUERY, session_id="s"))
            refreshed = _ids(get_context(QUERY, session_id="s", refresh=True))
        assert first
        assert set(again) == set(refreshed) == set(first)
