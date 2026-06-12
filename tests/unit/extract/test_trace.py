"""Tests for TraceExtractor — structured trace→graph extraction.

Mirrors the structure of ``test_json_rules.py``: entity/edge mapping,
canonicalization, provenance stamps, and de-duplication. The three worked
examples come straight from ``docs/agent-guide/trace-format.md`` so the
tests double as a contract check that the documented examples extract.
"""

from __future__ import annotations

import pytest

from trellis.extract.base import ExtractorTier
from trellis.extract.trace import TRACE_SOURCE_HINT, TraceExtractor
from trellis.schemas.extraction import EdgeDraft, EntityDraft
from trellis.schemas.trace import Trace
from trellis.schemas.well_known import (
    AGENT,
    APPLIES_TO,
    CONCEPT,
    CREATIVE_WORK,
    DATASET,
    SOFTWARE_APPLICATION,
    TEAM,
    USED,
    WAS_ASSOCIATED_WITH,
    WAS_ATTRIBUTED_TO,
    WAS_GENERATED_BY,
    WAS_INFORMED_BY,
)

# ---------------------------------------------------------------------------
# Worked examples from docs/agent-guide/trace-format.md
# ---------------------------------------------------------------------------

EXAMPLE_1: dict = {
    "source": "agent",
    "intent": "Find and fix the broken import in auth_service.py",
    "steps": [
        {
            "step_type": "tool_call",
            "name": "search_codebase",
            "args": {"query": "from auth_service import", "file_pattern": "*.py"},
            "result": {"matches": 3},
            "duration_ms": 450,
        },
        {
            "step_type": "tool_call",
            "name": "edit_file",
            "args": {"file": "api/routes.py"},
            "result": {"status": "applied"},
            "duration_ms": 120,
        },
    ],
    "outcome": {
        "status": "success",
        "metrics": {"files_changed": 1},
        "summary": "Fixed broken import",
    },
    "context": {
        "agent_id": "code-orchestrator",
        "domain": "backend",
        "started_at": "2026-03-10T14:30:00Z",
        "ended_at": "2026-03-10T14:30:05Z",
    },
}

EXAMPLE_2: dict = {
    "source": "workflow",
    "intent": "Deploy v2.3.1 to staging and run smoke tests",
    "steps": [
        {"step_type": "tool_call", "name": "git_tag", "args": {}, "result": {}},
        {"step_type": "tool_call", "name": "deploy", "args": {}, "result": {}},
        {
            "step_type": "tool_call",
            "name": "run_smoke_tests",
            "args": {},
            "result": {"passed": 18, "failed": 2},
            "error": "2 smoke tests failed",
        },
    ],
    "evidence_used": [
        {"evidence_id": "ev_01JRK5N7QF8GHTM2XVZP3CWD9E", "role": "reference"}
    ],
    "artifacts_produced": [
        {"artifact_id": "deploy_staging_v2.3.1", "artifact_type": "deployment"}
    ],
    "outcome": {"status": "partial", "summary": "Deployed but 2 smoke tests failed"},
    "context": {
        "workflow_id": "deploy_staging",
        "domain": "platform",
        "team": "infra",
    },
}

EXAMPLE_3: dict = {
    "source": "human",
    "intent": "Review and approve PR #847 for the billing refactor",
    "steps": [
        {"step_type": "decision", "name": "review_pr", "args": {}, "result": {}},
        {"step_type": "observation", "name": "note_risk", "args": {}, "result": {}},
    ],
    "outcome": {"status": "success", "summary": "PR #847 approved"},
    "feedback": [
        {"rating": 0.85, "label": "good", "given_by": "tech-lead"},
    ],
    "context": {
        "agent_id": "nathan",
        "domain": "billing",
        "team": "payments",
    },
}


def _entity_by_id(entities: list[EntityDraft], entity_id: str) -> EntityDraft:
    for e in entities:
        if e.entity_id == entity_id:
            return e
    msg = f"entity {entity_id!r} not found"
    raise AssertionError(msg)


def _has_edge(edges: list[EdgeDraft], source: str, kind: str, target: str) -> bool:
    return any(
        e.source_id == source and e.edge_kind == kind and e.target_id == target
        for e in edges
    )


# ---------------------------------------------------------------------------
# Protocol / metadata
# ---------------------------------------------------------------------------


class TestExtractorMetadata:
    def test_tier_is_deterministic(self) -> None:
        assert TraceExtractor().tier is ExtractorTier.DETERMINISTIC

    def test_default_supported_sources(self) -> None:
        assert TraceExtractor().supported_sources == [TRACE_SOURCE_HINT]

    def test_conforms_to_extractor_protocol(self) -> None:
        from trellis.extract.base import Extractor

        assert isinstance(TraceExtractor(), Extractor)


# ---------------------------------------------------------------------------
# Example 1 — simple agent tool call
# ---------------------------------------------------------------------------


