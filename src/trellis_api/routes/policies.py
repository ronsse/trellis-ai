"""Policy routes — list, add, remove governance policies via REST API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from trellis.auth import SCOPE_ADMIN
from trellis.errors import DegradedStoreWriteError
from trellis.mutate import resolve_policy_path
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.policy_store import PolicyStore
from trellis_api.app import get_registry
from trellis_api.auth import require_scope

router = APIRouter()

_policy_store_cache: PolicyStore | None = None
_policy_store_registry_id: int | None = None


def _get_policy_store() -> PolicyStore:
    """Get a cached policy store co-located with the other stores.

    Cache is invalidated when the underlying registry instance changes
    (e.g. between test fixtures) — **and whenever the cached store is
    degraded.** A degraded store refuses every write (#413), and the cache
    lives for the life of the process, so without that second condition an
    operator who repaired ``policies.json`` would go on getting refusals
    until someone restarted the API. Re-reading a small JSON file on the
    admin-rate policy routes costs nothing, and only happens while the
    store is broken, which is not a steady state.
    """
    global _policy_store_cache, _policy_store_registry_id  # noqa: PLW0603
    registry = get_registry()
    reg_id = id(registry)
    if (
        _policy_store_cache is None
        or _policy_store_registry_id != reg_id
        or _policy_store_cache.is_degraded
    ):
        # Resolve through the shared helper so this route writes the file
        # the mutation pipeline's Stage 2 reads — and the same file
        # ``trellis policy`` writes. See trellis.mutate.policy_source.
        path = resolve_policy_path(registry.stores_dir)
        if path is None:
            msg = "stores_dir must be set on registry to use PolicyStore"
            raise ValueError(msg)
        _policy_store_cache = PolicyStore(path)
        _policy_store_registry_id = reg_id
    return _policy_store_cache


def _degraded_http_error(store: PolicyStore) -> HTTPException:
    """409 for a request that cannot be honoured from a degraded store.

    409 rather than 503: the file will not repair itself, so "retry later"
    is the wrong instruction. The body carries the recovery command for the
    same reason the CLI banner does — the operator meeting this needs the
    fix, not a diagnosis. These routes are admin-scoped, and ``GET`` is
    read-scoped and already serves the file's contents, so naming the path
    discloses nothing the caller could not already reach (the shape #414
    settled for ``GET /api/v1/advisories``).
    """
    degradation = store.degradation
    detail: dict[str, Any] = {
        "code": "degraded_store_write",
        "message": (
            "The policy store loaded degraded; writes are refused so the "
            "unreadable file is not replaced by the partial view."
        ),
    }
    if degradation is not None:
        detail["store_degradation"] = degradation.to_dict()
    return HTTPException(status_code=409, detail=detail)


class CreatePolicyRequest(BaseModel):
    """Typed request body for policy creation."""

    policy_type: PolicyType
    scope: PolicyScope
    rules: list[PolicyRule]
    enforcement: Enforcement = Enforcement.WARN


@router.get("/policies")
def list_policies() -> dict[str, Any]:
    """List all governance policies.

    Serves what parsed even on a damaged file, and says so: ``count``
    alone under-reports a degraded store, so a caller reading it as the
    size of the ruleset would be wrong.
    """
    store = _get_policy_store()
    policies = store.list()
    payload: dict[str, Any] = {
        "count": len(policies),
        "policies": [p.model_dump(mode="json") for p in policies],
    }
    degradation = store.degradation
    if degradation is not None:
        payload["store_degradation"] = degradation.to_dict()
    return payload


@router.get("/policies/{policy_id}")
def get_policy(policy_id: str) -> dict[str, Any]:
    """Get a policy by ID."""
    store = _get_policy_store()
    policy = store.get(policy_id)
    if policy is None:
        # A 404 from a degraded store is a claim the store cannot support:
        # the entry may exist in the file and simply have failed to parse.
        if store.is_degraded:
            raise _degraded_http_error(store)
        raise HTTPException(status_code=404, detail=f"Policy not found: {policy_id}")
    return {"policy": policy.model_dump(mode="json")}


@router.post(
    "/policies",
    # Router-level dependency is ``read``; policy writes additionally
    # require ``admin`` (see the router→scope map in trellis_api.app).
    dependencies=[Depends(require_scope(SCOPE_ADMIN))],
)
def create_policy(body: CreatePolicyRequest) -> dict[str, Any]:
    """Create a governance policy."""
    store = _get_policy_store()
    if store.is_degraded:
        raise _degraded_http_error(store)
    policy = Policy(
        policy_type=body.policy_type,
        scope=body.scope,
        rules=body.rules,
        enforcement=body.enforcement,
    )
    try:
        store.add(policy)
    # Backstop. The guard above turns the common case into a clean 409;
    # this catches a store that became degraded between the two, and keeps
    # the refusal a 409 rather than a 500 from the unhandled-exception
    # handler, which would drop the recovery command.
    except DegradedStoreWriteError as exc:
        raise _degraded_http_error(store) from exc
    return {
        "status": "ok",
        "policy_id": policy.policy_id,
        "message": "Policy created",
    }


@router.delete(
    "/policies/{policy_id}",
    dependencies=[Depends(require_scope(SCOPE_ADMIN))],
)
def delete_policy(policy_id: str) -> dict[str, Any]:
    """Delete a governance policy."""
    store = _get_policy_store()
    if store.is_degraded:
        raise _degraded_http_error(store)
    try:
        removed = store.remove(policy_id)
    except DegradedStoreWriteError as exc:
        raise _degraded_http_error(store) from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"Policy not found: {policy_id}")
    return {
        "status": "ok",
        "policy_id": policy_id,
        "message": "Policy removed",
    }
