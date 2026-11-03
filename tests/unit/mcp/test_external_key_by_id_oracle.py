"""``get_items`` must not let a hidden row confirm a guessed id.

The MCP half of ``tests/unit/api/test_external_key_by_id_oracle.py``: a hidden
row whose stored metadata cannot be parsed answers an ``external`` key exactly as
a missing id does, a mixed call is not failed by one corrupt id, a store fault
stays an error, and a key above public is unchanged.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import pytest
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from tests.unit.api.test_external_key_by_id_oracle import (
    CORRUPT_ID,
    MISSING_ID,
    RAW_SHAPES,
    insert_raw,
)
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import as_http
from tests.unit.retrieve.test_sensitivity import label
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[StoreRegistry]:
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    monkeypatch.setattr(server_mod, "_registry", reg)
    reg.knowledge.document_store.put("pub-1", "visible words", {**label("public")})
    try:
        yield reg
    finally:
        reg.close()


def _fetch(ids: list[str]) -> str:
    return str(unwrap_tool(server_mod.get_items)(ids))


class TestAnUnparseableRowAnswersAsAMissingId:
    @pytest.mark.parametrize("shape", list(RAW_SHAPES))
    def test_an_external_key_gets_the_missing_id_answer(
        self, registry: StoreRegistry, shape: str
    ) -> None:
        insert_raw(registry.knowledge.document_store, CORRUPT_ID, *RAW_SHAPES[shape])
        with as_http(["read", "external"]):
            corrupt = _fetch([CORRUPT_ID])
            missing = _fetch([MISSING_ID])
        assert missing == "# Fetched items\n\n\n*[not found: `no-such-id`]*"
        assert corrupt.replace(CORRUPT_ID, "<id>") == missing.replace(
            MISSING_ID, "<id>"
        )

    @pytest.mark.parametrize("shape", list(RAW_SHAPES))
    def test_a_mixed_call_is_not_failed_by_one_corrupt_id(
        self, registry: StoreRegistry, shape: str
    ) -> None:
        insert_raw(registry.knowledge.document_store, CORRUPT_ID, *RAW_SHAPES[shape])
        with as_http(["read", "external"]):
            mixed = _fetch(["pub-1", CORRUPT_ID, MISSING_ID])
        assert "## [document] `pub-1`" in mixed
        assert f"`{CORRUPT_ID}`, `{MISSING_ID}`" in mixed

    @pytest.mark.parametrize("shape", list(RAW_SHAPES))
    def test_a_key_above_public_still_gets_the_fault(
        self, registry: StoreRegistry, shape: str
    ) -> None:
        insert_raw(registry.knowledge.document_store, CORRUPT_ID, *RAW_SHAPES[shape])
        with as_http(["read"]), pytest.raises(McpError):
            _fetch([CORRUPT_ID])


class TestAStoreFaultIsNotHidden:
    @pytest.mark.parametrize(
        "fault",
        [sqlite3.OperationalError("disk I/O error"), RuntimeError("store down")],
        ids=["io", "runtime"],
    )
    def test_an_outage_stays_an_error_for_an_external_key(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch, fault: Exception
    ) -> None:
        def down(doc_id: str) -> dict[str, Any]:
            raise fault

        monkeypatch.setattr(registry.knowledge.document_store, "get", down)
        with as_http(["read", "external"]):
            for ids in ([CORRUPT_ID], [MISSING_ID], ["pub-1"]):
                with pytest.raises(McpError):
                    _fetch(ids)
