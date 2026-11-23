"""The provenance line end to end through the MCP pack tools. Values made up."""

from __future__ import annotations

import re

import pytest

from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.server import get_context as _get_context
from trellis.mcp.server import get_sectioned_context as _get_sectioned_context
from trellis.retrieve.builder_factory import PACK_PROVENANCE_LINE_ENV
from trellis.stores.registry import StoreRegistry

get_context = unwrap_tool(_get_context)
get_sectioned_context = unwrap_tool(_get_sectioned_context)

_INTENT = "rotate widget cache"


def _seed(registry: StoreRegistry) -> None:
    registry.knowledge.document_store.put(
        "doc-widget",
        "How to rotate the widget cache without downtime",
        metadata={"source_system": "example-wiki"},
    )


def test_off_by_default_renders_no_line(
    temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PACK_PROVENANCE_LINE_ENV, raising=False)
    _seed(temp_registry)
    result = get_context(_INTENT)
    assert "`doc-widget`" in result
    assert "Provenance:" not in result


def test_on_renders_a_line_under_the_flat_item(
    temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PACK_PROVENANCE_LINE_ENV, "on")
    _seed(temp_registry)
    result = get_context(_INTENT)
    block = result.split("`doc-widget`", 1)[1].split("## ", 1)[0]
    assert "Provenance: from example-wiki" in block
    assert "via keyword" in block


def test_on_never_reaches_index_mode(
    temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PACK_PROVENANCE_LINE_ENV, "on")
    _seed(temp_registry)
    result = get_context(_INTENT, index=True)
    assert "`doc-widget`" in result
    assert "Provenance:" not in result


def test_on_renders_in_the_sectioned_reply(
    temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PACK_PROVENANCE_LINE_ENV, "on")
    _seed(temp_registry)
    result = get_sectioned_context(_INTENT, sections=[{"name": "All"}])
    if "`doc-widget`" not in result:
        pytest.fail(f"seeded document not served:\n{result}")
    assert "  Provenance: from example-wiki" in result


def test_a_failing_input_read_drops_the_line_not_the_pack(
    temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    import trellis.mcp.tools.context as context_mod

    def _boom(*_args: object, **_kwargs: object) -> None:
        msg = "made-up failure"
        raise RuntimeError(msg)

    monkeypatch.setenv(PACK_PROVENANCE_LINE_ENV, "on")
    monkeypatch.setattr(context_mod, "provenance_inputs", _boom)
    _seed(temp_registry)
    result = get_context(_INTENT)
    assert "`doc-widget`" in result
    assert "Provenance:" not in result


_PACK_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


def test_a_failing_flag_read_renders_exactly_the_off_reply(
    temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    import trellis.mcp.tools.context as context_mod

    def _boom(*_args: object, **_kwargs: object) -> bool:
        msg = "made-up failure"
        raise RuntimeError(msg)

    monkeypatch.delenv(PACK_PROVENANCE_LINE_ENV, raising=False)
    _seed(temp_registry)
    off = get_context(_INTENT)
    monkeypatch.setattr(context_mod, "resolve_pack_provenance_line", _boom)
    failed = get_context(_INTENT)
    assert "Provenance:" not in failed
    assert _PACK_ID.sub("<pack>", failed) == _PACK_ID.sub("<pack>", off)
