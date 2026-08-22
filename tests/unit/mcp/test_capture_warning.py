"""Capture-health banner on MCP pack outputs (#309).

The banner must reach every pack surface — flat, empty-state, and
sectioned — when capture is failing, and its computation must never
break retrieval: the GRACEFUL-DEGRADATION posture shared with
``track_token_usage``.
"""

from __future__ import annotations

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


@pytest.fixture(autouse=True)
def _clear_capture_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's shell must not change what the suite asserts.

    The MCP conftest clears only the auth vars, and an operator who has
    silenced the banner with ``TRELLIS_CAPTURE_WARN_THRESHOLD=0`` — the
    documented way to disable it — would otherwise fail every
    banner-prefixed assertion below.
    """
    monkeypatch.delenv("TRELLIS_CAPTURE_WARN_THRESHOLD", raising=False)
    monkeypatch.delenv("TRELLIS_CAPTURE_WARN_WINDOW_HOURS", raising=False)


get_context = unwrap_tool(server_mod.get_context)
get_task_context = unwrap_tool(server_mod.get_task_context)
search = unwrap_tool(server_mod.search)

_BANNER = "> **WARNING: memory capture is failing.**"


def _seed_capture_failure(registry: StoreRegistry, n: int = 3) -> None:
    for _ in range(n):
        registry.operational.event_log.emit(
            EventType.WRITE_REJECTED,
            "mcp:save_experience",
            payload={
                "tool": "save_experience",
                "stage": "boundary",
                "rejections": [
                    {"kind": "extra_forbidden", "loc": "outcome.artifacts", "msg": ""}
                ],
                "hints": [],
            },
        )


def _accept(registry: StoreRegistry, requested_by: str) -> None:
    registry.operational.event_log.emit(
        EventType.MUTATION_EXECUTED,
        "mutation_executor",
        payload={"requested_by": requested_by, "status": "success"},
    )


class TestCaptureWarningBanner:
    def test_flat_pack_is_prefixed(self, temp_registry: StoreRegistry) -> None:
        temp_registry.knowledge.document_store.put(
            "doc1", "How to deploy the platform safely"
        )
        _seed_capture_failure(temp_registry)
        result = get_context("deploy platform")
        assert result.startswith(_BANNER)
        assert "mcp:save_experience" in result
        # The pack itself still follows in full.
        assert "# Context for:" in result

    def test_empty_state_still_carries_banner(
        self, temp_registry: StoreRegistry
    ) -> None:
        """The empty pack is exactly where dark capture masquerades as
        greenfield — the claude-mem rule includes it deliberately."""
        _seed_capture_failure(temp_registry)
        result = get_context("something entirely obscure")
        assert result.startswith(_BANNER)
        assert "No context found" in result

    def test_search_is_prefixed(self, temp_registry: StoreRegistry) -> None:
        _seed_capture_failure(temp_registry)
        result = search("anything")
        assert result.startswith(_BANNER)

    def test_sectioned_path_is_prefixed(self, temp_registry: StoreRegistry) -> None:
        _seed_capture_failure(temp_registry)
        result = get_context("deploy", sections=[{"name": "Domain Knowledge"}])
        assert result.startswith(_BANNER)

    def test_sectioned_alias_is_prefixed(self, temp_registry: StoreRegistry) -> None:
        _seed_capture_failure(temp_registry)
        result = get_task_context("deploy")
        assert result.startswith(_BANNER)

    def test_healthy_capture_adds_nothing(self, temp_registry: StoreRegistry) -> None:
        result = get_context("something entirely obscure")
        assert _BANNER not in result

    def test_recovered_surface_suppresses_banner(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed_capture_failure(temp_registry)
        _accept(temp_registry, "mcp:save_experience")
        result = get_context("something entirely obscure")
        assert _BANNER not in result

    def test_accept_from_another_surface_still_warns(
        self, temp_registry: StoreRegistry
    ) -> None:
        """The rule is per surface: a nightly ingest landing rows does not
        make a 100%-rejected ``save_experience`` look healthy."""
        _seed_capture_failure(temp_registry)
        _accept(temp_registry, "cli:ingest")
        result = get_context("something entirely obscure")
        assert result.startswith(_BANNER)
        assert "mcp:save_experience" in result

    def test_health_check_failure_never_blocks_retrieval(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GRACEFUL-DEGRADATION: a broken health check serves the pack
        unbannered instead of failing the call."""
        _seed_capture_failure(temp_registry)

        def _boom(*args: object, **kwargs: object) -> None:
            msg = "event log unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(server_mod, "check_capture_health", _boom)
        result = get_context("something entirely obscure")
        assert _BANNER not in result
        assert "No context found" in result
