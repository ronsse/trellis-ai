"""Policy routes — list, add, remove governance policies via REST API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.policy_store import PolicyStore
from trellis_api.app import get_registry

router = APIRouter()

_policy_store_cache: PolicyStore | None = None
_policy_store_registry_id: int | None = None


def _get_policy_store() -> PolicyStore:
    """Get a cached policy store co-located with the other stores.

    Cache is invalidated when the underlying registry instance changes
    (e.g. between test fixtures).
    """
    global _policy_store_cache, _policy_store_registry_id  # noqa: PLW0603
    registry = get_registry()
    reg_id = id(registry)
    if _policy_store_cache is None or _policy_store_registry_id != reg_id:
        stores_dir = registry.stores_dir
        if stores_dir is None:
            msg = "stores_dir must be set on registry to use PolicyStore"
            raise ValueError(msg)
        _policy_store_cache = PolicyStore(stores_dir / "policies.json")
        _policy_store_registry_id = reg_id
    return _policy_store_cache


class CreatePolicyRequest(BaseModel):
    """Typed request body for policy creation."""

    policy_type: PolicyType
    scope: PolicyScope
    rules: list[PolicyRule]
    enforcement: Enforcement = Enforcement.WARN


@router.get("/policies")
def list_policies() -> dict[str, Any]:
    """List all governance policies."""
    store = _get_policy_store()
    policies = store.list()
    return {
        "count": len(policies),
        "policies": [p.model_dump(mode="json") for p in policies],
    }


@router.get("/policies/{policy_id}")
def get_policy(policy_id: str) -> dict[str, Any]:
    """Get a policy by ID."""
    store = _get_policy_store()
    policy = store.get(policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail=f"Policy not found: {policy_id}")
    return {"policy": policy.model_dump(mode="json")}


@router.post("/policies")
def create_policy(body: CreatePolicyRequest) -> dict[str, Any]:
    """Create a governance policy."""
    store = _get_policy_store()
    policy = Policy(
        policy_type=body.policy_type,
        scope=body.scope,
        rules=body.rules,
        enforcement=body.enforcement,
    )
    store.add(policy)
    return {
        "status": "ok",
        "policy_id": policy.policy_id,
        "message": "Policy created",
    }


@router.delete("/policies/{policy_id}")
def delete_policy(policy_id: str) -> dict[str, Any]:
    """Delete a governance policy."""
    store = _get_policy_store()
    if not store.remove(policy_id):
        raise HTTPException(status_code=404, detail=f"Policy not found: {policy_id}")
    return {
        "status": "ok",
        "policy_id": policy_id,
        "message": "Policy removed",
    }
