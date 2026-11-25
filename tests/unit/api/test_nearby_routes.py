"""The nearby section reaches a REST caller, and through it the SDK.

``POST /api/v1/packs`` and ``POST /api/v1/packs/sectioned`` return
``pack.metadata["nearby"]`` as a top-level ``nearby`` field
(:class:`trellis_wire.dtos.NearbyResponse`), ``null`` when the pack has none.
The builder is the one ``tests/unit/retrieve/test_nearby.py`` drives, swapped
in for the route's factory call.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import trellis_api.app as app_module
from tests.unit.retrieve.test_nearby import WEAK_IDS, _builder
from trellis.retrieve.formatters import (
    format_pack_as_markdown,
    format_sectioned_pack_as_markdown,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import retrieve
from trellis_sdk._format import format_pack_as_markdown as sdk_format_pack
from trellis_sdk._format import (
    format_sectioned_pack_as_markdown as sdk_format_sectioned,
)
from trellis_sdk._format import nearby_of
from trellis_wire.dtos import NearbyResponse, PackResponse, SectionedPackResponse

FLAT = ("/api/v1/packs", {"intent": "q"})
SECTIONED = ("/api/v1/packs/sectioned", {"intent": "q", "sections": [{"name": "all"}]})


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[StoreRegistry]:
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> Iterator[TestClient]:
    @asynccontextmanager
    async def noop_lifespan(app: FastAPI) -> Any:
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(retrieve.router, prefix="/api/v1", tags=["retrieve"])
    with TestClient(app) as c:
        yield c


class _Captured:
    """The builder the route uses, and every pack it built."""

    def __init__(self, builder: PackBuilder) -> None:
        self.builder = builder
        self.packs: list[Any] = []
        build, build_sectioned = builder.build, builder.build_sectioned

        def _build(*a: Any, **k: Any) -> Any:
            self.packs.append(build(*a, **k))
            return self.packs[-1]

        def _build_sectioned(*a: Any, **k: Any) -> Any:
            self.packs.append(build_sectioned(*a, **k))
            return self.packs[-1]

        builder.build = _build  # type: ignore[method-assign]
        builder.build_sectioned = _build_sectioned  # type: ignore[method-assign]


def _use(
    monkeypatch: pytest.MonkeyPatch, *, nearby: str, holdout_rate: float = 0.0
) -> _Captured:
    captured = _Captured(_builder(nearby=nearby, holdout_rate=holdout_rate))
    monkeypatch.setattr(retrieve, "_build_pack_builder", lambda _r: captured.builder)
    return captured


def _post(client: TestClient, route: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    response = client.post(route[0], json=route[1])
    assert response.status_code == 200, response.text
    return response.json()


def _section(text: str) -> str:
    start = text.index("## Nearby")
    end = text.find("\n\n", start)
    return text[start:] if end == -1 else text[start:end]


class TestFlatRoute:
    def test_section_on_and_cut_returns_the_block(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _use(monkeypatch, nearby="on")
        body = _post(client, FLAT)
        (pack,) = captured.packs
        assert body["nearby"] == pack.metadata["nearby"]
        assert set(body["nearby"]["listed_item_ids"]) == WEAK_IDS
        assert body["nearby"]["markdown"].startswith("## Nearby")

    def test_section_off_is_null(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use(monkeypatch, nearby="off")
        body = _post(client, FLAT)
        assert "nearby" in body
        assert body["nearby"] is None
        # Nothing else gained a key: the response is the DTO's fields.
        assert set(body) == set(PackResponse.model_fields)

    def test_held_out_pack_has_no_nearby(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use(monkeypatch, nearby="on", holdout_rate=1.0)
        body = _post(client, FLAT)
        assert body["nearby"] is None
        assert not [i for i in WEAK_IDS if i in json.dumps(body)]

    def test_sdk_prints_the_core_section_from_the_rest_json(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _use(monkeypatch, nearby="on")
        body = _post(client, FLAT)
        (pack,) = captured.packs
        items = [
            {"item_id": i.item_id, "item_type": i.item_type, "excerpt": i.excerpt}
            for i in pack.items
        ]
        core = format_pack_as_markdown(
            items, "q", pack_id=pack.pack_id, nearby=pack.metadata["nearby"]
        )
        sdk = sdk_format_pack(
            body["items"], "q", pack_id=body["pack_id"], nearby=nearby_of(body)
        )
        assert _section(sdk) == _section(core) == pack.metadata["nearby"]["markdown"]


class TestSectionedRoute:
    def test_section_on_returns_the_block(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _use(monkeypatch, nearby="on")
        body = _post(client, SECTIONED)
        (pack,) = captured.packs
        assert body["nearby"] == pack.metadata["nearby"]
        core = format_sectioned_pack_as_markdown(
            [], "q", pack_id=pack.pack_id, nearby=pack.metadata["nearby"]
        )
        sdk = sdk_format_sectioned([], "q", nearby=nearby_of(body))
        assert _section(sdk) == _section(core)

    def test_section_off_is_null(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use(monkeypatch, nearby="off")
        body = _post(client, SECTIONED)
        assert body["nearby"] is None
        assert set(body) == set(SectionedPackResponse.model_fields)

    def test_held_out_pack_has_no_nearby(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use(monkeypatch, nearby="on", holdout_rate=1.0)
        assert _post(client, SECTIONED)["nearby"] is None


class TestNearbyResponse:
    def test_round_trips_the_block_exactly(self) -> None:
        pack = _builder(nearby="on").build("q", clearance="restricted")
        block = pack.metadata["nearby"]
        assert NearbyResponse.model_validate(block).model_dump() == block

    def test_strings_are_not_stripped(self) -> None:
        pack = _builder(nearby="on").build("q", clearance="restricted")
        block = {**pack.metadata["nearby"], "markdown": " x \n"}
        assert NearbyResponse.model_validate(block).markdown == " x \n"

    def test_unknown_key_is_refused(self) -> None:
        pack = _builder(nearby="on").build("q", clearance="restricted")
        with pytest.raises(ValidationError):
            NearbyResponse.model_validate({**pack.metadata["nearby"], "extra": 1})


class TestNearbyOf:
    def test_top_level_key_wins_and_null_means_none(self) -> None:
        block = {"markdown": "top"}
        assert nearby_of({"nearby": block}) == block
        assert (
            nearby_of({"nearby": None, "metadata": {"nearby": {"markdown": "m"}}})
            is None
        )

    def test_metadata_is_the_fallback(self) -> None:
        assert nearby_of({"metadata": {"nearby": {"markdown": "m"}}}) == {
            "markdown": "m"
        }
        assert nearby_of({}) is None
