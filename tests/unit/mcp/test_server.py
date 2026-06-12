"""Tests for MCP macro tool server functions."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.server import RESOURCE_NOT_FOUND
from trellis.mcp.server import (
    get_context as _get_context,
)
from trellis.mcp.server import (
    get_graph as _get_graph,
)
from trellis.mcp.server import (
    get_lessons as _get_lessons,
)
from trellis.mcp.server import (
    get_objective_context as _get_objective_context,
)
from trellis.mcp.server import (
    get_sectioned_context as _get_sectioned_context,
)
from trellis.mcp.server import (
    get_task_context as _get_task_context,
)
from trellis.mcp.server import (
    record_feedback as _record_feedback,
)
from trellis.mcp.server import (
    save_experience as _save_experience,
)
from trellis.mcp.server import (
    save_knowledge as _save_knowledge,
)
from trellis.mcp.server import (
    save_memory as _save_memory,
)
from trellis.mcp.server import (
    search as _search,
)
from trellis.stores.registry import StoreRegistry

get_context = unwrap_tool(_get_context)
get_graph = unwrap_tool(_get_graph)
get_lessons = unwrap_tool(_get_lessons)
get_objective_context = unwrap_tool(_get_objective_context)
get_sectioned_context = unwrap_tool(_get_sectioned_context)
get_task_context = unwrap_tool(_get_task_context)
record_feedback = unwrap_tool(_record_feedback)
save_experience = unwrap_tool(_save_experience)
save_knowledge = unwrap_tool(_save_knowledge)
save_memory = unwrap_tool(_save_memory)
search = unwrap_tool(_search)


# ``_suppress_structlog`` and ``temp_registry`` come from conftest.py.


# ---------------------------------------------------------------------------
# get_context
# ---------------------------------------------------------------------------


class TestGetContext:
    def test_empty_intent_raises_invalid_params(self) -> None:
        for arg in ("", "   "):
            with pytest.raises(McpError) as excinfo:
                get_context(arg)
            assert excinfo.value.error.code == INVALID_PARAMS
            assert "intent must not be empty" in excinfo.value.error.message
            assert excinfo.value.error.data == {"field": "intent"}

    def test_no_results_returns_message(self) -> None:
        result = get_context("something obscure")
        assert "No context found" in result

    def test_returns_matching_documents(self, temp_registry: StoreRegistry) -> None:
        doc_store = temp_registry.knowledge.document_store
        doc_store.put(
            "doc1", "How to deploy the platform safely", metadata={"domain": "platform"}
        )

        result = get_context("deploy platform")
        # Should find the doc via FTS
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_matching_graph_nodes(self, temp_registry: StoreRegistry) -> None:
        graph = temp_registry.knowledge.graph_store
        graph.upsert_node(
            node_id="n1",
            node_type="concept",
            properties={"name": "deployment pipeline"},
        )

        result = get_context("deployment")
        assert isinstance(result, str)
        # The node name should appear in the markdown output
        assert "deployment" in result.lower()

    def test_deduplicates_results(self, temp_registry: StoreRegistry) -> None:
        doc_store = temp_registry.knowledge.document_store
        doc_store.put("d1", "duplicate content about testing")
        doc_store.put("d1", "duplicate content about testing")  # same id

        result = get_context("testing")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# save_experience
# ---------------------------------------------------------------------------


class TestSaveExperience:
    def test_empty_trace_raises_invalid_params(self) -> None:
        with pytest.raises(McpError) as excinfo:
            save_experience("")
        assert excinfo.value.error.code == INVALID_PARAMS
        assert "trace_json must not be empty" in excinfo.value.error.message
        assert excinfo.value.error.data == {"field": "trace_json"}

    def test_invalid_json_raises_invalid_params(self) -> None:
        with pytest.raises(McpError) as excinfo:
            save_experience("not valid json")
        assert excinfo.value.error.code == INVALID_PARAMS
        assert "invalid trace JSON" in excinfo.value.error.message
        assert excinfo.value.error.data is not None
        assert excinfo.value.error.data["field"] == "trace_json"
        # ``error_class`` carries the pydantic exception class so the agent
        # can switch on programmatic categories without parsing prose.
        assert "error_class" in excinfo.value.error.data

    def test_invalid_trace_schema_raises_invalid_params(self) -> None:
        with pytest.raises(McpError) as excinfo:
            save_experience('{"foo": "bar"}')
        assert excinfo.value.error.code == INVALID_PARAMS

    def test_valid_trace_is_stored(self) -> None:
        trace = {
            "source": "agent",
            "intent": "test the deployment",
            "context": {"agent_id": "test-agent"},
            "steps": [
                {
                    "step_type": "action",
                    "name": "deploy",
                    "args": {},
                    "result": {"status": "ok"},
                }
            ],
            "outcome": {"status": "success", "summary": "deployed"},
        }
        result = save_experience(json.dumps(trace))
        assert result.startswith("Trace saved:")

    def _rich_trace(self) -> dict:
        return {
            "source": "agent",
            "intent": "fix the import",
            "context": {"agent_id": "test-agent", "domain": "backend"},
            "steps": [{"step_type": "tool_call", "name": "grep"}],
            "outcome": {"status": "success"},
        }

    def test_extraction_flag_off_leaves_graph_empty(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRELLIS_ENABLE_TRACE_EXTRACTION", raising=False)
        save_experience(json.dumps(self._rich_trace()))
        assert temp_registry.knowledge.graph_store.count_nodes() == 0

    def test_extraction_flag_on_populates_graph(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_ENABLE_TRACE_EXTRACTION", "1")
        result = save_experience(json.dumps(self._rich_trace()))
        trace_id = result.split("Trace saved:")[1].strip()

        graph = temp_registry.knowledge.graph_store
        assert graph.count_nodes() > 0
        assert graph.get_node(f"trace:{trace_id}") is not None
        edges = graph.get_edges(f"trace:{trace_id}", direction="outgoing")
        assert edges
        for edge in edges:
            assert edge.get("properties", {}).get("source_trace_id") == trace_id

    def test_extraction_failure_does_not_fail_save(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_ENABLE_TRACE_EXTRACTION", "1")
        import trellis.extract.trace_ingest_hook as hook

        def _boom(*_a: object, **_k: object) -> object:
            msg = "boom"
            raise RuntimeError(msg)

        monkeypatch.setattr(hook, "result_to_batch", _boom)
        # Save must still succeed even though extraction explodes.
        result = save_experience(json.dumps(self._rich_trace()))
        assert result.startswith("Trace saved:")


# ---------------------------------------------------------------------------
# save_knowledge
# ---------------------------------------------------------------------------


class TestSaveKnowledge:
    def test_empty_name_raises_invalid_params(self) -> None:
        with pytest.raises(McpError) as excinfo:
            save_knowledge("")
        assert excinfo.value.error.code == INVALID_PARAMS
        assert "name must not be empty" in excinfo.value.error.message
        assert excinfo.value.error.data == {"field": "name"}

    def test_creates_entity(self) -> None:
        result = save_knowledge("test concept")
        assert "Entity created" in result
        assert "test concept" in result

    def test_creates_entity_with_properties(self) -> None:
        result = save_knowledge("my entity", properties={"color": "blue"})
        assert "Entity created" in result

    def test_creates_edge_when_target_exists(
        self, temp_registry: StoreRegistry
    ) -> None:
        graph = temp_registry.knowledge.graph_store
        target_id = graph.upsert_node(
            node_id=None, node_type="concept", properties={"name": "target"}
        )

        result = save_knowledge("source", relates_to=target_id)
        assert "Entity created" in result
        assert "Edge created" in result

    def test_warns_when_target_missing(self) -> None:
        result = save_knowledge("orphan", relates_to="nonexistent_id")
        assert "Warning" in result
        assert "edge not created" in result


# ---------------------------------------------------------------------------
# save_memory
# ---------------------------------------------------------------------------


class TestSaveMemory:
    def test_empty_content_raises_invalid_params(self) -> None:
        with pytest.raises(McpError) as excinfo:
            save_memory("")
        assert excinfo.value.error.code == INVALID_PARAMS
        assert "content must not be empty" in excinfo.value.error.message
        assert excinfo.value.error.data == {"field": "content"}

    def test_stores_document(self) -> None:
        result = save_memory("remember this fact")
        assert result.startswith("Memory saved:")

    def test_stores_with_metadata(self) -> None:
        result = save_memory("tagged content", metadata={"domain": "ops"})
        assert result.startswith("Memory saved:")

    def test_stores_with_custom_id(self) -> None:
        result = save_memory("custom id doc", doc_id="my-custom-id")
        assert "my-custom-id" in result

    def test_dedup_returns_existing_id(self, temp_registry: StoreRegistry) -> None:
        first = save_memory("identical content")
        second = save_memory("identical content")
        first_id = first.split(":", 1)[1].strip()
        assert second == f"Memory already exists: {first_id}"

    def test_dedup_does_not_emit_second_event(
        self, temp_registry: StoreRegistry
    ) -> None:
        from trellis.stores.base.event_log import EventType

        save_memory("dedup event test")
        save_memory("dedup event test")
        events = temp_registry.operational.event_log.get_events(
            event_type=EventType.MEMORY_STORED, limit=100
        )
        assert len(events) == 1

    def test_emits_memory_stored_event(self, temp_registry: StoreRegistry) -> None:
        from trellis.stores.base.event_log import EventType

        result = save_memory("event emission test", metadata={"domain": "ops"})
        doc_id = result.split(":", 1)[1].strip()
        events = temp_registry.operational.event_log.get_events(
            event_type=EventType.MEMORY_STORED, limit=100
        )
        assert len(events) == 1
        event = events[0]
        assert event.source == "save_memory"
        assert event.entity_id == doc_id
        assert event.entity_type == "document"
        assert event.payload["doc_id"] == doc_id
        assert event.payload["content_length"] == len("event emission test")
        assert event.payload["metadata"] == {"domain": "ops"}
        assert "content_hash" in event.payload


# ---------------------------------------------------------------------------
# save_memory — tiered extraction (feature-flagged)
# ---------------------------------------------------------------------------


@pytest.fixture
def _reset_memory_extractor_cache():
    """Reset the module-level memory-extractor cache between tests."""
    server_mod._memory_extractor = None
    server_mod._memory_extractor_attempted = False
    yield
    server_mod._memory_extractor = None
    server_mod._memory_extractor_attempted = False


@pytest.mark.usefixtures("_reset_memory_extractor_cache")
class TestSaveMemoryExtractionFeatureFlag:
    def test_flag_off_by_default_no_extractor_built(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Default behavior: env var unset → extractor is None, nothing runs."""
        monkeypatch.delenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", raising=False)
        result = save_memory("hello world")
        assert result.startswith("Memory saved:")
        assert server_mod._memory_extractor is None

    def test_flag_on_without_llm_client_skips_extractor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Flag on but no API key → extractor None, save_memory still succeeds."""
        monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")
        monkeypatch.setattr(server_mod, "_build_llm_client_from_env", lambda: None)
        result = save_memory("hello again")
        assert result.startswith("Memory saved:")
        assert server_mod._memory_extractor is None

    def test_flag_on_with_llm_client_runs_extraction(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Flag on + fake LLM client → extractor built and invoked.

        LLM returns empty JSON so the extractor finds no drafts — this
        isolates "did extraction run?" from "do drafts write through
        MutationExecutor?" (which is covered by result_to_batch tests).
        """
        monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")

        call_count = {"n": 0}

        class _FakeLLM:
            async def generate(self, **kwargs):
                from trellis.llm.types import LLMResponse, TokenUsage

                call_count["n"] += 1
                return LLMResponse(
                    content='{"entities": [], "edges": []}',
                    model="fake",
                    usage=TokenUsage(
                        prompt_tokens=1, completion_tokens=1, total_tokens=2
                    ),
                )

        monkeypatch.setattr(server_mod, "_build_llm_client_from_env", _FakeLLM)
        result = save_memory("observation about @system")
        assert result.startswith("Memory saved:")
        assert server_mod._memory_extractor is not None
        # LLM stage fires exactly once — no "@system" in the graph, so
        # AliasMatch finds no match, residue flows to LLM.
        assert call_count["n"] == 1

    def test_extraction_failure_non_fatal(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the extractor blows up, save_memory still returns success."""
        monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")

        class _BrokenLLM:
            async def generate(self, **kwargs):
                msg = "llm down"
                raise RuntimeError(msg)

        monkeypatch.setattr(server_mod, "_build_llm_client_from_env", _BrokenLLM)
        result = save_memory("should still save")
        assert result.startswith("Memory saved:")

    def test_registry_llm_client_preempts_env_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        temp_registry: StoreRegistry,
    ) -> None:
        """Registry-sourced LLM wins over env vars.

        When ``registry.build_llm_client()`` returns a configured client,
        the env-var construction path must NOT be consulted — even if
        ``OPENAI_API_KEY`` is present.
        """
        monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")
        # Env var is set — the old code path would have used it.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-env-key-should-be-ignored")

        call_count = {"registry_llm": 0}

        class _RegistryLLM:
            async def generate(self, **kwargs):
                from trellis.llm.types import LLMResponse, TokenUsage

                call_count["registry_llm"] += 1
                return LLMResponse(
                    content='{"entities": [], "edges": []}',
                    model="registry-fake",
                    usage=TokenUsage(
                        prompt_tokens=1, completion_tokens=1, total_tokens=2
                    ),
                )

        registry_llm_instance = _RegistryLLM()
        monkeypatch.setattr(
            temp_registry,
            "build_llm_client",
            lambda: registry_llm_instance,
        )

        # Sentinel: if env path is consulted, this blows up loudly.
        def _env_path_must_not_run() -> None:
            msg = "env-var LLM path should not run when registry provides a client"
            raise AssertionError(msg)

        monkeypatch.setattr(
            server_mod,
            "_build_llm_client_from_env",
            _env_path_must_not_run,
        )

        result = save_memory("registry preempts env observation about @system")
        assert result.startswith("Memory saved:")
        assert server_mod._memory_extractor is not None
        # The registry-sourced client handled the extraction call.
        assert call_count["registry_llm"] == 1


# ---------------------------------------------------------------------------
# get_graph
# ---------------------------------------------------------------------------


class TestGetGraph:
    def test_empty_entity_id_raises_invalid_params(self) -> None:
        with pytest.raises(McpError) as excinfo:
            get_graph("")
        assert excinfo.value.error.code == INVALID_PARAMS
        assert "entity_id must not be empty" in excinfo.value.error.message
        assert excinfo.value.error.data == {"field": "entity_id"}

    def test_not_found_raises_resource_not_found(self) -> None:
        with pytest.raises(McpError) as excinfo:
            get_graph("nonexistent")
        assert excinfo.value.error.code == RESOURCE_NOT_FOUND
        assert "entity not found" in excinfo.value.error.message.lower()
        assert excinfo.value.error.data == {"entity_id": "nonexistent"}

    def test_returns_entity_neighborhood(self, temp_registry: StoreRegistry) -> None:
        graph = temp_registry.knowledge.graph_store
        graph.upsert_node(
            node_id="e1", node_type="system", properties={"name": "API Server"}
        )
        graph.upsert_node(
            node_id="e2", node_type="system", properties={"name": "Database"}
        )
        graph.upsert_edge("e1", "e2", "depends_on")

        result = get_graph("e1")
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# record_feedback
# ---------------------------------------------------------------------------


class TestRecordFeedback:
    def test_missing_ids_raises_invalid_params(self) -> None:
        with pytest.raises(McpError) as excinfo:
            record_feedback(success=True)
        assert excinfo.value.error.code == INVALID_PARAMS
        assert "one of trace_id or pack_id" in excinfo.value.error.message
        assert excinfo.value.error.data == {"fields": ["trace_id", "pack_id"]}
        with pytest.raises(McpError):
            record_feedback("", "", success=True)

    def test_positive_feedback(self) -> None:
        result = record_feedback("trace_abc", success=True)
        assert "positive" in result
        assert "trace_abc" in result

    def test_negative_feedback(self) -> None:
        result = record_feedback("trace_xyz", success=False, notes="didn't work")
        assert "negative" in result

    def test_feedback_event_is_logged(self, temp_registry: StoreRegistry) -> None:
        record_feedback("trace_42", success=True, notes="great")
        events = temp_registry.operational.event_log.get_events(entity_id="trace_42")
        assert len(events) >= 1
        assert any(e.payload.get("success") is True for e in events)

    def test_pack_feedback_emits_pack_entity(
        self, temp_registry: StoreRegistry
    ) -> None:
        result = record_feedback(pack_id="pack_99", success=True)
        assert "pack_99" in result
        events = temp_registry.operational.event_log.get_events(entity_id="pack_99")
        assert len(events) >= 1
        event = events[0]
        assert event.entity_type == "pack"
        assert event.payload.get("pack_id") == "pack_99"

    def test_pack_feedback_stores_element_refs(
        self, temp_registry: StoreRegistry
    ) -> None:
        record_feedback(
            pack_id="pack_7",
            success=True,
            helpful_item_ids=["doc_a", "entity_b"],
            unhelpful_item_ids=["doc_noise"],
            followed_advisory_ids=["adv_1"],
        )
        events = temp_registry.operational.event_log.get_events(entity_id="pack_7")
        assert len(events) >= 1
        payload = events[0].payload
        assert payload["helpful_item_ids"] == ["doc_a", "entity_b"]
        assert payload["unhelpful_item_ids"] == ["doc_noise"]
        assert payload["followed_advisory_ids"] == ["adv_1"]

    def test_pack_id_preferred_when_both_provided(
        self, temp_registry: StoreRegistry
    ) -> None:
        result = record_feedback(trace_id="trace_x", pack_id="pack_y", success=True)
        # Pack feedback takes precedence
        assert "pack: pack_y" in result
        pack_events = temp_registry.operational.event_log.get_events(entity_id="pack_y")
        assert len(pack_events) == 1
        trace_events = temp_registry.operational.event_log.get_events(
            entity_id="trace_x"
        )
        assert len(trace_events) == 0


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_empty_query_raises_invalid_params(self) -> None:
        with pytest.raises(McpError) as excinfo:
            search("")
        assert excinfo.value.error.code == INVALID_PARAMS
        assert "query must not be empty" in excinfo.value.error.message
        assert excinfo.value.error.data == {"field": "query"}

    def test_no_results_returns_message(self) -> None:
        result = search("absolutely nothing here")
        assert "No results" in result

    def test_finds_documents(self, temp_registry: StoreRegistry) -> None:
        temp_registry.knowledge.document_store.put("d1", "kubernetes deployment guide")
        result = search("kubernetes")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_finds_graph_nodes(self, temp_registry: StoreRegistry) -> None:
        temp_registry.knowledge.graph_store.upsert_node(
            node_id="n1", node_type="concept", properties={"name": "kubernetes"}
        )
        result = search("kubernetes")
        assert "kubernetes" in result.lower()


# ---------------------------------------------------------------------------
# get_sectioned_context
# ---------------------------------------------------------------------------


class TestGetSectionedContext:
    def test_empty_intent_raises_invalid_params(self) -> None:
        with pytest.raises(McpError) as excinfo:
            get_sectioned_context("", sections=[{"name": "S1"}])
        assert excinfo.value.error.code == INVALID_PARAMS
        assert "intent must not be empty" in excinfo.value.error.message

    def test_empty_sections_raises_invalid_params(self) -> None:
        with pytest.raises(McpError) as excinfo:
            get_sectioned_context("intent", sections=[])
        assert excinfo.value.error.code == INVALID_PARAMS
        assert "sections must not be empty" in excinfo.value.error.message

    def test_returns_markdown_with_sections(self) -> None:
        sections = [
            {
                "name": "Background",
                "retrieval_affinities": ["domain_knowledge"],
                "max_tokens": 500,
            },
            {
                "name": "Patterns",
                "retrieval_affinities": ["technical_pattern"],
                "max_tokens": 500,
            },
        ]
        result = get_sectioned_context("test intent", sections=sections)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# get_lessons
# ---------------------------------------------------------------------------


class TestGetLessons:
    def test_returns_string(self) -> None:
        result = get_lessons()
        assert isinstance(result, str)

    def test_with_domain_filter(self) -> None:
        result = get_lessons(domain="platform")
        assert isinstance(result, str)

    def test_invalid_limit_type_raises(self) -> None:
        # ``limit`` is typed ``int`` and is forwarded down to the SQL layer.
        # Passing a non-int surfaces a useful, non-generic error from the
        # store backend (TypeError or sqlite3 error) rather than being
        # silently coerced. We catch the union so the assertion stays
        # meaningful across SQLite versions / store backends.
        import sqlite3

        with pytest.raises((TypeError, sqlite3.IntegrityError, sqlite3.DataError)):
            get_lessons(limit="ten")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# get_objective_context
# ---------------------------------------------------------------------------


class TestGetObjectiveContext:
    def test_empty_intent_raises_invalid_params(self) -> None:
        for arg in ("", "   "):
            with pytest.raises(McpError) as excinfo:
                get_objective_context(arg)
            assert excinfo.value.error.code == INVALID_PARAMS
            assert "intent must not be empty" in excinfo.value.error.message

    def test_returns_markdown_string(self) -> None:
        result = get_objective_context("ship the deploy checklist")
        assert isinstance(result, str)
        # Either the formatter rendered something, or the assembly failed
        # gracefully — both produce a string, not an exception.
        assert len(result) > 0

    def test_with_domain_filter(self, temp_registry: StoreRegistry) -> None:
        temp_registry.knowledge.document_store.put(
            "doc-obj-domain", "platform deploy guide", metadata={"domain": "platform"}
        )
        result = get_objective_context(
            "release plan", domain="platform", max_tokens=1500
        )
        assert isinstance(result, str)

    def test_session_dedup_across_calls(self, temp_registry: StoreRegistry) -> None:
        # Just exercise the session_id path; semantic dedup is covered by
        # PackBuilder unit tests. Here we only assert the call shape works.
        first = get_objective_context(
            "objective dedup probe", session_id="sess-obj-1"
        )
        second = get_objective_context(
            "objective dedup probe", session_id="sess-obj-1"
        )
        assert isinstance(first, str)
        assert isinstance(second, str)

    def test_assembly_failure_raises_internal_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pack-builder failure surfaces as ``McpError(INTERNAL_ERROR)``
        with the original ``RuntimeError`` chained via ``__cause__``."""

        def _boom(_registry: object) -> None:
            msg = "fake builder failure"
            raise RuntimeError(msg)

        monkeypatch.setattr(server_mod, "_build_pack_builder", _boom)
        with pytest.raises(McpError) as excinfo:
            get_objective_context("anything")
        err = excinfo.value
        assert err.error.code == INTERNAL_ERROR
        assert "objective context" in err.error.message
        assert err.error.data is not None
        assert err.error.data["tool"] == "get_objective_context"
        # ``from exc`` preserves the original cause for operator debugging.
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert str(excinfo.value.__cause__) == "fake builder failure"


# ---------------------------------------------------------------------------
# get_task_context
# ---------------------------------------------------------------------------


class TestGetTaskContext:
    def test_empty_intent_raises_invalid_params(self) -> None:
        for arg in ("", "   "):
            with pytest.raises(McpError) as excinfo:
                get_task_context(arg)
            assert excinfo.value.error.code == INVALID_PARAMS
            assert "intent must not be empty" in excinfo.value.error.message

    def test_returns_markdown_string(self) -> None:
        result = get_task_context("write SQL for sessions table")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_with_entity_ids(self, temp_registry: StoreRegistry) -> None:
        graph = temp_registry.knowledge.graph_store
        graph.upsert_node(
            node_id="uc://cat.sch.tbl",
            node_type="table",
            properties={"name": "sessions"},
        )
        result = get_task_context(
            "summarize sessions",
            entity_ids=["uc://cat.sch.tbl"],
            max_tokens=1500,
        )
        assert isinstance(result, str)

    def test_invalid_entity_ids_type_raises_internal_error(self) -> None:
        # ``entity_ids`` is typed ``list[str] | None``; passing a non-list
        # like an int triggers a TypeError/ValidationError that the outer
        # try/except wraps as ``McpError(INTERNAL_ERROR)``.
        with pytest.raises(McpError) as excinfo:
            get_task_context("intent", entity_ids=42)  # type: ignore[arg-type]
        assert excinfo.value.error.code == INTERNAL_ERROR

    def test_assembly_failure_raises_internal_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_registry: object) -> None:
            msg = "fake builder failure"
            raise RuntimeError(msg)

        monkeypatch.setattr(server_mod, "_build_pack_builder", _boom)
        with pytest.raises(McpError) as excinfo:
            get_task_context("anything")
        err = excinfo.value
        assert err.error.code == INTERNAL_ERROR
        assert "task context" in err.error.message
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert err.error.data is not None
        assert err.error.data["tool"] == "get_task_context"