class TestExample1:
    async def test_activity_entity(self) -> None:
        trace = Trace.model_validate(EXAMPLE_1)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        activity = _entity_by_id(result.entities, f"trace:{trace.trace_id}")
        assert activity.entity_type == "Activity"
        assert activity.name == EXAMPLE_1["intent"]
        assert activity.properties["outcome_status"] == "success"
        assert activity.properties["trace_source"] == "agent"

    async def test_agent_entity_and_edge(self) -> None:
        trace = Trace.model_validate(EXAMPLE_1)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        agent = _entity_by_id(result.entities, "agent:code-orchestrator")
        assert agent.entity_type == AGENT
        assert _has_edge(
            result.edges,
            f"trace:{trace.trace_id}",
            WAS_ATTRIBUTED_TO,
            "agent:code-orchestrator",
        )

    async def test_domain_concept_and_edge(self) -> None:
        trace = Trace.model_validate(EXAMPLE_1)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        domain = _entity_by_id(result.entities, "domain:backend")
        assert domain.entity_type == CONCEPT
        assert _has_edge(
            result.edges, f"trace:{trace.trace_id}", APPLIES_TO, "domain:backend"
        )

    async def test_tool_entities_and_used_edges(self) -> None:
        trace = Trace.model_validate(EXAMPLE_1)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        tool = _entity_by_id(result.entities, "tool:search_codebase")
        assert tool.entity_type == SOFTWARE_APPLICATION
        assert _has_edge(
            result.edges, f"trace:{trace.trace_id}", USED, "tool:search_codebase"
        )
        assert _has_edge(
            result.edges, f"trace:{trace.trace_id}", USED, "tool:edit_file"
        )

    async def test_no_evidence_or_artifacts(self) -> None:
        trace = Trace.model_validate(EXAMPLE_1)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        ids = {e.entity_id for e in result.entities}
        assert not any(i.startswith("evidence:") for i in ids)
        assert not any(i.startswith("artifact:") for i in ids)


# ---------------------------------------------------------------------------
# Example 2 — multi-step workflow with evidence + artifacts
# ---------------------------------------------------------------------------


class TestExample2:
    async def test_team_entity_and_edge(self) -> None:
        trace = Trace.model_validate(EXAMPLE_2)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        team = _entity_by_id(result.entities, "team:infra")
        assert team.entity_type == TEAM
        assert _has_edge(
            result.edges, f"trace:{trace.trace_id}", WAS_ASSOCIATED_WITH, "team:infra"
        )

    async def test_evidence_dataset_and_used_edge(self) -> None:
        trace = Trace.model_validate(EXAMPLE_2)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        ev_id = "evidence:ev_01JRK5N7QF8GHTM2XVZP3CWD9E"
        ev = _entity_by_id(result.entities, ev_id)
        assert ev.entity_type == DATASET
        assert ev.properties["evidence_role"] == "reference"
        assert _has_edge(result.edges, f"trace:{trace.trace_id}", USED, ev_id)

    async def test_artifact_generated_by_edge(self) -> None:
        trace = Trace.model_validate(EXAMPLE_2)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        art_id = "artifact:deploy_staging_v2.3.1"
        art = _entity_by_id(result.entities, art_id)
        # 'deployment' is not a File/document token -> CreativeWork
        assert art.entity_type == CREATIVE_WORK
        assert art.properties["artifact_type"] == "deployment"
        # Generation-style edge points artifact -> activity
        assert _has_edge(
            result.edges, art_id, WAS_GENERATED_BY, f"trace:{trace.trace_id}"
        )

    async def test_workflow_id_recorded_on_activity(self) -> None:
        trace = Trace.model_validate(EXAMPLE_2)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        activity = _entity_by_id(result.entities, f"trace:{trace.trace_id}")
        assert activity.properties["workflow_id"] == "deploy_staging"

    async def test_no_agent_when_agent_id_absent(self) -> None:
        trace = Trace.model_validate(EXAMPLE_2)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        ids = {e.entity_id for e in result.entities}
        assert not any(i.startswith("agent:") for i in ids)


# ---------------------------------------------------------------------------
# Example 3 — human review, no tool_call steps
# ---------------------------------------------------------------------------


class TestExample3:
    async def test_no_tool_entities_for_non_tool_steps(self) -> None:
        trace = Trace.model_validate(EXAMPLE_3)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        ids = {e.entity_id for e in result.entities}
        # decision / observation steps don't produce SoftwareApplication nodes
        assert not any(i.startswith("tool:") for i in ids)

    async def test_human_source_still_attributes_to_agent(self) -> None:
        trace = Trace.model_validate(EXAMPLE_3)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        # context.agent_id="nathan" -> Agent, regardless of source=human
        assert _entity_by_id(result.entities, "agent:nathan").entity_type == AGENT


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


