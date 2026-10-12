"""The MCP server stamps its session's project on packs and traces.

Traces are stamped on both MCP surfaces that ingest one: ``save_experience``
and ``execute_mutation(operation="trace.ingest")``.

The working directory here is a hand-built synthetic repository (see
``tests/unit/core/test_project.py`` for the resolver itself).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import trellis.core.project as project_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.core.project import PROJECT_ENV
from trellis.mcp.server import execute_mutation as _execute_mutation
from trellis.mcp.server import get_context as _get_context
from trellis.mcp.server import save_experience as _save_experience
from trellis.schemas.trace import Trace
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.registry import StoreRegistry

execute_mutation = unwrap_tool(_execute_mutation)
get_context = unwrap_tool(_get_context)
save_experience = unwrap_tool(_save_experience)

_REPO = "epsilon-repo"
_TRACE: dict[str, Any] = {
    "source": "agent",
    "intent": "exercise the project stamp",
    "context": {"agent_id": "test-agent"},
}


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


def _ingest(surface: str, trace: dict[str, Any]) -> str:
    """Ingest *trace* through one MCP surface and return the stored id."""
    if surface == "save_experience":
        return save_experience(json.dumps(trace)).split("Trace saved:")[1].strip()
    payload = json.loads(
        execute_mutation(operation="trace.ingest", args={"trace": trace})
    )
    assert payload["status"] == "success", payload
    return payload["created_id"]


def _stored_metadata(registry: StoreRegistry, trace_id: str) -> dict:
    stored = registry.operational.trace_store.get(trace_id)
    assert stored is not None
    return stored.metadata


@pytest.fixture(params=["save_experience", "execute_mutation"])
def save(request: pytest.FixtureRequest, temp_registry: StoreRegistry) -> Any:
    """Save a trace through each MCP surface; return its stored metadata."""

    def _save(metadata: dict[str, Any] | None = None) -> dict:
        trace = _TRACE if metadata is None else {**_TRACE, "metadata": metadata}
        return _stored_metadata(temp_registry, _ingest(request.param, trace))

    return _save


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
    def test_the_stamp_is_added_beside_the_agents_metadata(self, save: Any) -> None:
        assert save({"note": "kept"}) == {
            "note": "kept",
            "project": _REPO,
        }

    def test_a_disagreeing_agent_value_is_kept_as_unverified(self, save: Any) -> None:
        metadata = save({"project": "zeta-claimed"})
        assert metadata == {"project": _REPO, "project_unverified": "zeta-claimed"}

    @pytest.mark.parametrize("supplied", [_REPO, None], ids=["agrees", "null"])
    def test_an_agreeing_or_null_agent_value_adds_no_companion(
        self, save: Any, supplied: str | None
    ) -> None:
        assert save({"project": supplied}) == {"project": _REPO}


class TestHttpTransport:
    def test_the_cwd_is_ignored_and_the_override_is_honoured(
        self, save: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_MCP_TRANSPORT", "http")
        assert save() == {"project": None}

        monkeypatch.setenv(PROJECT_ENV, "eta-deploy")
        assert save() == {"project": "eta-deploy"}

    def test_a_null_stamp_still_owns_the_key(
        self, save: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_MCP_TRANSPORT", "http")
        metadata = save({"project": "zeta-claimed"})
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
        self, temp_registry: StoreRegistry, save: Any
    ) -> None:
        temp_registry.knowledge.document_store.put("doc1", "a canary rollout runbook")

        get_context("canary rollout")

        (event,) = _pack_events(temp_registry)
        assert event.payload["project"] is None
        assert save() == {"project": None}


class TestExecuteMutation:
    """``trace.ingest`` through ``execute_mutation``: the cases beyond a dict."""

    def test_a_trace_instance_is_stamped_like_a_dict(
        self, temp_registry: StoreRegistry
    ) -> None:
        trace = Trace.model_validate(
            {**_TRACE, "metadata": {"project": "zeta-claimed"}}
        )
        args: dict[str, Any] = {"trace": trace}
        payload = json.loads(execute_mutation(operation="trace.ingest", args=args))

        assert _stored_metadata(temp_registry, payload["created_id"]) == {
            "project": _REPO,
            "project_unverified": "zeta-claimed",
        }
        # The stamp goes on a copy: the caller's args and trace are untouched.
        assert args["trace"] is trace
        assert trace.metadata == {"project": "zeta-claimed"}

    @pytest.mark.parametrize(
        "trace",
        [
            {"source": "agent", "intent": "exercise the project stamp"},
            {**_TRACE, "unknown_field": 1},
            {**_TRACE, "source": "not-a-source"},
            json.dumps(_TRACE),
            None,
        ],
        ids=["missing-field", "extra-field", "bad-enum", "json-string", "null"],
    )
    def test_an_invalid_trace_is_refused_by_the_handler(
        self, temp_registry: StoreRegistry, trace: Any
    ) -> None:
        with pytest.raises(ValidationError):
            Trace.model_validate(trace)

        payload = json.loads(
            execute_mutation(operation="trace.ingest", args={"trace": trace})
        )

        assert (payload["status"], payload["message"]) == (
            "failed",
            "Execution failed: ValidationError",
        )
        assert temp_registry.operational.trace_store.count() == 0

    def test_a_missing_trace_is_refused_by_validation(
        self, temp_registry: StoreRegistry
    ) -> None:
        payload = json.loads(execute_mutation(operation="trace.ingest", args={}))

        assert (payload["status"], payload["message"]) == (
            "rejected",
            "Validation failed: Missing required args: trace",
        )
        assert temp_registry.operational.trace_store.count() == 0
