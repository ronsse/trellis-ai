"""Policy store — JSON file-based persistence for governance policies."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from trellis.schemas.policy import Policy

logger = structlog.get_logger(__name__)


class PolicyStore:
    """Load and save policies from a JSON file.

    Lightweight persistence suitable for local and single-node deployments.
    Policies are small, rarely change, and are loaded in full at startup —
    a JSON file is the right weight class.

    File format::

        {"policies": [<Policy.model_dump()>, ...]}
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._policies: dict[str, Policy] = {}
        if self._path.exists():
            self._load()

    # -- Public API --

    def list(self) -> list[Policy]:
        """Return all policies, ordered by creation time."""
        return list(self._policies.values())

    def get(self, policy_id: str) -> Policy | None:
        """Get a policy by ID."""
        return self._policies.get(policy_id)

    def add(self, policy: Policy) -> Policy:
        """Add or replace a policy. Persists immediately."""
        self._policies[policy.policy_id] = policy
        self._save()
        logger.info("policy_stored", policy_id=policy.policy_id)
        return policy

    def remove(self, policy_id: str) -> bool:
        """Remove a policy by ID. Returns ``True`` if found."""
        if policy_id not in self._policies:
            return False
        del self._policies[policy_id]
        self._save()
        logger.info("policy_removed", policy_id=policy_id)
        return True

    # -- Persistence --

    def _load(self) -> None:
        """Load policies from the JSON file."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for entry in raw.get("policies", []):
                policy = Policy.model_validate(entry)
                self._policies[policy.policy_id] = policy
            logger.info("policies_loaded", count=len(self._policies))
        except Exception:
            logger.exception("policy_load_failed", path=str(self._path))

    def _save(self) -> None:
        """Persist current policies to the JSON file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "policies": [p.model_dump(mode="json") for p in self._policies.values()]
        }
        self._path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