# ---------------------------------------------------------------------------
# get_sectioned_context — error-path coverage beyond the happy path above
# ---------------------------------------------------------------------------


class TestGetSectionedContextErrors:
    def test_invalid_section_schema_raises_internal_error(self) -> None:
        """A section dict missing the required ``name`` key fails Pydantic
        validation; the outer wrapper surfaces this as INTERNAL_ERROR."""
        with pytest.raises(McpError) as excinfo:
            get_sectioned_context(
                "intent",
                sections=[{"retrieval_affinities": ["domain_knowledge"]}],
            )
        assert excinfo.value.error.code == INTERNAL_ERROR

    def test_assembly_failure_raises_internal_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_registry: object) -> None:
            msg = "fake builder failure"
            raise RuntimeError(msg)

        monkeypatch.setattr(server_mod, "_build_pack_builder", _boom)
        with pytest.raises(McpError) as excinfo:
            get_sectioned_context(
                "intent",
                sections=[
                    {
                        "name": "Background",
                        "retrieval_affinities": ["domain_knowledge"],
                        "max_tokens": 500,
                    }
                ],
            )
        err = excinfo.value
        assert err.error.code == INTERNAL_ERROR
        assert "sectioned context" in err.error.message
        assert isinstance(excinfo.value.__cause__, RuntimeError)


