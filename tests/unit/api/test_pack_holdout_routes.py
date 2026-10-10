"""A withheld pack reaches a REST caller as an ordinary empty pack.

``POST /api/v1/packs`` and ``POST /api/v1/packs/sectioned`` build through
:func:`~trellis.retrieve.builder_factory.build_pack_builder` (the SDK posts
to the same routes). A withheld response is the JSON the same route returns
for a corpus that holds nothing — no items, no advisories, an empty
withholding summary, a greenfield retrieval report — apart from its
``pack_id``, and none of it mentions a holdout. ``GET /api/version`` echoes
the rate in force, as it echoes every write-behaviour knob.

The rows the explore routes read back (``GET /api/v1/events`` and
``GET /api/v1/packs/{pack_id}``) keep the would-be pack for ``admin``
callers alone: any other key reads ``holdout`` and ``holdout_rate`` and no
other ``holdout_*`` key.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import (
    SCOPE_ADMIN,
    SCOPE_INGEST,
    SCOPE_MUTATE,
    SCOPE_READ,
    generate_api_key,
)
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app
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
                    evidence_confidence=1.0,
                ),
                scope="global",
            )
        )


def _post(
    client: TestClient, name: str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    route, body, _flat = _ROUTES[name]
    response = client.post(route, json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _without_pack_id(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "pack_id"}


def _steady(node: Any) -> Any:
    """``node`` with every ``original_score`` and ``duration_ms`` masked.

    ``original_score`` carries recency decay against the current time, so it
    moves between any two calls, flag or no flag. The ranking and the fused
    scores built from it hold still at these distinct keyword scores.
    ``duration_ms`` is real wall-clock elapsed time for that call's build, so
    two separately-executed builds (even of the same, empty, pack) are not
    expected to report the same value.
    """
    if isinstance(node, dict):
        return {
            key: "<decayed>"
            if key == "original_score"
            else "<elapsed>"
            if key == "duration_ms"
            else _steady(value)
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
        assert _steady(_without_pack_id(withheld)) == _steady(
            _without_pack_id(greenfield)
        )
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

    def test_negative_zero_is_recorded_as_zero(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``-0`` parses to a float equal to ``0.0`` that serialises as ``-0.0``.

        Equality cannot see the sign, so the recorded values are compared
        by it: on the ``PACK_ASSEMBLED`` row and in ``env_flags``.
        """
        monkeypatch.setenv(RATE_ENV, "-0")
        pack_id = _post(client, "flat")["pack_id"]
        version = client.get("/api/version").json()
        recorded = [
            _assembled(registry, pack_id)["holdout_rate"],
            version["write_provenance"]["env_flags"]["pack_holdout_rate"],
        ]
        assert recorded == [0.0, 0.0]
        assert [math.copysign(1.0, rate) for rate in recorded] == [1.0, 1.0]


_SHARED_SECRET = "synthetic-holdout-shared-secret"  # noqa: S105 - a test value

#: What a withheld pack's row records beyond a served pack's: the would-be
#: pack. Flat rows carry the first and last, sectioned rows the last two.
_WOULD_BE_KEYS = {"holdout_items", "holdout_sections", "holdout_advisory_ids"}

#: Each caller, and whether it holds ``admin``.
_CALLERS = {
    "read": False,
    "every-scope-but-admin": False,
    "admin": True,
    "shared-secret": True,
}


@dataclass
class _Scoped:
    client: TestClient
    headers: dict[str, dict[str, str]]
    held: list[str]
    served: str
    #: Each pack's ``PACK_ASSEMBLED`` payload, read from the event log.
    raw: dict[str, dict[str, Any]]


