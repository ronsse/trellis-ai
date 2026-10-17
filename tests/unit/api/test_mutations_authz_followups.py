"""Authorization follow-ups on ``/commands/batch`` (#883 review defects).

Three gaps the admin-scope review left:

* **An unknown operation 500'd.** ``_parse_operation`` now catches the
  ``ValueError`` and answers 422, naming the bad value -- a caller typo
  no longer looks like a server fault (mirrors ``routes/explore.py``'s
  ``_parse_event_type``).
* **A scope refusal wrote no log line.** ``execute_batch`` now emits one
  ``structlog`` warning per refusal, naming the route, the required
  scope and the refused operations -- never the key or principal.
* **``requested_by`` came from the body, not the principal.** With a
  principal authenticated, the audited ``requested_by`` on the
  governed-pipeline event is derived from it (``api:<key_id>``); the
  body's own claim rides along separately and never overwrites it. With
  auth off (no principal), current behaviour is unchanged: the claimed
  value passes through verbatim.

Fixture pattern mirrors ``tests/unit/api/test_settings_routes.py``: a
fresh in-memory ``StoreRegistry`` bound to ``trellis_api.app``, and a
``TestClient`` that is never entered as a context manager so the
lifespan never re-inits ``_registry`` out from under the fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

import trellis_api.app as app_module
from trellis.auth import SCOPE_ADMIN, SCOPE_MUTATE, generate_api_key
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app
from trellis_api.routes.mutations import _resolve_requested_by

# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Item 2: unknown operation is a 4xx, not a 500
# ---------------------------------------------------------------------------


class TestUnknownOperationIsRefusedNotCrashed:
    def test_unknown_operation_answers_422_naming_it(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json={"commands": [{"operation": "not.a.real.operation", "args": {}}]},
        )
        assert resp.status_code == 422, resp.text
        assert "not.a.real.operation" in resp.json()["detail"]

    def test_one_bad_operation_refuses_the_whole_batch_before_anything_runs(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json={
                "commands": [
                    {
                        "operation": "entity.create",
                        "args": {"entity_type": "service", "name": "should-not-land"},
                    },
                    {"operation": "not.a.real.operation", "args": {}},
                ]
            },
        )
        assert resp.status_code == 422, resp.text
        assert _executed_events(registry) == []


# ---------------------------------------------------------------------------
# Item 5: a scope refusal writes a log line, and never the key
# ---------------------------------------------------------------------------


class TestScopeRefusalIsLogged:
    def test_a_403_refusal_writes_one_warning_naming_scope_and_operation(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token, key_id = _mint_with_key_id(registry, [SCOPE_MUTATE])

        with capture_logs() as logs:
            resp = client.post(
                "/api/v1/commands/batch",
                json={"commands": [{"operation": "alias.upsert", "args": {}}]},
                headers={"X-API-Key": token},
            )
        assert resp.status_code == 403, resp.text

        warnings = [
            e
            for e in logs
            if e.get("log_level") == "warning"
            and e["event"] == "commands_batch_scope_refused"
        ]
        assert len(warnings) == 1, logs
        warning = warnings[0]
        assert warning["route"] == "/commands/batch"
        assert warning["required_scope"] == SCOPE_ADMIN
        assert "alias.upsert" in warning["operations"]

        # Never the key or principal.
        for entry in logs:
            rendered = repr(entry)
            assert token not in rendered
            assert key_id not in rendered


# ---------------------------------------------------------------------------
# Item 6: requested_by is audited from the principal, not the body
# ---------------------------------------------------------------------------


class TestRequestedByIsAuditedFromThePrincipal:
    def test_a_claimed_requested_by_is_overridden_by_the_authenticated_key(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token, key_id = _mint_with_key_id(registry, [SCOPE_MUTATE])

        resp = client.post(
            "/api/v1/commands/batch",
            json={
                "commands": [
                    {
                        "operation": "entity.create",
                        "args": {"entity_type": "service", "name": "audited"},
                    }
                ],
                "requested_by": "attacker-chosen-label",
            },
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200, resp.text

        (event,) = _executed_events(registry)
        assert event.payload["requested_by"] == f"api:{key_id}"
        assert event.payload["requested_by"] != "attacker-chosen-label"

    def test_with_auth_off_the_claimed_value_is_kept_unchanged(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json={
                "commands": [
                    {
                        "operation": "entity.create",
                        "args": {"entity_type": "service", "name": "anon"},
                    }
                ],
                "requested_by": "anon-claim",
            },
        )
        assert resp.status_code == 200, resp.text

        (event,) = _executed_events(registry)
        assert event.payload["requested_by"] == "anon-claim"


class TestResolveRequestedByUnit:
    """Direct coverage of ``_resolve_requested_by``'s two branches.

    The route-level tests above prove the audited identity; this proves
    the claim-preservation contract that never reaches the generic
    ``MUTATION_EXECUTED`` payload (``executor._emit_event`` only reads
    ``command.requested_by``, never ``command.metadata`` -- true for
    every operation without a bespoke semantic event, unlike
    ``settings.py``'s ``SETTINGS_UPDATED``) but is still the thing #6
    requires: the claim survives as a separate, clearly-labelled field.
    """

    def test_with_a_principal_the_claim_is_kept_separately(self) -> None:
        from trellis_api.auth import AuthContext

        ctx = AuthContext(
            key_id="abc123", name=None, scopes=[SCOPE_MUTATE], mode="required"
        )
        requested_by, claim = _resolve_requested_by(ctx, "someone-else")
        assert requested_by == "api:abc123"
        assert claim == "someone-else"

    def test_with_no_principal_the_claim_passes_through_and_nothing_is_kept(
        self,
    ) -> None:
        from trellis.auth import ALL_SCOPES
        from trellis_api.auth import AuthContext

        ctx = AuthContext(key_id=None, name=None, scopes=ALL_SCOPES, mode="off")
        requested_by, claim = _resolve_requested_by(ctx, "anon-claim")
        assert requested_by == "anon-claim"
        assert claim is None