# ---------------------------------------------------------------------------
# Session-aware dedup across MCP tools
# ---------------------------------------------------------------------------


class TestSessionAwareGetContext:
    def test_session_id_emits_pack_assembled_event(
        self, temp_registry: StoreRegistry
    ) -> None:
        from trellis.stores.base.event_log import EventType

        temp_registry.knowledge.document_store.put(
            "doc-session-1", "kubernetes deployment tips"
        )
        get_context("kubernetes", session_id="sess-1")
        events = temp_registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        matching = [e for e in events if e.payload.get("session_id") == "sess-1"]
        assert len(matching) == 1
        assert "doc-session-1" in matching[0].payload.get("injected_item_ids", [])

    def test_repeat_call_same_session_excludes_served_items(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put(
            "doc-repeat", "kubernetes deployment tips"
        )
        first = get_context("kubernetes", session_id="sess-repeat")
        assert "doc-repeat" in first or len(first) > 0
        second = get_context("kubernetes", session_id="sess-repeat")
        # doc-repeat was served in first call; second call should report none
        assert "No context found" in second

    def test_different_session_not_deduped(self, temp_registry: StoreRegistry) -> None:
        temp_registry.knowledge.document_store.put(
            "doc-isolated", "kubernetes deployment tips"
        )
        get_context("kubernetes", session_id="sess-A")
        other = get_context("kubernetes", session_id="sess-B")
        assert "No context found" not in other

    def test_no_session_id_no_dedup(self, temp_registry: StoreRegistry) -> None:
        temp_registry.knowledge.document_store.put(
            "doc-nosess", "kubernetes deployment tips"
        )
        first = get_context("kubernetes")
        second = get_context("kubernetes")
        # Without session_id, both calls return content
        assert "No context found" not in first
        assert "No context found" not in second


# ---------------------------------------------------------------------------
# Shutdown handling
# ---------------------------------------------------------------------------


class TestMainShutdown:
    """``main()`` must close the cached registry on exit, even if mcp.run raises.

    Regression guard for the Postgres-pool / Neo4j-driver leak that
    used to happen on stdio EOF: the cached :class:`StoreRegistry`
    held connections open until the process was reaped.
    """

    def _drain_registry(self) -> None:
        """Reset module-level state so each test starts clean."""
        if server_mod._registry is not None:
            with contextlib.suppress(Exception):
                server_mod._registry.close()
        server_mod._registry = None

    def test_main_closes_registry_on_clean_exit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._drain_registry()
        registry = StoreRegistry(stores_dir=tmp_path / "stores2")
        registry.stores_dir.mkdir(parents=True)
        # Touch a store so close() has something to do.
        registry.knowledge.document_store.put("doc1", "shutdown probe")
        server_mod._registry = registry
        close_calls: list[int] = []
        original_close = registry.close

        def _tracking_close() -> None:
            close_calls.append(1)
            original_close()

        monkeypatch.setattr(registry, "close", _tracking_close)
        monkeypatch.setattr(server_mod.mcp, "run", lambda: None)
        # configure_stderr_logging mutates global structlog config — keep
        # the conftest CRITICAL filter by stubbing it out here.
        monkeypatch.setattr(server_mod, "configure_stderr_logging", lambda: None)

        server_mod.main()

        assert close_calls == [1]
        assert server_mod._registry is None

    def test_main_closes_registry_when_run_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._drain_registry()
        registry = StoreRegistry(stores_dir=tmp_path / "stores3")
        registry.stores_dir.mkdir(parents=True)
        server_mod._registry = registry
        close_calls: list[int] = []
        original_close = registry.close

        def _tracking_close() -> None:
            close_calls.append(1)
            original_close()

        def _boom() -> None:
            msg = "simulated mcp.run failure"
            raise RuntimeError(msg)

        monkeypatch.setattr(registry, "close", _tracking_close)
        monkeypatch.setattr(server_mod.mcp, "run", _boom)
        monkeypatch.setattr(server_mod, "configure_stderr_logging", lambda: None)

        with pytest.raises(RuntimeError, match="simulated"):
            server_mod.main()

        assert close_calls == [1]
        assert server_mod._registry is None

    def test_main_no_registry_constructed_no_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If no tool ever called ``_get_registry()``, finally must no-op."""
        self._drain_registry()
        # No registry constructed; the autouse temp_registry fixture
        # already nulled it out via teardown, but be explicit.
        assert server_mod._registry is None
        monkeypatch.setattr(server_mod.mcp, "run", lambda: None)
        monkeypatch.setattr(server_mod, "configure_stderr_logging", lambda: None)

        # Should not raise.
        server_mod.main()
        assert server_mod._registry is None


# ---------------------------------------------------------------------------
# C2 Phase 3 — structured-error protocol coverage
# ---------------------------------------------------------------------------
#
# These tests exercise the loud-failure contract from the silent-fallback
# cleanup track: store-layer outages inside a tool handler must surface
# as ``McpError`` with a meaningful JSON-RPC ``code`` and the original
# exception chained via ``__cause__``. They complement the per-tool
# error-path tests above by force-feeding store failures from outside
# and asserting on the resulting structured error.


def _patch_method_to_raise(
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    method_name: str,
    exc: BaseException,
) -> None:
    """Patch ``target.method_name`` to a callable that raises ``exc``.

    Used to force a specific sub-system failure inside an aggregator-style
    tool (``get_context``, ``search``) without replacing the whole store
    object (which can't be assigned through the ``_KnowledgePlane``
    property without a setter).
    """

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise exc

    monkeypatch.setattr(target, method_name, _boom)


class TestStructuredErrorContract:
    """Each test covers a different ``McpError`` category. The combined
    set demonstrates the four codes the contract exposes:

    * ``INVALID_PARAMS`` — pre-flight validation.
    * ``RESOURCE_NOT_FOUND`` — handler asked for a missing entity.
    * ``MUTATION_FAILED`` — governed mutation returned non-success.
    * ``INTERNAL_ERROR`` — unexpected sub-system failure with cause chain.
    """

    def test_get_context_doc_store_failure_surfaces_internal_error(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the document store is down, ``get_context`` raises rather
        than silently returning the partial pack from the other axes."""
        boom = RuntimeError("fake doc store outage")
        _patch_method_to_raise(
            monkeypatch, temp_registry.knowledge.document_store, "search", boom
        )

        with pytest.raises(McpError) as excinfo:
            get_context("kubernetes")
        err = excinfo.value
        assert err.error.code == INTERNAL_ERROR
        assert "document search failed" in err.error.message
        assert err.error.data is not None
        assert err.error.data["stage"] == "doc_search"
        assert err.error.data["intent"] == "kubernetes"
        # ``raise … from exc`` preserves the cause chain so operators
        # see the underlying ``RuntimeError`` traceback in server logs.
        assert excinfo.value.__cause__ is boom

    def test_get_context_graph_store_failure_surfaces_internal_error(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Graph-store outage is structurally distinct from doc-store outage
        (different ``data['stage']``) so the agent can attribute fault."""
        boom = RuntimeError("fake graph store outage")
        _patch_method_to_raise(
            monkeypatch, temp_registry.knowledge.graph_store, "query", boom
        )

        with pytest.raises(McpError) as excinfo:
            get_context("anything")
        err = excinfo.value
        assert err.error.code == INTERNAL_ERROR
        assert err.error.data is not None
        assert err.error.data["stage"] == "graph_search"
        assert excinfo.value.__cause__ is boom

    def test_save_experience_invalid_trace_data_carries_field(self) -> None:
        """Invalid trace JSON surfaces as INVALID_PARAMS with the
        offending field name in ``data`` so a programmatic agent can
        switch on the field without parsing the prose message."""
        with pytest.raises(McpError) as excinfo:
            save_experience("not-json")
        err = excinfo.value
        assert err.error.code == INVALID_PARAMS
        assert err.error.data is not None
        assert err.error.data["field"] == "trace_json"
        assert "error_class" in err.error.data

    def test_save_experience_mutation_failure_surfaces_mutation_failed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the executor returns a non-success ``CommandResult``, the
        tool raises ``McpError(MUTATION_FAILED, …)`` rather than the old
        ``"Error: Failed to store trace — ..."`` string."""
        from trellis.mutate import CommandStatus

        class _FailingExecutor:
            def execute(self, _command: object) -> object:
                class _R:
                    status = CommandStatus.FAILED
                    command_id = "cmd-xyz"
                    message = "synthetic handler error"
                    created_id = None

                return _R()

        monkeypatch.setattr(
            server_mod,
            "build_curate_executor",
            lambda _registry: _FailingExecutor(),
        )

        # Build a syntactically valid trace JSON so we reach the executor.
        trace = {
            "source": "agent",
            "intent": "trigger executor failure",
            "context": {"agent_id": "test-agent"},
            "steps": [
                {
                    "step_type": "action",
                    "name": "noop",
                    "args": {},
                    "result": {"status": "ok"},
                }
            ],
            "outcome": {"status": "success", "summary": "synthetic"},
        }
        from trellis.mcp.server import MUTATION_FAILED as _MUTATION_FAILED

        with pytest.raises(McpError) as excinfo:
            save_experience(json.dumps(trace))
        err = excinfo.value
        assert err.error.code == _MUTATION_FAILED
        assert "failed to store trace" in err.error.message
        assert "synthetic handler error" in err.error.message
        assert err.error.data is not None
        assert err.error.data["status"] == "failed"
        assert err.error.data["command_id"] == "cmd-xyz"

    def test_get_graph_not_found_uses_resource_not_found_code(self) -> None:
        """``get_graph`` for an unknown entity uses the app-layer
        ``RESOURCE_NOT_FOUND`` code, not the catch-all INTERNAL_ERROR.
        Agents differentiate "ask for a different entity" from "retry later"."""
        with pytest.raises(McpError) as excinfo:
            get_graph("zorblax-not-real")
        err = excinfo.value
        assert err.error.code == RESOURCE_NOT_FOUND
        # Code lives in the documented app-layer JSON-RPC range.
        assert -32099 <= err.error.code <= -32000
        assert err.error.data == {"entity_id": "zorblax-not-real"}

    def test_save_memory_event_emission_failure_chains_cause(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An event-log emit failure inside ``save_memory`` raises with
        the original exception chained — used to be a silent debug log."""
        boom = RuntimeError("fake event-log outage")

        def _boom(*args: object, **kwargs: object) -> None:
            raise boom

        monkeypatch.setattr(temp_registry.operational.event_log, "emit", _boom)

        with pytest.raises(McpError) as excinfo:
            save_memory("unique memory content for emission failure test")
        err = excinfo.value
        assert err.error.code == INTERNAL_ERROR
        assert "MEMORY_STORED" in err.error.message
        assert err.error.data is not None
        assert err.error.data["stage"] == "memory_stored_emit"
        assert excinfo.value.__cause__ is boom

    def test_save_memory_minhash_init_failure_chains_cause(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If MinHash index init blows up, ``save_memory`` raises rather
        than silently disabling fuzzy dedup."""
        # Reset module-level cache so _get_minhash_index will try to build.
        monkeypatch.setattr(server_mod, "_minhash_index", None)
        boom = RuntimeError("fake minhash init failure")

        def _boom(*args: object, **kwargs: object) -> list[dict[str, object]]:
            raise boom

        # Force the seed-from-docs path to blow up — the constructor
        # itself is happy; the seed loop is where the error appears.
        monkeypatch.setattr(
            temp_registry.knowledge.document_store, "search", _boom
        )

        with pytest.raises(McpError) as excinfo:
            save_memory("content that triggers minhash seed")
        err = excinfo.value
        assert err.error.code == INTERNAL_ERROR
        assert "MinHash" in err.error.message
        assert err.error.data == {"stage": "minhash_index_init"}
        assert excinfo.value.__cause__ is boom

    def test_record_feedback_missing_ids_data_lists_both_fields(self) -> None:
        """The ``data`` payload lists every field involved in the
        validation rule (one-of), not just the singular ``field`` key.
        Agents can render a clearer prompt from this."""
        with pytest.raises(McpError) as excinfo:
            record_feedback(success=True)
        err = excinfo.value
        assert err.error.code == INVALID_PARAMS
        assert err.error.data == {"fields": ["trace_id", "pack_id"]}

    def test_execute_mutation_non_dict_args_uses_data_type_hint(self) -> None:
        """The ``data['type']`` reflects the offending value's Python type
        so the agent can self-correct (e.g. ``"args must be a dict; got str"``)."""
        from trellis.mcp.server import execute_mutation as _em

        em = unwrap_tool(_em)
        with pytest.raises(McpError) as excinfo:
            em(operation="link.create", args="not a dict")  # type: ignore[arg-type]
        err = excinfo.value
        assert err.error.code == INVALID_PARAMS
        assert err.error.data is not None
        assert err.error.data["field"] == "args"
        assert err.error.data["type"] == "str"
