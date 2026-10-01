"""The MCP server stamps its session's project on packs and traces.

The working directory here is a hand-built synthetic repository (see
``tests/unit/core/test_project.py`` for the resolver itself).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import trellis.core.project as project_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.core.project import PROJECT_ENV
from trellis.mcp.server import get_context as _get_context
from trellis.mcp.server import save_experience as _save_experience
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.registry import StoreRegistry

get_context = unwrap_tool(_get_context)
save_experience = unwrap_tool(_save_experience)

_REPO = "epsilon-repo"


@pytest.fixture(autouse=True)
def _session_in_a_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test from ``<tmp>/epsilon-repo/src``, as a spawned server would."""
    repo = tmp_path / _REPO
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    monkeypatch.chdir(repo / "src")


def _pack_events(registry: StoreRegistry, **filters: Any) -> list[Event]:
    return registry.operational.event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, limit=10, **filters
    )


def _save(registry: StoreRegistry, metadata: dict[str, Any] | None = None) -> dict:
    trace: dict[str, Any] = {
        "source": "agent",
        "intent": "exercise the project stamp",
        "context": {"agent_id": "test-agent"},
    }
    if metadata is not None:
        trace["metadata"] = metadata
    trace_id = save_experience(json.dumps(trace)).split("Trace saved:")[1].strip()
    stored = registry.operational.trace_store.get(trace_id)
    assert stored is not None
    return stored.metadata


class TestPacks:
    def test_a_flat_pack_is_stamped_and_found_by_project(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("doc1", "a canary rollout runbook")

        get_context("canary rollout")

        (event,) = _pack_events(temp_registry)
        assert event.payload["project"] == _REPO
        found = _pack_events(temp_registry, payload_filters={"project": _REPO})
        assert [e.entity_id for e in found] == [event.entity_id]
        assert _pack_events(temp_registry, payload_filters={"project": "other"}) == []

    def test_a_sectioned_pack_is_stamped(self, temp_registry: StoreRegistry) -> None:
        temp_registry.knowledge.document_store.put("doc1", "a canary rollout runbook")

        get_context("canary rollout", sections=[{"name": "All"}])

        (event,) = _pack_events(temp_registry)
        assert event.payload["project"] == _REPO


class TestTraces:
    def test_the_stamp_is_added_beside_the_agents_metadata(
        self, temp_registry: StoreRegistry
    ) -> None:
        assert _save(temp_registry, {"note": "kept"}) == {
            "note": "kept",
            "project": _REPO,
        }

    def test_a_disagreeing_agent_value_is_kept_as_unverified(
        self, temp_registry: StoreRegistry
    ) -> None:
        metadata = _save(temp_registry, {"project": "zeta-claimed"})
        assert metadata == {"project": _REPO, "project_unverified": "zeta-claimed"}

    @pytest.mark.parametrize("supplied", [_REPO, None], ids=["agrees", "null"])
    def test_an_agreeing_or_null_agent_value_adds_no_companion(
        self, temp_registry: StoreRegistry, supplied: str | None
    ) -> None:
        assert _save(temp_registry, {"project": supplied}) == {"project": _REPO}


class TestHttpTransport:
    def test_the_cwd_is_ignored_and_the_override_is_honoured(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_MCP_TRANSPORT", "http")
        assert _save(temp_registry) == {"project": None}

        monkeypatch.setenv(PROJECT_ENV, "eta-deploy")
        assert _save(temp_registry) == {"project": "eta-deploy"}

    def test_a_null_stamp_still_owns_the_key(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_MCP_TRANSPORT", "http")
        metadata = _save(temp_registry, {"project": "zeta-claimed"})
        assert metadata == {"project": None, "project_unverified": "zeta-claimed"}


class TestFailSoft:
    @pytest.fixture(params=["resolver-raises", "bad-transport"])
    def broken(
        self, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if request.param == "resolver-raises":

            def _boom(_start: Path) -> str | None:
                raise RuntimeError

            monkeypatch.setattr(project_mod, "_repo_name", _boom)
        else:
            monkeypatch.setenv("TRELLIS_MCP_TRANSPORT", "bogus")

    @pytest.mark.usefixtures("broken")
    def test_packs_and_traces_still_succeed_unstamped(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put("doc1", "a canary rollout runbook")

        get_context("canary rollout")

        (event,) = _pack_events(temp_registry)
        assert event.payload["project"] is None
        assert _save(temp_registry) == {"project": None}
