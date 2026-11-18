"""The REST observation, measurement and precedent listings judge a node's label.

Follow-up to ``test_node_label_routes.py`` (#1029): ``GET /observations``,
``GET /measurements``, ``GET /precedents`` and ``GET /precedents/domains`` now
leave out a node whose own ``data_classification`` property is above the key's
clearance (#1264). A node without the key is served at every clearance, and a
malformed record reads ``restricted``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import SCOPE_CONFIDENTIAL, SCOPE_RESTRICTED, generate_api_key
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY
from trellis.schemas.well_known import MEASUREMENT, OBSERVATION
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

SUBJECT = "entity://subject"
OBSERVER = "agent-one"
#: Each fixture node's ``data_classification`` record, or ``None`` for none.
LABELS: dict[str, Any] = {
    "secret": {"sensitivity": "restricted"},
    "conf": {"sensitivity": "confidential"},
    "bad": "secret-ish",
    "plain": None,
}
INTERNAL = ["read"]
CONFIDENTIAL = ["read", SCOPE_CONFIDENTIAL]
RESTRICTED = ["read", SCOPE_RESTRICTED]
#: The fixture keys each scope set is served.
SERVED = {
    "internal": {"plain"},
    "confidential": {"plain", "conf"},
    "restricted": {"plain", "conf", "secret", "bad"},
}
SCOPES = {"internal": INTERNAL, "confidential": CONFIDENTIAL, "restricted": RESTRICTED}


def _props(key: str, **extra: Any) -> dict[str, Any]:
    props: dict[str, Any] = dict(extra)
    if LABELS[key] is not None:
        props[DATA_CLASSIFICATION_KEY] = LABELS[key]
    return props


def _seed(registry: StoreRegistry) -> None:
    graph = registry.knowledge.graph_store
    log = registry.operational.event_log
    for key in LABELS:
        graph.upsert_node(
            f"obs-{key}",
            OBSERVATION,
            _props(
                key,
                observation_id=f"obs-{key}",
                subject_entity_id=SUBJECT,
                observer_agent_id=OBSERVER,
                summary=f"observed {key}",
            ),
        )
        graph.upsert_node(
            f"meas-{key}",
            MEASUREMENT,
            _props(
                key,
                measurement_id=f"meas-{key}",
                subject_entity_id=SUBJECT,
                metric_name="latency",
                observer_agent_id=OBSERVER,
            ),
        )
        graph.upsert_node(
            f"prec-{key}",
            "precedent",
            _props(key, name=f"lesson {key}", description=f"about {key}"),
        )
        log.emit(
            EventType.PRECEDENT_PROMOTED,
            "mutation_executor",
            entity_id=f"prec-{key}",
            entity_type="precedent",
            payload={
                "title": f"lesson {key}",
                "description": f"about {key}",
                "domain": f"domain-{key}",
                "trace_id": f"trace-{key}",
            },
        )


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    _seed(reg)
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    return TestClient(create_app())


def _get(client, registry, path: str, scopes: list[str], **params: Any) -> Any:
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    resp = client.get(
        f"/api/v1{path}",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _keys(ids: list[str], prefix: str) -> set[str]:
    return {i.removeprefix(prefix) for i in ids}


@pytest.mark.parametrize("level", sorted(SCOPES))
def test_observations_withhold_a_node_labelled_above_the_key(
    client, registry, level: str
) -> None:
    body = _get(client, registry, "/observations", SCOPES[level], limit=50)
    served = _keys([r["node_id"] for r in body["observations"]], "obs-")
    assert served == SERVED[level]


@pytest.mark.parametrize("level", sorted(SCOPES))
def test_measurements_withhold_a_node_labelled_above_the_key(
    client, registry, level: str
) -> None:
    body = _get(client, registry, "/measurements", SCOPES[level], limit=50)
    served = _keys([r["node_id"] for r in body["measurements"]], "meas-")
    assert served == SERVED[level]


@pytest.mark.parametrize("level", sorted(SCOPES))
def test_precedent_listing_withholds_a_node_labelled_above_the_key(
    client, registry, level: str
) -> None:
    body = _get(client, registry, "/precedents", SCOPES[level], limit=50)
    served = _keys([r["entity_id"] for r in body["precedents"]], "prec-")
    assert served == SERVED[level]
    assert body["count"] == len(body["precedents"]), "count agrees with the rows"
    text = str(body)
    for key in set(LABELS) - SERVED[level]:
        assert f"lesson {key}" not in text
        assert f"about {key}" not in text


@pytest.mark.parametrize("level", sorted(SCOPES))
def test_precedent_domains_do_not_count_a_withheld_node(
    client, registry, level: str
) -> None:
    body = _get(client, registry, "/precedents/domains", SCOPES[level])
    domains = {d["domain"]: d["count"] for d in body["domains"]}
    assert domains == {f"domain-{key}": 1 for key in SERVED[level]}
    assert body["total"] == len(SERVED[level])


def test_a_withheld_node_does_not_take_a_slot_from_a_servable_one(
    client, registry
) -> None:
    # The listing is newest first and ``plain`` was promoted last; a labelled
    # promotion on top of it means a cap of one must look past the withheld row.
    registry.knowledge.graph_store.upsert_node(
        "prec-top",
        "precedent",
        {"name": "lesson top", DATA_CLASSIFICATION_KEY: {"sensitivity": "restricted"}},
    )
    registry.operational.event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        "mutation_executor",
        entity_id="prec-top",
        entity_type="precedent",
        payload={"title": "lesson top", "description": "x", "trace_id": "t"},
    )
    body = _get(client, registry, "/precedents", INTERNAL, limit=1)
    assert [r["entity_id"] for r in body["precedents"]] == ["prec-plain"]
