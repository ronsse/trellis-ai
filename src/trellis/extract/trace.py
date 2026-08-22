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
residue pass (see module footer).  The *verifiable* subset of the step
payloads — files touched, files read, commands run — is the exception:
``trellis.extract.evidence`` parses it deterministically and stamps it
onto the Activity draft at the shared ingest-hook seam (#308), so those
fields never depend on an LLM's recollection.

Entities

* **Activity** — the trace itself (``trace:<trace_id>``), named by
  ``intent``.  PROV-O ``Activity``.
* **Agent** — ``context.agent_id`` (``agent:<agent_id>``).  PROV-O ``Agent``.
* **Team** — ``context.team`` (``team:<team>``).
* **Concept** — ``context.domain`` (``domain:<domain>``).  ``domain`` is
  intentionally *not* a well-known entity type (it collides with the
  ContentTags.domain facet), so the scope is modeled as a ``Concept``.
* **SoftwareApplication** — the tool invoked by each ``tool_call`` step,
  keyed by ``step.name`` (``tool:<slug>``).  Minted at
  ``NodeRole.STRUCTURAL`` — see "Node role" below.
* **File / CreativeWork** — each ``artifacts_produced`` ref
  (``artifact:<artifact_id>``); type derived from ``artifact_type``.
* **Dataset** — each ``evidence_used`` ref (``evidence:<evidence_id>``).

ID normalization
----------------

Free-text names arrive in whatever spelling the agent used — ``Bash``,
``bash``, ``mcp__trellis__search``, ``Search Codebase``.  Minting an id
straight from the raw string turns every spelling into its own permanent
node, which is where the bulk of the graph's duplicate junk comes from.
:func:`normalize_slug` collapses the spellings into one id: NFKC
normalize, casefold, then replace every run of non-alphanumeric
characters with a single ``-``.  ``mcp__trellis__search`` →
``mcp-trellis-search``; ``Bash`` / ``bash`` → ``bash``.

Only *name-derived* ids are normalized — ``tool:``, ``agent:``,
``team:``, ``domain:``.  ``evidence:`` / ``artifact:`` / ``trace:`` ids
are opaque caller-supplied identifiers (ULIDs, paths, tags): normalizing
them would break the join back to the thing they reference, and they
don't suffer from spelling drift in the first place.  The *display name*
always stays the raw string — an operator has to be able to recognise
the node.

Node role
---------

Tool nodes are minted ``NodeRole.STRUCTURAL``.  They are exactly what
that role is for: fine-grained, machine-generated plumbing regenerated
from source on every trace, carrying no standalone content (a ``bash``
node is three words long).  The role makes them invisible to the pack
builder's existing default ``node_role == "structural"`` filter while
leaving them fully traversable in the graph.

Every other minted node stays ``SEMANTIC``, deliberately.  The Activity
node *is* the trace — demoting it would hide trace memory from packs
entirely, which is the opposite of the point — and ``evidence:`` /
``artifact:`` nodes point at real files and datasets that an operator
searches for by name.  Only the tool node is pure plumbing.

``node_role`` is **immutable across SCD-2 versions** (see
``check_node_role_immutable``), so a ``tool:<slug>`` node that a previous
release already wrote as ``SEMANTIC`` cannot be promoted in place.
:func:`trellis.extract.commands.reconcile_node_roles` detects that
collision before submission and keeps the stored role, logging the node
id — see its docstring for the migration.

Document links
--------------

A trace is not a document, so there is nothing to link by default.  When
the ingest path that produced the trace *did* render it into the
``DocumentStore``, it names the row in ``trace.metadata`` (either
``document_ids: list[str]`` or ``document_id: str``); that link is
carried onto the Activity node — the one node that *is* this trace.  It
is deliberately not fanned out to the tool / agent / team nodes: those
are shared across traces and a supplied ``document_ids`` *replaces*
rather than merges the stored link, so fanning out would leave every
shared node pointing at whichever trace happened to be extracted last.

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

import re
import unicodedata
from typing import TYPE_CHECKING, Any

import structlog

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

logger = structlog.get_logger(__name__)

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

#: ``trace.metadata`` keys an ingest path may use to name the
#: ``DocumentStore`` row(s) it rendered the trace into.  Plural wins when
#: both are present.  Anything that isn't a non-empty string is ignored —
#: a bogus pointer is worse than no pointer.
_METADATA_DOCUMENT_IDS_KEY = "document_ids"
_METADATA_DOCUMENT_ID_KEY = "document_id"


#: Runs of everything that isn't a unicode word character (plus ``_``,
#: which ``\w`` counts as one).  Splitting on this is what collapses
#: ``mcp__trellis__search`` and ``Search Codebase`` onto one slug shape.
_SLUG_SEPARATORS = re.compile(r"[\W_]+")


def normalize_slug(value: str) -> str:
    """Collapse a free-text name into a stable, case-insensitive id slug.

    NFKC-normalize, casefold, then join every run of alphanumeric
    characters with a single ``-``.  The split is unicode-aware, so a
    non-ASCII name still yields a meaningful slug rather than a row of
    separators (``Zażółć gęślą`` → ``zażółć-gęślą``).

    Returns ``""`` when *value* holds no alphanumeric character at all;
    callers skip the entity rather than mint an id with an empty tail.

    Idempotent: ``normalize_slug(normalize_slug(x)) == normalize_slug(x)``.

    .. note::
       Two narrower slug helpers predate this one and are deliberately
       *not* collapsed onto it: ``learning.scoring._slugify`` (ASCII-only)
       and ``trellis_workers.enrichment.service.normalize_tag`` (keeps
       ``/``).  Both feed already-persisted identifiers, so widening them
       to this unicode-aware rule would silently re-key stored rows.
       Prefer this one for new call sites.
    """
    folded = unicodedata.normalize("NFKC", value).casefold()
    return "-".join(part for part in _SLUG_SEPARATORS.split(folded) if part)


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
        # ...and the matching edge index, keyed by the full edge identity.
        # Without it a 40-step trace emits 40 identical `used` edges to the
        # one tool node the entity index collapsed.
        self._seen_edges: set[tuple[str, str, str]] = set()

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
        node_role: NodeRole = NodeRole.SEMANTIC,
        document_ids: list[str] | None = None,
    ) -> str:
        """Emit a canonicalized, provenance-stamped entity draft once.

        Returns the (stable) ``entity_id`` so callers can wire edges.

        ``node_role`` defaults to ``SEMANTIC`` — the caller opts a node
        down to ``STRUCTURAL`` when it is regenerated plumbing rather
        than a thing in the world (see the module docstring).
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
                node_role=node_role,
                document_ids=document_ids,
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
        """Emit a canonicalized, provenance-stamped edge draft once.

        De-duplicated on ``(source, kind, target)`` *after*
        canonicalization, so the same relationship restated across N
        steps — 40 ``tool_call`` steps naming the same tool — yields one
        edge, matching what :meth:`_emit_entity` already does for nodes.

        Drafts use ``allow_dangling=True`` so a reference to an entity that
        was extracted by a *different* trace (e.g. a parent trace's
        Activity, or shared evidence) doesn't fail FK validation in
        ``LinkCreateHandler`` — trace graphs are inherently cross-batch.
        """
        canonical_kind = canonicalize_edge_kind(edge_kind)
        identity = (source_id, canonical_kind, target_id)
        if identity in self._seen_edges:
            return
        self._seen_edges.add(identity)

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
            document_ids=self._trace_document_ids(),
        )

    def _trace_document_ids(self) -> list[str] | None:
        """Document row(s) this trace was rendered into, if any.

        Reads ``trace.metadata`` (``document_ids`` list, else
        ``document_id`` string).  ``None`` when the trace names none —
        the common case, since trace ingest does not write a document.
        """
        metadata = self._trace.metadata
        raw = metadata.get(_METADATA_DOCUMENT_IDS_KEY)
        if isinstance(raw, str):
            raw = [raw]
        elif not isinstance(raw, list):
            single = metadata.get(_METADATA_DOCUMENT_ID_KEY)
            raw = [single] if isinstance(single, str) else []
        # De-dup while preserving order: validate_document_ids rejects
        # repeats outright, and a duplicated pointer is a caller typo we
        # can absorb rather than fail the whole extraction on.
        return list(dict.fromkeys(d for d in raw if isinstance(d, str) and d)) or None

    def _slug_or_none(self, raw: str, *, namespace: str) -> str | None:
        """Slug *raw*, or ``None`` (logged) when it has no alphanumerics.

        Dropping a punctuation-only name is the right call — it would
        otherwise mint ``agent:`` — but doing it silently means an entity
        that vanishes from the graph has no explanation anywhere.  Log the
        raw name and the id namespace so the drop is greppable.
        """
        slug = normalize_slug(raw)
        if not slug:
            logger.info(
                "trace_extraction_name_not_sluggable",
                trace_id=self._trace.trace_id,
                namespace=namespace,
                raw_name=raw,
            )
            return None
        return slug

    def _build_agent(self, activity_id: str) -> None:
        agent_id = self._trace.context.agent_id
        if not agent_id:
            return
        slug = self._slug_or_none(agent_id, namespace="agent")
        if not slug:
            return
        entity_id = self._emit_entity(
            entity_id=f"agent:{slug}",
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
        slug = self._slug_or_none(team, namespace="team")
        if not slug:
            return
        entity_id = self._emit_entity(
            entity_id=f"team:{slug}",
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
        slug = self._slug_or_none(domain, namespace="domain")
        if not slug:
            return
        entity_id = self._emit_entity(
            entity_id=f"domain:{slug}",
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
            slug = self._slug_or_none(step.name, namespace="tool")
            if not slug:
                continue
            entity_id = self._emit_entity(
                entity_id=f"tool:{slug}",
                entity_type=SOFTWARE_APPLICATION,
                # Display name stays raw — `mcp__trellis__search` is what
                # the operator will recognise, `mcp-trellis-search` isn't.
                name=step.name,
                node_role=NodeRole.STRUCTURAL,
            )
            # Both indexes are in play here: _emit_entity collapses the tool
            # node across steps, _emit_edge collapses the matching `used`
            # edge so a 40-step trace doesn't restate it 40 times.
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
