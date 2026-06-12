"""TraceExtractor — deterministic trace→graph extraction.

Trace ingestion is write-only to the ``TraceStore`` today; an agent run
never populates the knowledge graph.  :class:`TraceExtractor` closes that
gap by mining the **structured** fields of a :class:`~trellis.schemas.trace.Trace`
into ``EntityDraft`` / ``EdgeDraft`` records that flow through the governed
``MutationExecutor`` (the extractor itself is PURE — it never touches a
store).

Field → entity / edge mapping
------------------------------

The deterministic tier reads only fields that genuinely exist on the
``Trace`` / ``TraceStep`` schemas (``src/trellis/schemas/trace.py``).  No
field is invented; ambiguous free-text (``intent`` prose, ``step.args`` /
``step.result`` payload mining) is deliberately left to a future LLM
residue pass (see module footer).

Entities

* **Activity** — the trace itself (``trace:<trace_id>``), named by
  ``intent``.  PROV-O ``Activity``.
* **Agent** — ``context.agent_id`` (``agent:<agent_id>``).  PROV-O ``Agent``.
* **Team** — ``context.team`` (``team:<team>``).
* **Concept** — ``context.domain`` (``domain:<domain>``).  ``domain`` is
  intentionally *not* a well-known entity type (it collides with the
  ContentTags.domain facet), so the scope is modeled as a ``Concept``.
* **SoftwareApplication** — the tool invoked by each ``tool_call`` step,
  keyed by ``step.name`` (``tool:<name>``).
* **File / CreativeWork** — each ``artifacts_produced`` ref
  (``artifact:<artifact_id>``); type derived from ``artifact_type``.
* **Dataset** — each ``evidence_used`` ref (``evidence:<evidence_id>``).

Edges (PROV-aligned well-known kinds)

* Activity ``wasAttributedTo`` Agent.
* Activity ``wasAssociatedWith`` Team.
* Activity ``appliesTo`` domain Concept.
* Activity ``used`` tool SoftwareApplication (one per distinct tool).
* Activity ``used`` evidence Dataset.
* artifact ``wasGeneratedBy`` Activity (generation-style).
* Activity ``wasInformedBy`` parent Activity (``context.parent_trace_id``).

Every emitted draft carries property-based provenance — ``source_trace_id``,
``agent_id``, ``extractor_tier`` — so a downstream consumer can attribute
any node or edge back to the trace that produced it without a column-schema
change (column promotion is roadmap item B.3, out of scope here).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trellis.extract.base import ExtractorTier
from trellis.schemas.enums import NodeRole
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)
from trellis.schemas.trace import Trace
from trellis.schemas.well_known import (
    AGENT,
    APPLIES_TO,
    CONCEPT,
    CREATIVE_WORK,
    DATASET,
    FILE,
    SOFTWARE_APPLICATION,
    TEAM,
    USED,
    WAS_ASSOCIATED_WITH,
    WAS_ATTRIBUTED_TO,
    WAS_GENERATED_BY,
    WAS_INFORMED_BY,
    canonicalize_edge_kind,
    canonicalize_entity_type,
    schema_alignment_for_edge_kind,
    schema_alignment_for_entity_type,
)

if TYPE_CHECKING:
    from trellis.extract.context import ExtractionContext

#: Default ``source_hint`` the dispatcher routes on for this extractor.
TRACE_SOURCE_HINT = "trace"

#: ``step_type`` values that name a tool worth modeling as a
#: ``SoftwareApplication`` the trace ``used``.  Kept as an open set — any
#: other step type contributes no tool entity (its prose lives in
#: ``args`` / ``result`` and is LLM-residue territory).
_TOOL_STEP_TYPES = frozenset({"tool_call"})

#: ``artifact_type`` tokens that map onto the well-known ``File`` entity
#: type.  Everything else falls back to ``CreativeWork`` — both are
#: schema.org-aligned so RDF/JSON-LD export stays clean.
_FILE_ARTIFACT_TYPES = frozenset({"file", "document"})


class TraceExtractor:
    """Deterministic structured-field extractor for :class:`Trace` records.

    Stateless at call time — safe to share across concurrent ``extract``
    calls.  Conforms to the
    :class:`~trellis.extract.base.Extractor` protocol and registers at
    tier :attr:`~trellis.extract.base.ExtractorTier.DETERMINISTIC`.

    Accepts either a :class:`Trace` instance or a trace-shaped ``dict``
    / JSON string as ``raw_input`` (the CLI/MCP/API layers already hold a
    validated ``Trace``; the dict path lets the backfill command pass
    stored rows straight through).  Unparseable input yields an empty
    result rather than raising — the dispatcher owns failure telemetry.
    """

    tier = ExtractorTier.DETERMINISTIC

    def __init__(
        self,
        name: str = "trace",
        *,
        supported_sources: list[str] | None = None,
        version: str = "0.1.0",
    ) -> None:
        self.name = name
        self.supported_sources = list(
            supported_sources if supported_sources is not None else [TRACE_SOURCE_HINT]
        )
        self.version = version

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        del context  # deterministic extractor has no cost budget

        trace = _coerce_trace(raw_input)
        provenance = ExtractionProvenance(
            extractor_name=self.name,
            extractor_version=self.version,
            source_hint=source_hint,
        )
        if trace is None:
            return ExtractionResult(
                entities=[],
                edges=[],
                extractor_used=self.name,
                tier=self.tier.value,
                provenance=provenance,
                unparsed_residue={"reason": "input is not a trace"},
            )

        builder = _DraftBuilder(trace)
        builder.run()
        return ExtractionResult(
            entities=builder.entities,
            edges=builder.edges,
            extractor_used=self.name,
            tier=self.tier.value,
            provenance=provenance,
        )


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _coerce_trace(raw_input: Any) -> Trace | None:
    """Best-effort coercion of ``raw_input`` into a :class:`Trace`.

    Returns ``None`` for anything that can't be validated as a trace — the
    extractor surfaces that via ``unparsed_residue`` instead of raising,
    matching the extractor contract (deterministic parse, no exceptions
    for recoverable mismatches).
    """
    if isinstance(raw_input, Trace):
        return raw_input
    try:
        if isinstance(raw_input, str):
            return Trace.model_validate_json(raw_input)
        if isinstance(raw_input, dict):
            return Trace.model_validate(raw_input)
    except Exception:
        return None
    return None


class _DraftBuilder:
    """Accumulates entity / edge drafts for one trace.

    Splitting the build into a stateful helper keeps ``extract`` flat and
    lets each emit-site share the provenance stamp + de-duplication index
    without threading them through every method.
    """

    def __init__(self, trace: Trace) -> None:
        self._trace = trace
        self.entities: list[EntityDraft] = []
        self.edges: list[EdgeDraft] = []
        # De-dupe entity drafts by id so a tool invoked across N steps (or
        # an artifact referenced twice) produces a single node.
        self._seen_entities: set[str] = set()

    # -- provenance ----------------------------------------------------

    def _provenance_props(self) -> dict[str, Any]:
        """Property-based provenance stamped on every draft (locked decision #4)."""
        return {
            "source_trace_id": self._trace.trace_id,
            "agent_id": self._trace.context.agent_id,
            "extractor_tier": ExtractorTier.DETERMINISTIC.value,
        }

    # -- entity / edge emit -------------------------------------------

    def _emit_entity(
        self,
        *,
        entity_id: str,
        entity_type: str,
        name: str,
        extra_props: dict[str, Any] | None = None,
    ) -> str:
        """Emit a canonicalized, provenance-stamped entity draft once.

        Returns the (stable) ``entity_id`` so callers can wire edges.
        """
        canonical_type = canonicalize_entity_type(entity_type)
        if entity_id in self._seen_entities:
            return entity_id
        self._seen_entities.add(entity_id)

        props: dict[str, Any] = self._provenance_props()
        if extra_props:
            props.update(extra_props)
        alignment = schema_alignment_for_entity_type(canonical_type)
        if alignment is not None:
            props.setdefault("schema_alignment", alignment)

        self.entities.append(
            EntityDraft(
                entity_id=entity_id,
                entity_type=canonical_type,
                name=name,
                properties=props,
                node_role=NodeRole.SEMANTIC,
            )
        )
        return entity_id

    def _emit_edge(
        self,
        *,
        source_id: str,
        target_id: str,
        edge_kind: str,
    ) -> None:
        """Emit a canonicalized, provenance-stamped edge draft.

        Drafts use ``allow_dangling=True`` so a reference to an entity that
        was extracted by a *different* trace (e.g. a parent trace's
        Activity, or shared evidence) doesn't fail FK validation in
        ``LinkCreateHandler`` — trace graphs are inherently cross-batch.
        """
        canonical_kind = canonicalize_edge_kind(edge_kind)
        props: dict[str, Any] = self._provenance_props()
        alignment = schema_alignment_for_edge_kind(canonical_kind)
        if alignment is not None:
            props.setdefault("schema_alignment", alignment)

        self.edges.append(
            EdgeDraft(
                source_id=source_id,
                target_id=target_id,
                edge_kind=canonical_kind,
                properties=props,
                allow_dangling=True,
            )
        )

    # -- build ---------------------------------------------------------

    def run(self) -> None:
        activity_id = self._build_activity()
        self._build_agent(activity_id)
        self._build_team(activity_id)
        self._build_domain(activity_id)
        self._build_parent(activity_id)
        self._build_tools(activity_id)
        self._build_evidence(activity_id)
        self._build_artifacts(activity_id)

    def _build_activity(self) -> str:
        ctx = self._trace.context
        outcome = self._trace.outcome
        extra: dict[str, Any] = {
            "trace_source": self._trace.source.value,
            "intent": self._trace.intent,
        }
        if outcome is not None:
            extra["outcome_status"] = outcome.status.value
        if ctx.workflow_id is not None:
            extra["workflow_id"] = ctx.workflow_id
        return self._emit_entity(
            entity_id=f"trace:{self._trace.trace_id}",
            entity_type="Activity",
            name=self._trace.intent,
            extra_props=extra,
        )

    def _build_agent(self, activity_id: str) -> None:
        agent_id = self._trace.context.agent_id
        if not agent_id:
            return
        entity_id = self._emit_entity(
            entity_id=f"agent:{agent_id}",
            entity_type=AGENT,
            name=agent_id,
        )
        # PROV: an activity wasAttributedTo... is for entities; for the
        # agent we use wasAttributedTo to record "this run is the work of
        # this agent" — the closest PROV verb Trellis aligns on the
        # trace→agent direction.
        self._emit_edge(
            source_id=activity_id,
            target_id=entity_id,
            edge_kind=WAS_ATTRIBUTED_TO,
        )

    def _build_team(self, activity_id: str) -> None:
        team = self._trace.context.team
        if not team:
            return
        entity_id = self._emit_entity(
            entity_id=f"team:{team}",
            entity_type=TEAM,
            name=team,
        )
        self._emit_edge(
            source_id=activity_id,
            target_id=entity_id,
            edge_kind=WAS_ASSOCIATED_WITH,
        )

    def _build_domain(self, activity_id: str) -> None:
        domain = self._trace.context.domain
        if not domain:
            return
        entity_id = self._emit_entity(
            entity_id=f"domain:{domain}",
            entity_type=CONCEPT,
            name=domain,
        )
        self._emit_edge(
            source_id=activity_id,
            target_id=entity_id,
            edge_kind=APPLIES_TO,
        )

    def _build_parent(self, activity_id: str) -> None:
        parent = self._trace.context.parent_trace_id
        if not parent:
            return
        # The parent Activity is (almost always) extracted by a different
        # trace run; reference it by the same stable id scheme and let
        # allow_dangling carry the cross-batch edge.
        self._emit_edge(
            source_id=activity_id,
            target_id=f"trace:{parent}",
            edge_kind=WAS_INFORMED_BY,
        )

    def _build_tools(self, activity_id: str) -> None:
        for step in self._trace.steps:
            if step.step_type not in _TOOL_STEP_TYPES:
                continue
            if not step.name:
                continue
            entity_id = self._emit_entity(
                entity_id=f"tool:{step.name}",
                entity_type=SOFTWARE_APPLICATION,
                name=step.name,
            )
            # _emit_edge de-dupes nothing, but _emit_entity does — guard the
            # edge against the same tool used across multiple steps so we
            # don't emit N identical `used` edges.
            self._emit_edge(
                source_id=activity_id,
                target_id=entity_id,
                edge_kind=USED,
            )

    def _build_evidence(self, activity_id: str) -> None:
        for ref in self._trace.evidence_used:
            entity_id = self._emit_entity(
                entity_id=f"evidence:{ref.evidence_id}",
                entity_type=DATASET,
                name=ref.evidence_id,
                extra_props={"evidence_role": ref.role},
            )
            self._emit_edge(
                source_id=activity_id,
                target_id=entity_id,
                edge_kind=USED,
            )

    def _build_artifacts(self, activity_id: str) -> None:
        for ref in self._trace.artifacts_produced:
            entity_type = (
                FILE
                if ref.artifact_type.lower() in _FILE_ARTIFACT_TYPES
                else CREATIVE_WORK
            )
            entity_id = self._emit_entity(
                entity_id=f"artifact:{ref.artifact_id}",
                entity_type=entity_type,
                name=ref.artifact_id,
                extra_props={"artifact_type": ref.artifact_type},
            )
            # Generation-style PROV edge: artifact wasGeneratedBy activity.
            self._emit_edge(
                source_id=entity_id,
                target_id=activity_id,
                edge_kind=WAS_GENERATED_BY,
            )


# ----------------------------------------------------------------------
# Follow-up — LLM residue (deliberately deferred)
# ----------------------------------------------------------------------
#
# The deterministic tier above mines only structured fields. The free-text
# residue — ``intent`` prose, ``step.args`` / ``step.result`` payloads,
# ``outcome.summary`` — carries entity mentions (people, systems, files
# named in passing) that no rule can resolve. Wiring an opt-in LLM residue
# pass is a clean follow-up: build a ``HybridJSONExtractor`` wrapping this
# extractor (deterministic) plus the existing ``LLMExtractor`` (LLM tier),
# exactly as ``build_save_memory_extractor`` does, gated behind
# ``allow_llm_fallback`` so it can never silently substitute for the
# deterministic path. Left out here per WP6 decision #3.
