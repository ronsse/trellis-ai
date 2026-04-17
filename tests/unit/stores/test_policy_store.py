"""Tests for PolicyStore — JSON file-based policy persistence."""

from __future__ import annotations

from pathlib import Path

from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.policy_store import PolicyStore


def _policy(**kwargs) -> Policy:
    defaults = {
        "policy_type": PolicyType.MUTATION,
        "scope": PolicyScope(level="global"),
        "rules": [PolicyRule(operation="entity.create", action="deny")],
        "enforcement": Enforcement.ENFORCE,
    }
    defaults.update(kwargs)
    return Policy(**defaults)


class TestPolicyStore:
    def test_add_and_list(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        p = _policy()
        store.add(p)
        policies = store.list()
        assert len(policies) == 1
        assert policies[0].policy_id == p.policy_id

    def test_get_by_id(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        p = _policy()
        store.add(p)
        found = store.get(p.policy_id)
        assert found is not None
        assert found.policy_id == p.policy_id

    def test_get_nonexistent(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        assert store.get("nonexistent") is None

    def test_remove(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        p = _policy()
        store.add(p)
        assert store.remove(p.policy_id) is True
        assert store.list() == []

    def test_remove_nonexistent(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        assert store.remove("nonexistent") is False

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "policies.json"
        store1 = PolicyStore(path)
        p = _policy()
        store1.add(p)

        # New instance reads persisted data
        store2 = PolicyStore(path)
        policies = store2.list()
        assert len(policies) == 1
        assert policies[0].policy_id == p.policy_id

    def test_multiple_policies(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        p1 = _policy()
        p2 = _policy(scope=PolicyScope(level="domain", value="payments"))
        store.add(p1)
        store.add(p2)
        assert len(store.list()) == 2

    def test_replace_existing(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        p = _policy()
        store.add(p)
        # Add same policy again (same ID) — should replace
        store.add(p)
        assert len(store.list()) == 1

    def test_empty_store_on_new_path(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "new" / "policies.json")
        assert store.list() == []
