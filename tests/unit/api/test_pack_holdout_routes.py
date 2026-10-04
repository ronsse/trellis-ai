"""A withheld pack reaches a REST caller as an ordinary empty pack.

``POST /api/v1/packs`` and ``POST /api/v1/packs/sectioned`` build through
:func:`~trellis.retrieve.builder_factory.build_pack_builder` (the SDK posts
to the same routes). A withheld response is the JSON the same route returns
for a corpus that holds nothing — no items, no advisories, an empty
withholding summary, a greenfield retrieval report — apart from its
``pack_id``, and none of it mentions a holdout. ``GET /api/version`` echoes
the rate in force, as it echoes every write-behaviour knob.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import retrieve
from trellis_api.routes import version as version_route

RATE_ENV = "TRELLIS_PACK_HOLDOUT_RATE"
INTENT = "failover runbook"

_TELLS = (
    "holdout",
    "hold-out",
    "held out",
    "held-out",
    "withheld",
    "experiment",
    "treatment",
    "randomi",
    "control arm",
)

_SUBJECTS = (
    "replication lag",
    "connection pooling",
    "vacuum scheduling",
    "wal shipping",
)

#: (route, request body, flat?) for each pack route.
_ROUTES = {
    "flat": ("/api/v1/packs", {"intent": INTENT}, True),
    "sectioned": (
        "/api/v1/packs/sectioned",
        {
            "intent": INTENT,
            "sections": [
                {"name": "patterns", "content_types": ["pattern"]},
                {"name": "configuration", "content_types": ["configuration"]},
            ],
        },
        False,
    ),
}


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[StoreRegistry]:
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(
    registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.delenv(RATE_ENV, raising=False)

    @asynccontextmanager
    async def noop_lifespan(app: FastAPI) -> Any:
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(retrieve.router, prefix="/api/v1", tags=["retrieve"])
    app.include_router(version_route.router)
    with TestClient(app) as c:
        yield c


def _body(subject: str, mentions: int) -> str:
    """``mentions`` of the intent in a body of near-constant length.

    Distinct counts keep the documents' keyword scores apart. Repeating one
    sentence per document instead tied them to within float noise, so two
    consecutive builds could rank them in different orders.
    """
    mention = f"failover runbook for {subject}: promote the replica. "
    filler = f"then restart the {subject} sidecar once the write queue drains. "
    return mention * mentions + filler * (8 - mentions)


def _seed(registry: StoreRegistry) -> None:
    store = registry.knowledge.document_store
    for i, subject in enumerate(_SUBJECTS):
        content_type = "pattern" if i % 2 == 0 else "configuration"
        store.put(
            f"doc-{i}",
            _body(subject, mentions=i + 2),
            {"title": f"Runbook {i}", "content_tags": {"content_type": content_type}},
        )
    advisories = AdvisoryStore(registry.stores_dir / "advisories.json")
    for advisory_id, confidence, category in [
        ("adv-entity", 0.82, AdvisoryCategory.ENTITY),
        ("adv-approach", 0.61, AdvisoryCategory.APPROACH),
    ]:
        advisories.put(
            Advisory(
                advisory_id=advisory_id,
                category=category,
                confidence=confidence,
                message=f"Synthetic advisory {advisory_id}",
                evidence=AdvisoryEvidence(
                    sample_size=9,
                    success_rate_with=0.7,
                    success_rate_without=0.4,
                    effect_size=0.3,
                ),
                scope="global",
            )
        )


def _post(client: TestClient, name: str) -> dict[str, Any]:
    route, body, _flat = _ROUTES[name]
    response = client.post(route, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _without_pack_id(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "pack_id"}


def _steady(node: Any) -> Any:
    """``node`` with every ``original_score`` masked.

    That raw score carries recency decay against the current time, so it
    moves between any two calls, flag or no flag. The ranking and the fused
    scores built from it hold still at these distinct keyword scores.
    """
    if isinstance(node, dict):
        return {
            key: "<decayed>" if key == "original_score" else _steady(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_steady(value) for value in node]
    return node


def _strings(node: Any) -> list[str]:
    """Every string value in a JSON body, keys excluded."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [text for value in node.values() for text in _strings(value)]
    if isinstance(node, list):
        return [text for value in node for text in _strings(value)]
    return []


def _assembled(registry: StoreRegistry, pack_id: str) -> dict[str, Any]:
    events = [
        event
        for event in registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=100
        )
        if event.entity_id == pack_id
    ]
    assert len(events) == 1, events
    return events[0].payload


