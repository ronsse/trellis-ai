"""End-to-end: trace drafts → governed batch → store → retrieval.

The per-layer tests in ``test_trace.py`` / ``test_commands.py`` check the
drafts and the command args.  This file checks the part that kept
regressing: whether the values actually *land*.  Confidence, node role
and the graph↔document link each pass through the extractor, the command
bridge, the handler and the graph store — a break anywhere in that chain
is invisible to a per-layer assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.extract.commands import CONFIDENCE_PROPERTY, result_to_batch
from trellis.extract.trace import TraceExtractor
from trellis.mutate import build_curate_executor
from trellis.retrieve.pack_builder import PackBudget, PackBuilder
from trellis.retrieve.strategies import GraphSearch
from trellis.schemas.trace import Trace
from trellis.stores.registry import StoreRegistry

_TRACE_DATA: dict = {
    "source": "agent",
    "intent": "fix the broken import",
    "steps": [
        {"step_type": "tool_call", "name": "Bash", "args": {}, "result": {}},
    ]
    * 40,
    "context": {"agent_id": "code-orchestrator", "domain": "backend"},
    "metadata": {"document_ids": ["doc-1"]},
}


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


async def _extract_into(registry: StoreRegistry, trace: Trace) -> None:
    result = await TraceExtractor().extract(trace, source_hint="trace")
    batch = result_to_batch(result, requested_by="test")
    build_curate_executor(registry).execute_batch(batch)


class TestValuesLand:
    async def test_confidence_is_persisted_on_the_node(
        self, registry: StoreRegistry
    ) -> None:
        trace = Trace.model_validate(_TRACE_DATA)
        await _extract_into(registry, trace)
        node = registry.knowledge.graph_store.get_node(f"trace:{trace.trace_id}")
        assert node is not None
        assert node["properties"][CONFIDENCE_PROPERTY] == 1.0

    async def test_document_ids_arrive_on_the_created_entity(
        self, registry: StoreRegistry
    ) -> None:
        trace = Trace.model_validate(_TRACE_DATA)
        await _extract_into(registry, trace)
        node = registry.knowledge.graph_store.get_node(f"trace:{trace.trace_id}")
        assert node is not None
        assert node["document_ids"] == ["doc-1"]

    async def test_tool_node_is_stored_structural(
        self, registry: StoreRegistry
    ) -> None:
        trace = Trace.model_validate(_TRACE_DATA)
        await _extract_into(registry, trace)
        node = registry.knowledge.graph_store.get_node("tool:bash")
        assert node is not None
        assert node["node_role"] == "structural"

    async def test_forty_steps_write_one_used_edge(
        self, registry: StoreRegistry
    ) -> None:
        trace = Trace.model_validate(_TRACE_DATA)
        await _extract_into(registry, trace)
        edges = registry.knowledge.graph_store.get_edges(
            f"trace:{trace.trace_id}", direction="outgoing"
        )
        used = [e for e in edges if e["target_id"] == "tool:bash"]
        assert len(used) == 1


class TestPackGate:
    """The minted tool node must be invisible to the default pack."""

    async def test_structural_tool_node_is_excluded_from_packs(
        self, registry: StoreRegistry
    ) -> None:
        trace = Trace.model_validate(_TRACE_DATA)
        await _extract_into(registry, trace)
        builder = PackBuilder(
            strategies=[GraphSearch(registry.knowledge.graph_store)],
            event_log=registry.operational.event_log,
        )
        pack = builder.build(
            intent="bash", budget=PackBudget(max_items=25, max_tokens=4000)
        )
        assert "tool:bash" not in {item.item_id for item in pack.items}

    async def test_include_structural_surfaces_it_again(
        self, registry: StoreRegistry
    ) -> None:
        """Excluded by default, not deleted — the gate stays a gate."""
        trace = Trace.model_validate(_TRACE_DATA)
        await _extract_into(registry, trace)
        builder = PackBuilder(
            strategies=[GraphSearch(registry.knowledge.graph_store)],
            event_log=registry.operational.event_log,
        )
        pack = builder.build(
            intent="bash",
            budget=PackBudget(max_items=25, max_tokens=4000),
            include_structural=True,
        )
        assert "tool:bash" in {item.item_id for item in pack.items}