@pytest.fixture
def scoped(registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch) -> _Scoped:
    """The full app in ``required`` mode with withheld and served packs.

    One credential per caller: per-key records for the scoped callers and
    ``TRELLIS_API_KEY`` for the shared secret. The client is not entered as
    a context manager, because the lifespan would replace the fixture
    registry with one read from the real config dir (as in ``test_auth``).
    """
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    monkeypatch.setenv("TRELLIS_API_KEY", _SHARED_SECRET)
    headers = {"shared-secret": {"X-API-Key": _SHARED_SECRET}}
    for caller, scopes in {
        "read": [SCOPE_READ],
        "every-scope-but-admin": [SCOPE_READ, SCOPE_INGEST, SCOPE_MUTATE],
        "admin": [SCOPE_ADMIN],
    }.items():
        token, record = generate_api_key(caller, scopes)
        registry.operational.api_key_store.create(record)
        headers[caller] = {"X-API-Key": token}
    client = TestClient(create_app())

    _seed(registry)
    monkeypatch.setenv(RATE_ENV, "1")
    held = [_post(client, name, headers["admin"])["pack_id"] for name in _ROUTES]
    monkeypatch.setenv(RATE_ENV, "0")
    served = _post(client, "flat", headers["admin"])["pack_id"]
    raw = {pack_id: _assembled(registry, pack_id) for pack_id in [*held, served]}

    # Each withheld row records a would-be pack, and the served row none.
    flat, sectioned = (raw[pack_id] for pack_id in held)
    assert set(flat) & _WOULD_BE_KEYS == {"holdout_items", "holdout_advisory_ids"}
    assert set(sectioned) & _WOULD_BE_KEYS == {
        "holdout_sections",
        "holdout_advisory_ids",
    }
    assert all(
        row[key] for row in (flat, sectioned) for key in _WOULD_BE_KEYS & set(row)
    )
    assert not set(raw[served]) & _WOULD_BE_KEYS
    return _Scoped(client, headers, held, served, raw)


def _as_seen(raw: dict[str, Any], *, admin: bool) -> dict[str, Any]:
    """The payload a caller should read: the would-be pack is admin-only."""
    return raw if admin else {k: v for k, v in raw.items() if k not in _WOULD_BE_KEYS}


class TestOnlyAdminReadsTheWouldBePack:
    """A key that can read a withheld pack's row must not recover the pack.

    ``holdout`` and ``holdout_rate`` stay readable to every caller; the
    would-be keys reach ``admin`` callers and the shared secret only, so the
    operator view is unchanged.
    """

    @pytest.mark.parametrize("include_payload", [True, False])
    @pytest.mark.parametrize("caller", sorted(_CALLERS))
    def test_events_carry_the_would_be_pack_to_admin_only(
        self, caller: str, include_payload: bool, scoped: _Scoped
    ) -> None:
        response = scoped.client.get(
            "/api/v1/events",
            params={
                "event_type": EventType.PACK_ASSEMBLED.value,
                "include_payload": include_payload,
            },
            headers=scoped.headers[caller],
        )
        assert response.status_code == 200, response.text
        rows = {row["entity_id"]: row for row in response.json()["events"]}
        assert set(rows) == set(scoped.raw)
        for pack_id, raw in scoped.raw.items():
            expected = _as_seen(raw, admin=_CALLERS[caller])
            row = rows[pack_id]
            if include_payload:
                assert row["payload"] == expected, (caller, pack_id)
                assert row["payload"]["holdout"] is (pack_id in scoped.held)
            else:
                assert "payload" not in row
                assert row["payload_keys"] == sorted(expected), (caller, pack_id)

    @pytest.mark.parametrize("caller", sorted(_CALLERS))
    def test_pack_detail_carries_the_would_be_pack_to_admin_only(
        self, caller: str, scoped: _Scoped
    ) -> None:
        for pack_id, raw in scoped.raw.items():
            response = scoped.client.get(
                f"/api/v1/packs/{pack_id}", headers=scoped.headers[caller]
            )
            assert response.status_code == 200, response.text
            payload = response.json()["pack"]["payload"]
            assert payload == _as_seen(raw, admin=_CALLERS[caller]), (caller, pack_id)
            assert payload["holdout"] is (pack_id in scoped.held)
