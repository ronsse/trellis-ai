"""Tests for MCP macro tool server functions."""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path

import pytest
import structlog

import trellis.mcp.server as server_mod
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
    get_sectioned_context as _get_sectioned_context,
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


def _unwrap(tool_or_fn):  # type: ignore[no-untyped-def]
    """Unwrap a FastMCP FunctionTool to its underlying callable."""
    return getattr(tool_or_fn, "fn", tool_or_fn)


get_context = _unwrap(_get_context)
get_graph = _unwrap(_get_graph)
get_lessons = _unwrap(_get_lessons)
get_sectioned_context = _unwrap(_get_sectioned_context)
record_feedback = _unwrap(_record_feedback)
save_experience = _unwrap(_save_experience)
save_knowledge = _unwrap(_save_knowledge)
save_memory = _unwrap(_save_memory)
search = _unwrap(_search)


@pytest.fixture(autouse=True)
def _suppress_structlog() -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL),
    )


@pytest.fixture(autouse=True)
def _temp_registry(tmp_path: Path, request: pytest.FixtureRequest) -> StoreRegistry:
    """Create a temp StoreRegistry and patch into the MCP server module."""
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir(parents=True)
    registry = StoreRegistry(stores_dir=stores_dir)
    server_mod._registry = registry
    # Stash for tests that need direct access
    request.config.cache_registry = registry  # type: ignore[attr-defined]
    yield registry
    server_mod._registry = None


@pytest.fixture
def temp_registry(_temp_registry: StoreRegistry) -> StoreRegistry:
    """Expose the autouse registry for tests that need direct access."""
    return _temp_registry


# ---------------------------------------------------------------------------
# get_context
# ---------------------------------------------------------------------------


class TestGetContext:
    def test_empty_intent_returns_error(self) -> None:
        assert get_context("").startswith("Error")
        assert get_context("   ").startswith("Error")

    def test_no_results_returns_message(self) -> None:
        result = get_context("something obscure")
        assert "No context found" in result

    def test_returns_matching_documents(self, temp_registry: StoreRegistry) -> None:
        doc_store = temp_registry.document_store
        doc_store.put(
            "doc1", "How to deploy the platform safely", metadata={"domain": "platform"}
        )

        result = get_context("deploy platform")
        # Should find the doc via FTS
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_matching_graph_nodes(self, temp_registry: StoreRegistry) -> None:
        graph = temp_registry.graph_store
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
        doc_store = temp_registry.document_store
        doc_store.put("d1", "duplicate content about testing")
        doc_store.put("d1", "duplicate content about testing")  # same id

        result = get_context("testing")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# save_experience
# ---------------------------------------------------------------------------


class TestSaveExperience:
    def test_empty_trace_returns_error(self) -> None:
        assert save_experience("").startswith("Error")

    def test_invalid_json_returns_error(self) -> None:
        result = save_experience("not valid json")
        assert "Error" in result

    def test_invalid_trace_schema_returns_error(self) -> None:
        result = save_experience('{"foo": "bar"}')
        assert "Error" in result

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


# ---------------------------------------------------------------------------
# save_knowledge
# ---------------------------------------------------------------------------