class TestCanonicalization:
    async def test_entity_types_are_canonical(self) -> None:
        trace = Trace.model_validate(EXAMPLE_2)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        # Every emitted type is a canonical PascalCase well-known name.
        types = {e.entity_type for e in result.entities}
        assert types <= {
            "Activity",
            AGENT,
            TEAM,
            CONCEPT,
            SOFTWARE_APPLICATION,
            DATASET,
            CREATIVE_WORK,
        }

    async def test_edge_kinds_are_canonical(self) -> None:
        trace = Trace.model_validate(EXAMPLE_2)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        kinds = {e.edge_kind for e in result.edges}
        assert kinds <= {
            USED,
            WAS_ATTRIBUTED_TO,
            WAS_ASSOCIATED_WITH,
            APPLIES_TO,
            WAS_GENERATED_BY,
            WAS_INFORMED_BY,
        }

    async def test_schema_alignment_populated_for_aligned_types(self) -> None:
        trace = Trace.model_validate(EXAMPLE_1)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        agent = _entity_by_id(result.entities, "agent:code-orchestrator")
        assert agent.properties["schema_alignment"] == "prov:Agent"

    async def test_schema_alignment_populated_for_aligned_edges(self) -> None:
        trace = Trace.model_validate(EXAMPLE_1)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        used_edge = next(e for e in result.edges if e.edge_kind == USED)
        assert used_edge.properties["schema_alignment"] == "prov:used"


# ---------------------------------------------------------------------------
# Provenance stamps (locked decision #4)
# ---------------------------------------------------------------------------


class TestProvenanceStamps:
    async def test_every_entity_carries_provenance(self) -> None:
        trace = Trace.model_validate(EXAMPLE_1)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        for entity in result.entities:
            assert entity.properties["source_trace_id"] == trace.trace_id
            assert entity.properties["agent_id"] == "code-orchestrator"
            assert entity.properties["extractor_tier"] == "deterministic"

    async def test_every_edge_carries_provenance(self) -> None:
        trace = Trace.model_validate(EXAMPLE_1)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        assert result.edges  # guard
        for edge in result.edges:
            assert edge.properties["source_trace_id"] == trace.trace_id
            assert edge.properties["agent_id"] == "code-orchestrator"
            assert edge.properties["extractor_tier"] == "deterministic"

    async def test_agent_id_is_none_when_absent(self) -> None:
        trace = Trace.model_validate(EXAMPLE_2)  # no agent_id
        result = await TraceExtractor().extract(trace, source_hint="trace")
        activity = _entity_by_id(result.entities, f"trace:{trace.trace_id}")
        assert activity.properties["agent_id"] is None


# ---------------------------------------------------------------------------
# Parent trace, de-dup, edge cross-batch tolerance, residue handling
# ---------------------------------------------------------------------------


class TestParentTrace:
    async def test_parent_trace_informed_by_edge(self) -> None:
        data = dict(EXAMPLE_1)
        data["context"] = {**EXAMPLE_1["context"], "parent_trace_id": "parent-123"}
        trace = Trace.model_validate(data)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        assert _has_edge(
            result.edges, f"trace:{trace.trace_id}", WAS_INFORMED_BY, "trace:parent-123"
        )


class TestDeduplication:
    async def test_repeated_tool_emits_single_entity(self) -> None:
        data = {
            "source": "agent",
            "intent": "run the same tool twice",
            "steps": [
                {"step_type": "tool_call", "name": "grep", "args": {}, "result": {}},
                {"step_type": "tool_call", "name": "grep", "args": {}, "result": {}},
            ],
            "context": {"agent_id": "a1"},
        }
        trace = Trace.model_validate(data)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        grep_entities = [e for e in result.entities if e.entity_id == "tool:grep"]
        assert len(grep_entities) == 1


class TestEdgeProperties:
    async def test_edges_allow_dangling(self) -> None:
        trace = Trace.model_validate(EXAMPLE_1)
        result = await TraceExtractor().extract(trace, source_hint="trace")
        assert all(e.allow_dangling for e in result.edges)


class TestInputCoercion:
    async def test_accepts_dict_input(self) -> None:
        result = await TraceExtractor().extract(EXAMPLE_1, source_hint="trace")
        assert any(e.entity_type == "Activity" for e in result.entities)

    async def test_accepts_json_string_input(self) -> None:
        import json

        result = await TraceExtractor().extract(
            json.dumps(EXAMPLE_1), source_hint="trace"
        )
        assert any(e.entity_type == "Activity" for e in result.entities)

    async def test_non_trace_input_yields_empty_with_residue(self) -> None:
        result = await TraceExtractor().extract({"not": "a trace"}, source_hint="trace")
        assert result.entities == []
        assert result.edges == []
        assert result.unparsed_residue is not None

    async def test_provenance_records_source_hint(self) -> None:
        result = await TraceExtractor().extract(EXAMPLE_1, source_hint="trace")
        assert result.provenance.source_hint == "trace"
        assert result.extractor_used == "trace"
        assert result.tier == ExtractorTier.DETERMINISTIC.value


@pytest.mark.parametrize("example", [EXAMPLE_1, EXAMPLE_2, EXAMPLE_3])
async def test_all_worked_examples_extract_without_error(example: dict) -> None:
    trace = Trace.model_validate(example)
    result = await TraceExtractor().extract(trace, source_hint="trace")
    # Every worked example produces at least the Activity node.
    assert any(e.entity_id == f"trace:{trace.trace_id}" for e in result.entities)
