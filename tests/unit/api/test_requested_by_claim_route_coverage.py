"""Route-level coverage of ``resolve_requested_by`` beyond ``/commands/batch``.

``trellis_api.routes._principal.resolve_requested_by`` is called from six
resolver routes: ``/precedents`` (promote), ``/precedents/{id}/demote``,
``/precedents/{id}/reinstate``, ``/ingest/bulk``, ``/extract/drafts`` and
``/commands/batch``. The AST rule in
``tests/unit/api/test_requested_by_principal_rule.py`` proves every call
site in the route source resolves through the helper rather than trusting
the body; this file proves the *runtime* behaviour end to end for the five
routes ``test_mutations_authz_followups.py`` does not already cover: with
a real authenticated key, a hostile ``requested_by`` in the body never
reaches the audit trail's ``requested_by`` field (``api:<key_id>`` always
wins), and the hostile value still rides along, unaltered, as
``requested_by_claim`` on the emitted event -- so the claim is preserved
for forensics without being trusted.

Fixture pattern mirrors ``tests/unit/api/test_mutations_authz_followups.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import SCOPE_INGEST, SCOPE_MUTATE, generate_api_key
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

_HOSTILE_CLAIM = "auto:learning_candidate"  # the forgery this helper exists to stop


@pytest.fixture
def registry(tmp_path: Path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    """In-memory app over the fixture registry (not context-managed)."""
    app = create_app()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_AUTH_MODE", raising=False)


@pytest.fixture(autouse=True)
def _require_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")


def _mint_with_key_id(
    registry: StoreRegistry, scopes: list[str], name: str = "test-key"
) -> tuple[str, str]:
    token, record = generate_api_key(name, scopes)
    registry.operational.api_key_store.create(record)
    return token, record.key_id


def _executed_events(registry: StoreRegistry) -> list:
    return registry.operational.event_log.get_events(
        event_type=EventType.MUTATION_EXECUTED, order="asc", limit=100
    )


def _one_event_for_operation(events: list, operation: str) -> object:
    """The single MUTATION_EXECUTED event for *operation*.

    The event payload carries no ``target_id``/``created_id`` key -- the
    created or targeted id is embedded only in the human-readable
    ``message`` string (e.g. ``"Precedent demoted: 01M5..."``). Filtering
    by ``operation`` is the same pattern ``executor.py`` itself uses and
    is reliable wherever a test's setup does not also exercise the same
    operation.
    """
    matches = [e for e in events if e.payload.get("operation") == operation]
    assert len(matches) == 1, (operation, [e.payload for e in events])
    return matches[0]


class TestPromoteAuditsThePrincipalNotTheClaim:
    def test_promote_requested_by_is_the_key_claim_is_preserved(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        token, key_id = _mint_with_key_id(registry, [SCOPE_MUTATE, SCOPE_INGEST])

        traced = client.post(
            "/api/v1/traces",
            json={
                "source": "agent",
                "intent": "fix the import",
                "steps": [{"step_type": "tool_call", "name": "grep"}],
                "context": {"agent_id": "a1", "domain": "backend"},
            },
            headers={"X-API-Key": token},
        )
        assert traced.status_code == 200, traced.text
        trace_id = traced.json()["trace_id"]

        resp = client.post(
            "/api/v1/precedents",
            json={
                "trace_id": trace_id,
                "title": "Pooling pattern",
                "description": "d",
                "requested_by": _HOSTILE_CLAIM,
            },
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200, resp.text

        events = _executed_events(registry)
        event = _one_event_for_operation(events, "precedent.promote")
        assert event.payload["requested_by"] == f"api:{key_id}"
        assert event.payload["requested_by_claim"] == _HOSTILE_CLAIM

    def test_promote_with_no_requested_by_in_the_body_records_no_claim(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """The DTO's own default (``"api:promote"``) is not a claim.

        When the body never mentions ``requested_by`` at all, the request
        model falls back to its own default rather than anything the
        caller wrote -- ``resolve_requested_by``'s ``claim_was_set`` gate
        (item 4) must keep that default off ``requested_by_claim``,
        otherwise every unclaimed promote would misreport the DTO default
        as something the caller claimed.
        """
        token, key_id = _mint_with_key_id(registry, [SCOPE_MUTATE, SCOPE_INGEST])

        traced = client.post(
            "/api/v1/traces",
            json={
                "source": "agent",
                "intent": "fix the import",
                "steps": [{"step_type": "tool_call", "name": "grep"}],
                "context": {"agent_id": "a1", "domain": "backend"},
            },
            headers={"X-API-Key": token},
        )
        assert traced.status_code == 200, traced.text
        trace_id = traced.json()["trace_id"]

        resp = client.post(
            "/api/v1/precedents",
            json={
                "trace_id": trace_id,
                "title": "Pooling pattern, unclaimed",
                "description": "d",
            },
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200, resp.text

        events = _executed_events(registry)
        event = _one_event_for_operation(events, "precedent.promote")
        assert event.payload["requested_by"] == f"api:{key_id}"
        assert "requested_by_claim" not in event.payload


class TestDemoteAuditsThePrincipalNotTheClaim:
    def _create_precedent(self, client: TestClient, token: str) -> str:
        resp = client.post(
            "/api/v1/entities",
            json={"entity_type": "precedent", "name": "Pooling pattern"},
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200, resp.text
        node_id: str = resp.json()["node_id"]
        return node_id

    def test_demote_requested_by_is_the_key_claim_is_preserved(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        token, key_id = _mint_with_key_id(registry, [SCOPE_MUTATE, SCOPE_INGEST])
        node_id = self._create_precedent(client, token)

        resp = client.post(
            f"/api/v1/precedents/{node_id}/demote",
            json={
                "reason": "superseded by a broader pattern",
                "requested_by": _HOSTILE_CLAIM,
            },
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200, resp.text

        events = _executed_events(registry)
        event = _one_event_for_operation(events, "precedent.demote")
        assert event.payload["requested_by"] == f"api:{key_id}"
        assert event.payload["requested_by_claim"] == _HOSTILE_CLAIM


class TestReinstateAuditsThePrincipalNotTheClaim:
    def _create_demoted_precedent(self, client: TestClient, token: str) -> str:
        created = client.post(
            "/api/v1/entities",
            json={"entity_type": "precedent", "name": "Pooling pattern"},
            headers={"X-API-Key": token},
        )
        assert created.status_code == 200, created.text
        node_id: str = created.json()["node_id"]
        demoted = client.post(
            f"/api/v1/precedents/{node_id}/demote",
            json={"reason": "superseded"},
            headers={"X-API-Key": token},
        )
        assert demoted.status_code == 200, demoted.text
        return node_id

    def test_reinstate_requested_by_is_the_key_claim_is_preserved(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        token, key_id = _mint_with_key_id(registry, [SCOPE_MUTATE, SCOPE_INGEST])
        node_id = self._create_demoted_precedent(client, token)

        resp = client.post(
            f"/api/v1/precedents/{node_id}/reinstate",
            json={
                "reason": "the broader pattern was withdrawn",
                "requested_by": _HOSTILE_CLAIM,
            },
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200, resp.text

        events = _executed_events(registry)
        event = _one_event_for_operation(events, "precedent.reinstate")
        assert event.payload["requested_by"] == f"api:{key_id}"
        assert event.payload["requested_by_claim"] == _HOSTILE_CLAIM


class TestIngestBulkAuditsThePrincipalNotTheClaim:
    def test_ingest_bulk_requested_by_is_the_key_claim_is_preserved(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        token, key_id = _mint_with_key_id(registry, [SCOPE_MUTATE, SCOPE_INGEST])

        resp = client.post(
            "/api/v1/ingest/bulk",
            json={
                "entities": [
                    {
                        "entity_type": "service",
                        "name": "auth",
                        "entity_id": "svc-auth-claim-test",
                    },
                ],
                "requested_by": _HOSTILE_CLAIM,
            },
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200, resp.text

        events = _executed_events(registry)
        event = _one_event_for_operation(events, "entity.create")
        assert event.payload["requested_by"] == f"api:{key_id}"
        assert event.payload["requested_by_claim"] == _HOSTILE_CLAIM


class TestExtractDraftsAuditsThePrincipalNotTheClaim:
    def test_extract_drafts_requested_by_is_the_key_claim_is_preserved(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        token, key_id = _mint_with_key_id(registry, [SCOPE_MUTATE, SCOPE_INGEST])

        resp = client.post(
            "/api/v1/extract/drafts",
            json={
                "batch": {
                    "source": "unity_catalog",
                    "extractor_name": "test_extractor",
                    "extractor_version": "0.1.0",
                    "entities": [
                        {
                            "entity_type": "unity_catalog.table",
                            "name": "sales.orders",
                        },
                    ],
                },
                "requested_by": _HOSTILE_CLAIM,
            },
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200, resp.text

        events = _executed_events(registry)
        event = _one_event_for_operation(events, "entity.create")
        assert event.payload["requested_by"] == f"api:{key_id}"
        assert event.payload["requested_by_claim"] == _HOSTILE_CLAIM