def _item_ids(body: dict[str, Any], *, flat: bool) -> list[list[str]]:
    if flat:
        return [sorted(item["item_id"] for item in body["items"])]
    return [sorted(item["item_id"] for item in s["items"]) for s in body["sections"]]


class TestAWithheldPackReadsAsAnEmptyPack:
    @pytest.mark.parametrize("name", sorted(_ROUTES))
    def test_the_response_is_an_empty_packs_response(
        self,
        name: str,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        flat = _ROUTES[name][2]
        monkeypatch.setenv(RATE_ENV, "1e-12")
        greenfield = _post(client, name)

        _seed(registry)
        monkeypatch.setenv(RATE_ENV, "0")
        control = _post(client, name)
        monkeypatch.setenv(RATE_ENV, "1")
        withheld = _post(client, name)

        # The seeded corpus answers: items in every section, two advisories.
        assert all(_item_ids(control, flat=flat))
        assert [a["advisory_id"] for a in control["advisories"]] == [
            "adv-entity",
            "adv-approach",
        ]
        # The withheld response is the greenfield one, pack id aside ...
        assert _without_pack_id(withheld) == _without_pack_id(greenfield)
        assert withheld["advisories"] == []
        assert withheld["withholding"]["total"] == 0
        # ... names its own pack ...
        assert len(withheld["pack_id"]) == 26
        assert withheld["pack_id"] not in {greenfield["pack_id"], control["pack_id"]}
        # ... and says nothing about why it is empty. The keys are pinned by
        # the equality above: the #404 summary every pack carries spells
        # ``withheld_item_ids``, so the tells are looked for in the values.
        lowered = " ".join(_strings(withheld)).lower()
        assert "failover runbook" in lowered
        assert [tell for tell in _TELLS if tell in lowered] == []

    @pytest.mark.parametrize("name", sorted(_ROUTES))
    def test_pack_assembled_keeps_the_would_be_pack_apart(
        self,
        name: str,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        flat = _ROUTES[name][2]
        _seed(registry)
        monkeypatch.setenv(RATE_ENV, "0")
        control_body = _post(client, name)
        monkeypatch.setenv(RATE_ENV, "1")
        held_body = _post(client, name)
        control = _assembled(registry, control_body["pack_id"])
        held = _assembled(registry, held_body["pack_id"])

        assert (control["holdout"], held["holdout"]) == (False, True)
        assert (control["holdout_rate"], held["holdout_rate"]) == (0.0, 1.0)
        assert held["advisory_ids"] == []
        assert (
            held["holdout_advisory_ids"]
            == control["advisory_ids"]
            == [
                "adv-entity",
                "adv-approach",
            ]
        )
        served = _item_ids(control_body, flat=flat)
        if flat:
            assert held["injected_items"] == []
            assert held["injected_item_ids"] == []
            assert [sorted(r["item_id"] for r in held["holdout_items"])] == served
        else:
            assert all(s["item_ids"] == [] for s in held["sections"])
            assert [sorted(s["item_ids"]) for s in held["holdout_sections"]] == served


class TestRateZeroIsTransparent:
    @pytest.mark.parametrize("name", sorted(_ROUTES))
    def test_a_zero_rate_returns_what_an_unset_one_does(
        self,
        name: str,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed(registry)
        unset = _post(client, name)
        monkeypatch.setenv(RATE_ENV, "0")
        zero = _post(client, name)

        assert _steady(_without_pack_id(zero)) == _steady(_without_pack_id(unset))
        assert "<decayed>" in str(_steady(zero))
        assert zero["advisories"]
        for body in (unset, zero):
            payload = _assembled(registry, body["pack_id"])
            assert (payload["holdout"], payload["holdout_rate"]) == (False, 0.0)
            assert not {
                "holdout_items",
                "holdout_sections",
                "holdout_advisory_ids",
            } & set(payload)


class TestVersionEchoesTheRate:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(None, 0.0), ("0.25", 0.25), ("1", 1.0), ("-0.5", 0.0), ("lots", 0.0)],
    )
    def test_env_flags_carry_the_rate_in_force(
        self,
        raw: str | None,
        expected: float,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if raw is not None:
            monkeypatch.setenv(RATE_ENV, raw)
        body = client.get("/api/version").json()
        assert body["write_provenance"]["env_flags"]["pack_holdout_rate"] == expected