class TestSaveKnowledge:
    def test_empty_name_returns_error(self) -> None:
        assert save_knowledge("").startswith("Error")

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
        graph = temp_registry.graph_store
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
    def test_empty_content_returns_error(self) -> None:
        assert save_memory("").startswith("Error")

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
        events = temp_registry.event_log.get_events(
            event_type=EventType.MEMORY_STORED, limit=100
        )
        assert len(events) == 1

    def test_emits_memory_stored_event(self, temp_registry: StoreRegistry) -> None:
        from trellis.stores.base.event_log import EventType

        result = save_memory("event emission test", metadata={"domain": "ops"})
        doc_id = result.split(":", 1)[1].strip()
        events = temp_registry.event_log.get_events(
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
    def test_empty_entity_id_returns_error(self) -> None:
        assert get_graph("").startswith("Error")

    def test_not_found_returns_message(self) -> None:
        result = get_graph("nonexistent")
        assert "not found" in result.lower()

    def test_returns_entity_neighborhood(self, temp_registry: StoreRegistry) -> None:
        graph = temp_registry.graph_store
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
    def test_empty_args_returns_error(self) -> None:
        assert record_feedback(success=True).startswith("Error")
        assert record_feedback("", "", success=True).startswith("Error")

    def test_positive_feedback(self) -> None:
        result = record_feedback("trace_abc", success=True)
        assert "positive" in result
        assert "trace_abc" in result

    def test_negative_feedback(self) -> None:
        result = record_feedback("trace_xyz", success=False, notes="didn't work")
        assert "negative" in result

    def test_feedback_event_is_logged(self, temp_registry: StoreRegistry) -> None:
        record_feedback("trace_42", success=True, notes="great")
        events = temp_registry.event_log.get_events(entity_id="trace_42")
        assert len(events) >= 1
        assert any(e.payload.get("success") is True for e in events)

    def test_pack_feedback_emits_pack_entity(
        self, temp_registry: StoreRegistry
    ) -> None:
        result = record_feedback(pack_id="pack_99", success=True)
        assert "pack_99" in result
        events = temp_registry.event_log.get_events(entity_id="pack_99")
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
        events = temp_registry.event_log.get_events(entity_id="pack_7")
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
        pack_events = temp_registry.event_log.get_events(entity_id="pack_y")
        assert len(pack_events) == 1
        trace_events = temp_registry.event_log.get_events(entity_id="trace_x")
        assert len(trace_events) == 0


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_empty_query_returns_error(self) -> None:
        assert search("").startswith("Error")

    def test_no_results_returns_message(self) -> None:
        result = search("absolutely nothing here")
        assert "No results" in result

    def test_finds_documents(self, temp_registry: StoreRegistry) -> None:
        temp_registry.document_store.put("d1", "kubernetes deployment guide")
        result = search("kubernetes")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_finds_graph_nodes(self, temp_registry: StoreRegistry) -> None:
        temp_registry.graph_store.upsert_node(
            node_id="n1", node_type="concept", properties={"name": "kubernetes"}
        )
        result = search("kubernetes")
        assert "kubernetes" in result.lower()


# ---------------------------------------------------------------------------
# get_sectioned_context
# ---------------------------------------------------------------------------


class TestGetSectionedContext:
    def test_empty_intent_returns_error(self) -> None:
        result = get_sectioned_context("", sections=[{"name": "S1"}])
        assert result.startswith("Error")

    def test_empty_sections_returns_error(self) -> None:
        result = get_sectioned_context("intent", sections=[])
        assert result.startswith("Error")

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


# ---------------------------------------------------------------------------
# Session-aware dedup across MCP tools
# ---------------------------------------------------------------------------


class TestSessionAwareGetContext:
    def test_session_id_emits_pack_assembled_event(
        self, temp_registry: StoreRegistry
    ) -> None:
        from trellis.stores.base.event_log import EventType

        temp_registry.document_store.put("doc-session-1", "kubernetes deployment tips")
        get_context("kubernetes", session_id="sess-1")
        events = temp_registry.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        matching = [e for e in events if e.payload.get("session_id") == "sess-1"]
        assert len(matching) == 1
        assert "doc-session-1" in matching[0].payload.get("injected_item_ids", [])

    def test_repeat_call_same_session_excludes_served_items(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.document_store.put("doc-repeat", "kubernetes deployment tips")
        first = get_context("kubernetes", session_id="sess-repeat")
        assert "doc-repeat" in first or len(first) > 0
        second = get_context("kubernetes", session_id="sess-repeat")
        # doc-repeat was served in first call; second call should report none
        assert "No context found" in second

    def test_different_session_not_deduped(self, temp_registry: StoreRegistry) -> None:
        temp_registry.document_store.put("doc-isolated", "kubernetes deployment tips")
        get_context("kubernetes", session_id="sess-A")
        other = get_context("kubernetes", session_id="sess-B")
        assert "No context found" not in other

    def test_no_session_id_no_dedup(self, temp_registry: StoreRegistry) -> None:
        temp_registry.document_store.put("doc-nosess", "kubernetes deployment tips")
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
        registry.document_store.put("doc1", "shutdown probe")
        server_mod._registry = registry
        close_calls: list[int] = []
        original_close = registry.close

        def _tracking_close() -> None:
            close_calls.append(1)
            original_close()

        monkeypatch.setattr(registry, "close", _tracking_close)
        monkeypatch.setattr(server_mod.mcp, "run", lambda: None)
        # _configure_mcp_logging mutates global structlog config — keep
        # the conftest CRITICAL filter by stubbing it out here.
        monkeypatch.setattr(server_mod, "_configure_mcp_logging", lambda: None)

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
        monkeypatch.setattr(server_mod, "_configure_mcp_logging", lambda: None)

        with pytest.raises(RuntimeError, match="simulated"):
            server_mod.main()

        assert close_calls == [1]
        assert server_mod._registry is None

    def test_main_no_registry_constructed_no_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If no tool ever called ``_get_registry()``, finally must no-op."""
        self._drain_registry()
        # No registry constructed; the autouse _temp_registry fixture
        # already nulled it out via teardown, but be explicit.
        assert server_mod._registry is None
        monkeypatch.setattr(server_mod.mcp, "run", lambda: None)
        monkeypatch.setattr(server_mod, "_configure_mcp_logging", lambda: None)

        # Should not raise.
        server_mod.main()
        assert server_mod._registry is None
