"""Optional store-backend hook for registry-owned resource preparation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class RegistryContext:
    """Backend-neutral services available while a registry builds a store.

    Backends must namespace ``shared`` keys with their module path so
    independently installed plugins cannot collide.
    """

    env: Mapping[str, str]
    shared: dict[Any, Any]
    register_closer: Callable[[Callable[[], None]], None]
    emit_warning: Callable[[str], None]


class RegistryPreparable(Protocol):
    """Store class that prepares constructor parameters through the registry."""

    @classmethod
    def prepare_registry_params(
        cls,
        ctx: RegistryContext,
        store_type: str,
        params: dict[str, Any],
    ) -> dict[str, Any]: ...
