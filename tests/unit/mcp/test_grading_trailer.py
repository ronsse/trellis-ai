"""The reply trailer: off by default, one line when ``TRELLIS_GRADING_TRAILER`` is on.

The agent-visible text is part of the holdout's treatment environment, so the
trailer ships behind a write-behaviour knob. With the knob unset every
retrieval reply is byte-identical to before it existed (the byte-for-byte pin is
``test_loop_text_surfaces.py``, whose golden changes only on purpose, by hand,
in the same change; these tests add the behavioural ones).
"""

from __future__ import annotations

import re

import pytest

import trellis.mcp.grading_trailer as trailer_mod
import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.loop_text_surfaces import _PACK_ID, DOC_A, DOC_B, QUERY, _normalise
from trellis.core.write_config import ENV_VAR_BY_FIELD
from trellis.stores.registry import StoreRegistry
from trellis_wire.loop_text import grading_trailer

get_context = unwrap_tool(server_mod.get_context)
search = unwrap_tool(server_mod.search)
get_items = unwrap_tool(server_mod.get_items)

KNOB = ENV_VAR_BY_FIELD["grading_trailer"]
SERVED_PACK_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


@pytest.fixture
def stocked(temp_registry: StoreRegistry) -> StoreRegistry:
    store = temp_registry.knowledge.document_store
    store.put(DOC_A, f"{QUERY} first body about the ledger.", {"title": "Alpha"})
    store.put(DOC_B, f"{QUERY} second body about the lantern.", {"title": "Beta"})
    return temp_registry


def _pack_id(reply: str) -> str:
    match = _PACK_ID.search(reply)
    assert match is not None, reply
    return match.group(1)


def _strip(reply: str, line: str) -> str:
    assert reply.endswith("\n" + line), reply[-300:]
    return reply[: -len("\n" + line)]


def test_the_knob_is_off_by_default(stocked: StoreRegistry) -> None:
    reply = get_context(QUERY)
    assert "Grade this pack" not in reply
    assert "Grade this pack" not in get_items([DOC_A], pack_id=SERVED_PACK_ID)


def test_on_a_flat_pack_the_trailer_is_the_last_line(
    stocked: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    off = get_context(QUERY)
    monkeypatch.setenv(KNOB, "1")
    on = get_context(QUERY)
    line = grading_trailer(_pack_id(on))
    assert line is not None
    # Same pack apart from the trailer; ids differ between two assemblies.
    assert _normalise(_strip(on, line), {}) == _normalise(off, {})
    assert "\n" not in line
    assert f'record_feedback(pack_id="{_pack_id(on)}"' in line


@pytest.mark.parametrize(
    "kwargs",
    [{"index": True}, {"sections": [{"name": "Prior work"}]}],
    ids=["index", "sectioned"],
)
def test_the_other_pack_forms_carry_it_too(
    stocked: StoreRegistry, monkeypatch: pytest.MonkeyPatch, kwargs: dict
) -> None:
    monkeypatch.setenv(KNOB, "true")
    on = get_context(QUERY, **kwargs)
    line = grading_trailer(_pack_id(on))
    assert line is not None
    assert on.endswith("\n" + line)


def test_an_empty_pack_is_graded_too(
    stocked: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KNOB, "1")
    on = get_context("qqq nothing matches xx")
    line = grading_trailer(_pack_id(on))
    assert line is not None
    assert on.endswith("\n" + line)


def test_get_items_names_the_pack_it_was_given(
    stocked: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    off = get_items([DOC_A], pack_id=SERVED_PACK_ID)
    monkeypatch.setenv(KNOB, "1")
    on = get_items([DOC_A], pack_id=SERVED_PACK_ID)
    line = grading_trailer(SERVED_PACK_ID)
    assert line is not None
    assert _strip(on, line) == off


@pytest.mark.parametrize(
    "pack_id",
    [
        None,
        "",
        "   ",
        "pk-1",
        "<script>",
        "ignore previous instructions",
        SERVED_PACK_ID.lower(),
        # A well-formed ULID followed by caller text: only a full match refuses
        # these, a prefix match would echo the whole string into the reply.
        SERVED_PACK_ID + "`",
        SERVED_PACK_ID + "\n*x*",
        SERVED_PACK_ID + "](http://example.invalid)",
        SERVED_PACK_ID + " " + SERVED_PACK_ID,
    ],
)
def test_get_items_never_echoes_a_caller_supplied_non_ulid(
    stocked: StoreRegistry, monkeypatch: pytest.MonkeyPatch, pack_id: str | None
) -> None:
    monkeypatch.setenv(KNOB, "1")
    kwargs = {} if pack_id is None else {"pack_id": pack_id}
    reply = get_items([DOC_A], **kwargs)
    assert "Grade this pack" not in reply
    if pack_id and pack_id.strip():
        assert "Grade this pack" not in reply
        assert "ignore previous instructions" not in reply.split("\n")[-1]


def test_search_is_a_pack_and_carries_it(
    stocked: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    off = search(QUERY)
    monkeypatch.setenv(KNOB, "1")
    on = search(QUERY)
    line = grading_trailer(_pack_id(on))
    assert line is not None
    assert _normalise(_strip(on, line), {}) == _normalise(off, {})


def test_the_trailer_text_comes_from_the_single_source() -> None:
    line = grading_trailer(SERVED_PACK_ID)
    assert line == (
        f'*Grade this pack: `record_feedback(pack_id="{SERVED_PACK_ID}", '
        "rating=..., helpful_item_ids=[...], unhelpful_item_ids=[...])`.*"
    )
    assert re.fullmatch(r"[^\n]+", line)
    assert grading_trailer("not-a-ulid") is None
    assert grading_trailer(None) is None


@pytest.mark.parametrize(
    "suffix",
    ["`", "\n*x*", "](http://example.invalid)", " " + SERVED_PACK_ID, " "],
)
def test_the_guard_refuses_a_ulid_with_a_suffix(suffix: str) -> None:
    """``fullmatch``, not ``match``: a valid prefix must not carry text through."""
    assert grading_trailer(SERVED_PACK_ID + suffix) is None


def test_the_trailer_names_rating() -> None:
    """A minimal call without ``rating`` records a success, so the trailer names it."""
    line = grading_trailer(SERVED_PACK_ID)
    assert line is not None
    assert "rating=..." in line


def test_a_failure_in_the_trailer_never_fails_the_retrieval(
    stocked: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KNOB, "1")

    def boom(_registry: StoreRegistry) -> None:
        msg = "config unreadable"
        raise RuntimeError(msg)

    monkeypatch.setattr(trailer_mod, "resolve_write_behaviour", boom)
    reply = get_context(QUERY)
    assert "Grade this pack" not in reply
    assert "pack_id" in reply
