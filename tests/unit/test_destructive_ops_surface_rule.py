"""Every destructive operation requires `admin` at both generic mutation
surfaces -- REST `/api/v1/commands/batch` and MCP `execute_mutation`.

Both surfaces gate on :func:`~trellis.mutate.requires_admin_scope`, which
is ``operation not in NON_DESTRUCTIVE_OPERATIONS`` -- the explicit
allow-list, not :data:`~trellis.mutate.DESTRUCTIVE_OPERATIONS` (the
deny-list). Gating on the deny-list would fail *open* for a future
``Operation`` member nobody has classified yet; gating on "not in the
allow-list" fails *closed* instead. This file polices that the set below
-- derived from :class:`~trellis.mutate.Operation` and
:data:`~trellis.mutate.NON_DESTRUCTIVE_OPERATIONS` themselves, never
hand-typed -- is refused below ``admin`` at both surfaces, individually,
for every member.

The population is pinned by :func:`tests.ast_rules.assert_hand_read_floor`
at 10, the count a hand read of ``immutable_core.py`` gives today
(``ALIAS_UPSERT``, ``CLASSIFY_RULE_APPLY``, ``CLASSIFY_RULE_REVOKE``,
``ENTITY_MERGE``, ``LABEL_REMOVE``, ``LINK_REMOVE``, ``REDACTION_APPLY``,
``RETENTION_PRUNE``, ``SCHEDULE_UPDATE``, ``SETTINGS_UPDATE``). A floor is
a lower bound, not an equality: a newly-added destructive operation is
swept into the parametrisation automatically and only ever raises this
number.

Mutation-tested: exempting one operation from either surface's guard (by
hand-patching ``requires_admin_scope`` to return ``False`` for it) fails
exactly the two parametrised cases naming that operation, at the surface
patched, and no others -- see the mutation-testing note shipped with the
pull request that added this file.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from fastmcp.server.auth import AccessToken
from fastmcp.server.context import reset_transport, set_transport
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var
from mcp.shared.exceptions import McpError

import trellis.mcp.auth as mcp_auth_mod
import trellis.mcp.server as mcp_server_mod
import trellis_api.app as rest_app_mod
from tests.ast_rules import assert_hand_read_floor
from tests.unit.mcp.conftest import unwrap_tool
from trellis.auth import SCOPE_ADMIN, SCOPE_MUTATE, generate_api_key
from trellis.mcp.server import POLICY_DENIED
from trellis.mcp.server import execute_mutation as _execute_mutation
from trellis.mutate import NON_DESTRUCTIVE_OPERATIONS, Operation
from trellis_api.app import create_app

execute_mutation = unwrap_tool(_execute_mutation)

#: Every Operation outside NON_DESTRUCTIVE_OPERATIONS -- derived from the
#: enum and the allow-list themselves (never hand-typed), so this sweeps
#: in any future Operation member nobody has classified as non-destructive.
DESTRUCTIVE_OPS: list[Operation] = sorted(
    (op for op in Operation if op not in NON_DESTRUCTIVE_OPERATIONS),
    key=str,
)


def test_destructive_operation_population_floor() -> None:
    """Pin the population a person counted, so a scan that silently

    narrows (e.g. an edit to ``NON_DESTRUCTIVE_OPERATIONS`` that
    accidentally widens it) cannot shrink the parametrisation below this
    file without the suite noticing.
    """
    assert_hand_read_floor(
        len(DESTRUCTIVE_OPS),
        10,
        subject="Operation members outside NON_DESTRUCTIVE_OPERATIONS",
        hint=(
            "read trellis.mutate.immutable_core.DESTRUCTIVE_OPERATIONS by "
            "hand and compare against this count."
        ),
    )


# ---------------------------------------------------------------------------
# REST: /api/v1/commands/batch
# ---------------------------------------------------------------------------


def _mint_rest(registry: Any, scopes: list[str]) -> str:
    token, record = generate_api_key("rule-test-key", scopes)
    registry.operational.api_key_store.create(record)
    return token


@pytest.fixture
def rest_registry(tmp_path: Path) -> Iterator[Any]:
    from trellis.stores.registry import StoreRegistry

    reg = StoreRegistry(stores_dir=tmp_path / "rest_stores")
    rest_app_mod._registry = reg
    try:
        yield reg
    finally:
        reg.close()
        rest_app_mod._registry = None


@pytest.fixture
def rest_client(rest_registry: Any) -> TestClient:
    return TestClient(create_app())


@pytest.mark.parametrize("operation", DESTRUCTIVE_OPS, ids=str)
class TestEveryDestructiveOperationNeedsAdminOverRest:
    @pytest.fixture(autouse=True)
    def _required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")

    def test_refused_below_admin(
        self,
        rest_client: TestClient,
        rest_registry: Any,
        operation: Operation,
    ) -> None:
        token = _mint_rest(rest_registry, [SCOPE_MUTATE])
        resp = rest_client.post(
            "/api/v1/commands/batch",
            json={"commands": [{"operation": operation.value, "args": {}}]},
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 403, resp.text
        assert operation.value in resp.json()["detail"]

    def test_admin_passes_the_gate(
        self,
        rest_client: TestClient,
        rest_registry: Any,
        operation: Operation,
    ) -> None:
        """Admin reaches the executor -- not necessarily a success, since

        most of these operations need real target data, but the response
        is never the 403 this gate would otherwise raise.
        """
        token = _mint_rest(rest_registry, [SCOPE_ADMIN])
        resp = rest_client.post(
            "/api/v1/commands/batch",
            json={"commands": [{"operation": operation.value, "args": {}}]},
            headers={"X-API-Key": token},
        )
        assert resp.status_code != 403, resp.text


# ---------------------------------------------------------------------------
# MCP tool -- execute_mutation
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _as_stdio() -> Iterator[None]:
    token = set_transport("stdio")
    try:
        yield
    finally:
        reset_transport(token)


@contextlib.contextmanager
def _as_http(scopes: list[str] | None) -> Iterator[None]:
    transport_token = set_transport("streamable-http")
    auth_token = None
    if scopes is not None:
        access_token = AccessToken(
            token="opaque",  # noqa: S106 — placeholder, never read
            client_id="key-id",
            scopes=scopes,
            expires_at=None,
        )
        auth_token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        reset_transport(transport_token)
        if auth_token is not None:
            auth_context_var.reset(auth_token)


@pytest.fixture
def mcp_registry(tmp_path: Path) -> Iterator[Any]:
    from trellis.stores.registry import StoreRegistry

    stores_dir = tmp_path / "mcp_stores"
    stores_dir.mkdir(parents=True)
    reg = StoreRegistry(stores_dir=stores_dir)
    prior_registry = mcp_server_mod._registry
    prior_minhash = mcp_server_mod._minhash_index
    mcp_server_mod._registry = reg
    mcp_server_mod._minhash_index = None
    mcp_auth_mod.set_auth_enforced(enforced=True)
    try:
        yield reg
    finally:
        mcp_server_mod._registry = prior_registry
        mcp_server_mod._minhash_index = prior_minhash
        mcp_auth_mod.set_auth_enforced(enforced=True)
        reg.close()


@pytest.mark.parametrize("operation", DESTRUCTIVE_OPS, ids=str)
class TestEveryDestructiveOperationNeedsAdminOverMcp:
    def test_refused_below_admin(self, mcp_registry: Any, operation: Operation) -> None:
        with _as_http(scopes=[SCOPE_MUTATE]), pytest.raises(McpError) as excinfo:
            execute_mutation(operation=operation.value, args={})
        assert excinfo.value.error.code == POLICY_DENIED
        assert excinfo.value.error.data == {"operation": operation.value}

    def test_admin_passes_the_gate(
        self, mcp_registry: Any, operation: Operation
    ) -> None:
        """Admin reaches the executor -- an ``McpError`` here would have

        to be something other than ``POLICY_DENIED`` (e.g.
        ``INVALID_PARAMS`` for a missing required arg), never the scope
        refusal this gate would otherwise raise.
        """
        with _as_stdio():
            try:
                execute_mutation(operation=operation.value, args={})
            except McpError as exc:
                # No raise is expected here: the op may legitimately succeed
                # or fail for reasons unrelated to scope, so this only
                # forbids POLICY_DENIED rather than requiring an exception.
                assert exc.error.code != POLICY_DENIED, exc.error.message  # noqa: PT017
