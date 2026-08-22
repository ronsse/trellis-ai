"""End-to-end: trace drafts → governed batch → store → retrieval.

The per-layer tests in ``test_trace.py`` / ``test_commands.py`` check the
drafts and the command args.  This file checks the part that kept
regressing: whether the values actually *land*.  Confidence, node role,
the graph↔document link and the deterministic evidence each pass through
the extractor, the command bridge, the handler and the graph store — a
break anywhere in that chain is invisible to a per-layer assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.extract.commands import (
    CONFIDENCE_PROPERTY,
    reconcile_node_roles,
    result_to_batch,
)
from trellis.extract.evidence import (
    COMMANDS_RUN_PROPERTY,
    FILES_READ_PROPERTY,
    FILES_TOUCHED_PROPERTY,
)
from trellis.extract.trace import TraceExtractor
from trellis.extract.trace_ingest_hook import extract_trace_batch
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import CommandStatus
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
        # ...and the edge carries its confidence too, not just the node.
        assert used[0]["properties"][CONFIDENCE_PROPERTY] == 1.0


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


class TestUnmigratedGraph:
    """A graph an older extractor populated must not break forever.

    ``node_role`` is immutable across SCD-2 versions, so a ``tool:bash``
    node a previous release wrote as ``semantic`` cannot be promoted to
    ``structural`` in place — the ``ENTITY_CREATE`` fails outright, on
    every re-ingest of every trace using that tool.
    """

    @staticmethod
    def _seed_semantic_tool(registry: StoreRegistry) -> None:
        registry.knowledge.graph_store.upsert_node(
            node_id="tool:bash",
            node_type="SoftwareApplication",
            properties={"name": "bash"},
            node_role="semantic",
        )

    async def test_role_conflict_would_fail_the_command_unreconciled(
        self, registry: StoreRegistry
    ) -> None:
        """Baseline: this is the failure reconciliation exists to prevent."""
        self._seed_semantic_tool(registry)
        trace = Trace.model_validate(_TRACE_DATA)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        batch = result_to_batch(result, requested_by="test")
        results = build_curate_executor(registry).execute_batch(batch)
        failed = [r for r in results if r.status is CommandStatus.FAILED]
        assert len(failed) == 1
        assert "Cannot change node_role" in failed[0].message

    async def test_reconciled_batch_has_no_failed_commands(
        self, registry: StoreRegistry
    ) -> None:
        self._seed_semantic_tool(registry)
        trace = Trace.model_validate(_TRACE_DATA)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        batch = result_to_batch(result, requested_by="test")

        reconciled = reconcile_node_roles(batch, registry.knowledge.graph_store)

        assert reconciled == ["tool:bash"]
        results = build_curate_executor(registry).execute_batch(batch)
        assert [r for r in results if r.status is CommandStatus.FAILED] == []
        # The stored node keeps its role — reconciliation is not a promotion.
        node = registry.knowledge.graph_store.get_node("tool:bash")
        assert node is not None
        assert node["node_role"] == "semantic"
        # ...and the `used` edge still lands, so the graph stays connected.
        edges = registry.knowledge.graph_store.get_edges(
            f"trace:{trace.trace_id}", direction="outgoing"
        )
        assert any(e["target_id"] == "tool:bash" for e in edges)

    async def test_fresh_graph_is_untouched_by_reconciliation(
        self, registry: StoreRegistry
    ) -> None:
        """No pre-existing node -> nothing to reconcile, role lands as minted."""
        trace = Trace.model_validate(_TRACE_DATA)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        batch = result_to_batch(result, requested_by="test")

        assert reconcile_node_roles(batch, registry.knowledge.graph_store) == []

        build_curate_executor(registry).execute_batch(batch)
        node = registry.knowledge.graph_store.get_node("tool:bash")
        assert node is not None
        assert node["node_role"] == "structural"


class TestDocumentLinkSurvivesReExtraction:
    """A re-run that stops naming a document must not wipe the link."""

    #: Pinned so both extractions target the same Activity node.
    _TRACE_ID = "trace-relink-1"

    async def _extract_with_metadata(
        self, registry: StoreRegistry, metadata: dict
    ) -> dict:
        trace = Trace.model_validate(
            {**_TRACE_DATA, "trace_id": self._TRACE_ID, "metadata": metadata}
        )
        await _extract_into(registry, trace)
        node = registry.knowledge.graph_store.get_node(f"trace:{self._TRACE_ID}")
        assert node is not None
        return node

    async def test_omitted_document_ids_carries_the_stored_link_forward(
        self, registry: StoreRegistry
    ) -> None:
        first = await self._extract_with_metadata(registry, {"document_ids": ["doc-1"]})
        assert first["document_ids"] == ["doc-1"]

        # Re-extracted after the ingest-time metadata is gone.
        again = await self._extract_with_metadata(registry, {})
        assert again["document_ids"] == ["doc-1"]

    async def test_an_explicit_value_still_replaces(
        self, registry: StoreRegistry
    ) -> None:
        """Carry-forward on omission, replace on supply — not merge."""
        await self._extract_with_metadata(registry, {"document_ids": ["doc-1"]})
        moved = await self._extract_with_metadata(registry, {"document_ids": ["doc-2"]})
        assert moved["document_ids"] == ["doc-2"]


_EVIDENCE_TRACE_ID = "tr_evidence"
_EVIDENCE_TRACE_DATA: dict = {
    "trace_id": _EVIDENCE_TRACE_ID,
    "source": "agent",
    "intent": "fix the broken import",
    "steps": [
        {
            "step_type": "tool_call",
            "name": "Edit",
            "args": {"file_path": "src/a.py", "old_string": "x"},
            "result": {},
        },
        {
            "step_type": "tool_call",
            "name": "Read",
            "args": {"file_path": "src/b.py"},
            "result": {},
        },
        {
            "step_type": "tool_call",
            "name": "Bash",
            "args": {"command": "pytest -q"},
            "result": {},
        },
    ],
    "context": {"agent_id": "code-orchestrator", "domain": "backend"},
}


class TestEvidenceLands:
    """The deterministic evidence (#308) has to survive the whole chain.

    ``extract_trace_batch`` is the seam that applies the gate, so this
    goes through it rather than through :func:`_extract_into` — the
    parsed values then still have to clear the command bridge, the
    handler and the store before anything can read them off the node.
    """

    def test_verifiable_fields_reach_the_stored_node(
        self, registry: StoreRegistry
    ) -> None:
        trace = Trace.model_validate(_EVIDENCE_TRACE_DATA)
        # Sync: extract_trace_batch owns its own event loop.
        _result, batch = extract_trace_batch(trace, requested_by="test")
        assert batch is not None
        build_curate_executor(registry).execute_batch(batch)

        node = registry.knowledge.graph_store.get_node(f"trace:{_EVIDENCE_TRACE_ID}")
        assert node is not None
        properties = node["properties"]
        assert properties[FILES_TOUCHED_PROPERTY] == ["src/a.py"]
        assert properties[FILES_READ_PROPERTY] == ["src/b.py"]
        assert properties[COMMANDS_RUN_PROPERTY] == ["pytest -q"]
