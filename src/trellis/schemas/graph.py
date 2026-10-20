"""Graph edge schema for Trellis."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from trellis.core.base import TimestampedModel, TrellisModel, VersionedModel
from trellis.core.ids import generate_ulid
from trellis.schemas._type_warnings import warn_if_near_miss_edge_kind

#: Allowed values for the ``extractor_tier`` provenance column on edges.
#: Mirrors the routing labels used by
#: :class:`trellis.extract.base.ExtractorTier`, normalised to upper-case
#: for storage. The validation is open-vocabulary at the API edge (any
#: string) but the schema layer constrains it to this set so retrieval
#: queries can filter on the tier without normalisation gymnastics.
#:
#: This is the *stored* spelling. Writers do not all stamp it: trace
#: extraction stamps the lower-case ``ExtractorTier`` value into an edge's
#: ``properties``, and the legacy-lift migration moves it into the typed
#: column, so every boundary that takes a tier passes it through
#: :func:`canonical_extractor_tier` first.
#:
#: Kept here rather than importing from ``trellis.extract`` to avoid a
#: layering cycle (``schemas`` must not depend on ``extract``);
#: ``tests/unit/schemas/test_graph.py`` pins the two against each other.
ALLOWED_EXTRACTOR_TIERS: frozenset[str] = frozenset({"DETERMINISTIC", "HYBRID", "LLM"})

#: The one other spelling a writer produces: the lower-case
#: ``ExtractorTier`` values. A closed map, not a case fold.
_EXTRACTOR_TIER_SPELLINGS: dict[str, str] = {
    **{tier: tier for tier in ALLOWED_EXTRACTOR_TIERS},
    "deterministic": "DETERMINISTIC",
    "hybrid": "HYBRID",
    "llm": "LLM",
}


def canonical_extractor_tier(value: str) -> str:
    """Return the stored (upper-case) spelling of an extractor tier.

    Accepts exactly the six spellings in use: the canonical members of
    :data:`ALLOWED_EXTRACTOR_TIERS` and the lower-case
    ``ExtractorTier`` values the extractors stamp. The map is closed and
    lossless. It is deliberately not ``str.upper()``: a mixed-case
    ``"Llm"``, a padded ``"llm "`` or an unknown ``"heuristic"`` is still
    malformed, because folding case would let any near-miss through.

    Raises:
        ValueError: *value* is not one of the six accepted spellings.
    """
    canonical = _EXTRACTOR_TIER_SPELLINGS.get(value)
    if canonical is None:
        msg = (
            f"extractor_tier must be one of "
            f"{sorted(ALLOWED_EXTRACTOR_TIERS)}, got {value!r}"
        )
        raise ValueError(msg)
    return canonical


class Edge(TimestampedModel, VersionedModel):
    """A directed edge in the experience graph.

    ``edge_kind`` is an **open string**. The ``EdgeKind`` enum in
    ``schemas/enums.py`` lists well-known agent-centric values
    (``trace_used_evidence``, ``entity_depends_on``, ...); domain-specific
    integrations pass their own strings (``uc_column_of``,
    ``dbt_references``, ...) and are accepted verbatim by the storage
    layer. Do not add domain-specific kinds to the core enum — define
    them in the consumer package instead.

    Edges optionally carry **provenance columns** promoted from the
    free-form ``properties`` bag by Phase 3 of ADR
    ``adr-graph-ontology.md`` (item 2 of the self-improvement program).
    All five fields default to ``None`` to keep the schema
    backwards-compatible; the storage layer reads them as NULL on rows
    written before the columns existed.

    * ``source_trace_id`` — the trace ID that minted this edge. Lets
      retrieval ask "what edges did trace X create?" without scanning
      JSON.
    * ``agent_id`` — agent identity attribution (free-form string;
      typically a tool name or a model identifier).
    * ``confidence`` — extractor's belief in this edge, in [0.0, 1.0].
      Values outside the range raise :class:`pydantic.ValidationError`
      at the schema boundary so callers see the offending value.
    * ``evidence_ref`` — opaque pointer back to the evidence that
      justifies the edge (e.g. a document or chunk ID). Free-form.
    * ``extractor_tier`` — which routing tier produced the edge; one of
      :data:`ALLOWED_EXTRACTOR_TIERS`. Stored in that spelling; the
      lower-case ``ExtractorTier`` values are accepted and canonicalised
      (:func:`canonical_extractor_tier`).
    """

    edge_id: str = Field(default_factory=generate_ulid)
    source_id: str
    target_id: str
    edge_kind: str
    properties: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # Provenance columns (Phase 3 of adr-graph-ontology §6.4 / item 2 of
    # plan-self-improvement-program). All Optional, all default None.
    # ------------------------------------------------------------------
    source_trace_id: str | None = None
    agent_id: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_ref: str | None = None
    extractor_tier: str | None = None

    @field_validator("edge_kind", mode="after")
    @classmethod
    def _warn_on_near_miss_edge_kind(cls, value: str) -> str:
        """Open-string contract preserved — value is returned verbatim.

        Emits a ``structlog`` warning under event key
        ``edge_kind.suspicious_input`` when *value* looks like a typo
        of a well-known canonical / alias / legacy enum value. Never
        raises (mirrors :class:`~trellis.schemas.entity.Entity`).
        """
        warn_if_near_miss_edge_kind(value)
        return value

    @field_validator("extractor_tier", mode="before")
    @classmethod
    def _validate_extractor_tier(cls, value: Any) -> Any:
        """Canonicalise ``extractor_tier`` and reject anything unrecognised.

        ``None`` is allowed (the column is optional and historical edges
        read back as NULL). A concrete value must be one of
        :data:`ALLOWED_EXTRACTOR_TIERS` or a lower-case ``ExtractorTier``
        value, which is stored in the canonical spelling
        (:func:`canonical_extractor_tier`) — no general case folding, no
        whitespace stripping. A ``before`` validator, because the base
        model's ``str_strip_whitespace`` runs ahead of an ``after`` one and
        would turn ``"llm "`` into ``"llm"`` before it was checked, so
        this model would accept a spelling the store boundary
        (:func:`~trellis.stores.base.edge_provenance.normalize_edge_provenance`)
        rejects. A non-string is left for pydantic's own type error.
        Validating loudly here keeps downstream filter queries
        deterministic.
        """
        if isinstance(value, str):
            return canonical_extractor_tier(value)
        return value


class CompactionReport(TrellisModel):
    """Result of a :meth:`GraphStore.compact_versions` call.

    Closes Gap 4.2. SCD Type 2 versioning leaves every closed
    (``valid_to IS NOT NULL``) row in place forever; on hot nodes this
    accumulates and degrades ``as_of`` queries. ``compact_versions``
    drops closed rows whose ``valid_to < before`` — current rows
    (``valid_to IS NULL``) are never touched.

    The report is deliberately wide enough to correlate runs with
    downstream storage/latency metrics: per-table counts, the
    ``valid_to`` range of the compacted rows, and an explicit ``dry_run``
    flag so previews and real runs emit the same event shape.
    """

    before: datetime
    """Cutoff — rows with ``valid_to < before`` are eligible."""

    nodes_compacted: int = 0
    edges_compacted: int = 0
    aliases_compacted: int = 0
    oldest_compacted_valid_to: datetime | None = None
    """Earliest ``valid_to`` among compacted rows (``None`` if nothing compacted)."""
    newest_compacted_valid_to: datetime | None = None
    """Latest ``valid_to`` among compacted rows (``None`` if nothing compacted)."""
    dry_run: bool = False
    """When ``True`` no rows were deleted; counts reflect what *would* be dropped."""
    duration_ms: int = 0

    @property
    def total_compacted(self) -> int:
        return self.nodes_compacted + self.edges_compacted + self.aliases_compacted
