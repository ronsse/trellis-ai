"""End-to-end coverage of the reserved-metadata guard on ``/commands/batch``.

``_build_command`` (``trellis_api/routes/mutations.py``) used to build each
item's ``Command.metadata`` as a plain ``dict(item.metadata)`` -- a caller's
own dict, copied whole. Several keys on ``Command.metadata`` are read as
authoritative rather than opaque decoration (see
``trellis/mutate/reserved_metadata.py``'s module docstring for every read
site), so an unguarded copy let any caller -- even one with only the base
``mutate`` scope -- plant a ``requested_by_claim``, ``key_id``, ``key_name``,
``write_provenance`` or ``handler_warnings`` straight into the audited
event. ``_build_command`` now calls
:func:`trellis.mutate.reject_reserved_metadata` on the caller's own
``metadata`` before merging its own ``requested_by_claim`` override; this
file proves the batch is refused whole, with 400 naming the key, before
any command executes, and that nothing else changed: an honest body
``requested_by`` still records its claim, and ordinary decoration metadata
still passes.

Fixture pattern mirrors ``tests/unit/api/test_mutations_authz_followups.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import SCOPE_ADMIN, SCOPE_MUTATE, generate_api_key
from trellis.mutate import RESERVED_COMMAND_METADATA_KEYS
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app


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


def _batch_body(metadata: dict, requested_by: str | None = None) -> dict:
    body: dict = {
        "commands": [
            {
                "operation": "entity.create",
                "args": {"entity_type": "service", "name": "probe"},
                "metadata": metadata,
            }
        ],
    }
    if requested_by is not None:
        body["requested_by"] = requested_by
    return body


class TestAPlantedReservedKeyIsRefusedWholeNamingItself:
    """Item 4(a)/(b): a planted reserved key is refused with 400 naming
    the key -- no auth required and under an admin-scoped key alike,
    since the guard runs regardless of scope."""

    def test_a_planted_requested_by_claim_without_a_body_requested_by_is_refused(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json=_batch_body({"requested_by_claim": "forged-actor"}),
        )
        assert resp.status_code == 400, resp.text
        assert "requested_by_claim" in resp.json()["detail"]
        assert _executed_events(registry) == []

    def test_a_planted_key_id_under_an_admin_key_is_refused(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token, _key_id = _mint_with_key_id(registry, [SCOPE_MUTATE, SCOPE_ADMIN])

        resp = client.post(
            "/api/v1/commands/batch",
            json=_batch_body({"key_id": "attacker-chosen-key-id"}),
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 400, resp.text
        assert "key_id" in resp.json()["detail"]
        assert _executed_events(registry) == []

    @pytest.mark.parametrize("key", sorted(RESERVED_COMMAND_METADATA_KEYS))
    def test_every_reserved_key_is_refused_naming_itself(
        self, client: TestClient, registry: StoreRegistry, key: str
    ) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json=_batch_body({key: "forged-value"}),
        )
        assert resp.status_code == 400, resp.text
        assert key in resp.json()["detail"]
        assert _executed_events(registry) == []

    def test_a_second_command_is_never_reached_when_the_first_is_refused(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """The whole batch is refused before any command executes -- a
        planted key on item 0 must not let item 1 land."""
        resp = client.post(
            "/api/v1/commands/batch",
            json={
                "commands": [
                    {
                        "operation": "entity.create",
                        "args": {"entity_type": "service", "name": "forged"},
                        "metadata": {"key_name": "attacker-chosen"},
                    },
                    {
                        "operation": "entity.create",
                        "args": {"entity_type": "service", "name": "should-not-land"},
                    },
                ]
            },
        )
        assert resp.status_code == 400, resp.text
        assert _executed_events(registry) == []


class TestBatchLevelMetadataIsGuardedToo:
    """``CommandBatch.metadata`` (``req.metadata`` -- distinct from any one
    command's own) is caller-supplied the same way, and gets the same
    guard in ``execute_batch`` even though no handler reads it today."""

    def test_a_planted_key_in_batch_metadata_is_refused_naming_the_key(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json={
                "commands": [
                    {
                        "operation": "entity.create",
                        "args": {"entity_type": "service", "name": "probe"},
                    }
                ],
                "metadata": {"write_provenance": "forged-build"},
            },
        )
        assert resp.status_code == 400, resp.text
        assert "write_provenance" in resp.json()["detail"]
        assert _executed_events(registry) == []

    def test_ordinary_batch_metadata_still_passes(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json={
                "commands": [
                    {
                        "operation": "entity.create",
                        "args": {"entity_type": "service", "name": "probe"},
                    }
                ],
                "metadata": {"note": "decoration"},
            },
        )
        assert resp.status_code == 200, resp.text
        assert len(_executed_events(registry)) == 1


class TestHonestUseIsUnaffected:
    """Item 4(c)/(d): the guard changes nothing about the two behaviours
    it sits beside -- an honest ``requested_by`` claim still rides along,
    and ordinary decoration metadata still passes through."""

    def test_an_honest_body_requested_by_still_records_its_claim(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token, key_id = _mint_with_key_id(registry, [SCOPE_MUTATE])

        resp = client.post(
            "/api/v1/commands/batch",
            json=_batch_body({"note": "decoration"}, requested_by="operator-claim"),
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200, resp.text

        (event,) = _executed_events(registry)
        assert event.payload["requested_by"] == f"api:{key_id}"
        assert event.payload["requested_by_claim"] == "operator-claim"

    def test_ordinary_decoration_metadata_still_passes(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json=_batch_body({"note": "decoration", "source": "cli"}),
        )
        assert resp.status_code == 200, resp.text
        assert len(_executed_events(registry)) == 1

    def test_metadata_absent_entirely_still_passes(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json={
                "commands": [
                    {
                        "operation": "entity.create",
                        "args": {"entity_type": "service", "name": "no-metadata"},
                    }
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        assert len(_executed_events(registry)) == 1
