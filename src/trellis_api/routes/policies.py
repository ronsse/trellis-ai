"""Policy routes — list, add, remove governance policies via REST API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from trellis.auth import SCOPE_ADMIN
from trellis.errors import StoreWriteRefusedError
from trellis.mutate import resolve_policy_path
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.policy_store import PolicyStore
from trellis_api.app import get_registry
from trellis_api.auth import require_scope

router = APIRouter()


def _get_policy_store() -> PolicyStore:
    """Build a policy store from the registry's stores directory.

    **Deliberately not cached.** It used to be, for the life of the process
    and keyed on registry identity — which meant a *healthy* cached store
    outlived the file it had read. The reference deployment writes
    ``policies.json`` from two processes (a host ``trellis policy add`` and
    this containerised API, against one bind-mounted data dir), so a store
    that loaded ``[A]`` would happily rewrite the file as ``[A, C]`` after
    the CLI had made it ``[A, B]`` — deleting a policy from Stage 2
    enforcement, on a request that returned ``200``, with the next ``GET``
    reporting perfectly normal. That is #413's defect arriving with no
    corruption anywhere in it.

    Re-reading is a ``stat`` and a small JSON parse on admin-rate routes,
    which is not a price worth a correctness hazard — and it is what makes
    "policies are read per call, so an edit takes effect without a restart"
    true of this surface as well as of the CLI.
    :meth:`PolicyStore.refuse_if_stale` backstops the window this cannot
    close.
    """
    registry = get_registry()
    # Resolve through the shared helper so this route writes the file the
    # mutation pipeline's Stage 2 reads — and the same file ``trellis
    # policy`` writes. See trellis.mutate.policy_source.
    path = resolve_policy_path(registry.stores_dir)
    if path is None:
        msg = "stores_dir must be set on registry to use PolicyStore"
        raise ValueError(msg)
    return PolicyStore(path)


def _refusal_http_error(
    store: PolicyStore, exc: StoreWriteRefusedError | None = None
) -> HTTPException:
    """409 for a request the policy file's state will not support.

    409 rather than 503: a degraded file does not repair itself, so "retry
    later" is the wrong instruction — a *stale* refusal is the retryable
    one, which is why ``code`` distinguishes them. And rather than 500: the
    unhandled-exception handler's body says only "internal server error",
    dropping the recovery command that is the whole justification for
    refusing.

    The two write routes are admin-scoped; the ``GET`` that also raises
    this is read-scoped and already serves the file's contents, so naming
    the path discloses nothing that caller could not already reach — the
    shape #414 settled for ``GET /api/v1/advisories``.
    """
    if exc is not None and exc.code == "STALE_STORE_WRITE":
        return HTTPException(
            status_code=409,
            detail={
                "code": "stale_store_write",
                "message": (
                    "The policy file changed after this request read it. The "
                    "write was refused rather than replace whatever landed "
                    "in between. Re-read and retry."
                ),
                "recovery": exc.recovery,
                "store_degradation": None,
            },
        )
    degradation = store.degradation
    return HTTPException(
        status_code=409,
        detail={
            # Not ``degraded_store_write``: this is also raised from a read
            # route, where nothing was being written, and a client branching
            # on the code would see a write failure for a ``GET``.
            "code": "degraded_store",
            "message": (
                "The policy store loaded degraded: it serves the rows that "
                "parsed, cannot answer for the rows that did not, and "
                "refuses every write so the unreadable file is not replaced "
                "by the partial view."
            ),
            "store_degradation": (
                degradation.to_dict() if degradation is not None else None
            ),
        },
    )


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
    degradation = store.degradation
    # Always present, ``None`` when clean — the shape ``GET
    # /api/v1/advisories`` already uses. An optional key makes every client
    # handle its absence, and absence is the dangerous case to guess at.
    return {
        "count": len(policies),
        "policies": [p.model_dump(mode="json") for p in policies],
        "store_degradation": (
            degradation.to_dict() if degradation is not None else None
        ),
    }


@router.get("/policies/{policy_id}")
def get_policy(policy_id: str) -> dict[str, Any]:
    """Get a policy by ID."""
    store = _get_policy_store()
    policy = store.get(policy_id)
    if policy is None:
        # A 404 from a degraded store is a claim the store cannot support:
        # the entry may exist in the file and simply have failed to parse.
        if store.is_degraded:
            raise _refusal_http_error(store)
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
        raise _refusal_http_error(store)
    policy = Policy(
        policy_type=body.policy_type,
        scope=body.scope,
        rules=body.rules,
        enforcement=body.enforcement,
    )
    try:
        store.add(policy)
    # Live, not decorative: ``refuse_if_stale`` fires here when another
    # process wrote the file between this store's load and its save. The
    # guard above covers only a degraded *load*, which cannot change after
    # construction — so before the stale guard existed this really was
    # unreachable, and the comment that used to sit here said otherwise.
    except StoreWriteRefusedError as exc:
        raise _refusal_http_error(store, exc) from exc
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
        raise _refusal_http_error(store)
    try:
        removed = store.remove(policy_id)
    except StoreWriteRefusedError as exc:
        raise _refusal_http_error(store, exc) from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"Policy not found: {policy_id}")
    return {
        "status": "ok",
        "policy_id": policy_id,
        "message": "Policy removed",
    }
