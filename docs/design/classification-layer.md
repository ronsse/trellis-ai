# Classification Layer: Hybrid Deterministic + LLM Tagging

## Problem

Ingestion is pass-through today. Traces, documents, and entities enter the graph with whatever metadata the caller provides. Enrichment exists but is LLM-only, async, not in the ingestion path, and non-deterministic. The graph grows but signal doesn't improve — retrieval has a single relevance lever (`base_score * (1 + auto_importance)`) and no way to narrow search scope before scoring.

The result: more data makes retrieval slower and noisier. Pack assembly searches everything, scores everything, then budget-trims. There's no concept-level filtering, no proactive noise rejection, and no auditable classification trail.

## Design Principles

1. **Deterministic first, LLM for judgment.** Pattern-detectable metadata should never require an LLM call. LLMs handle intent, quality, and ambiguity.
2. **One pipeline, two modes.** The same `ClassifierPipeline` runs at ingestion (deterministic-only, inline, microseconds) and as async enrichment (deterministic + LLM, selective). The mode is a configuration choice, not a code path split.
3. **Graph-aware classification.** Tags propagate through edges. A Spark job connected to tables already tagged `data-pipeline` inherits that tag without re-classifying the content. Neighbors inform confidence.
4. **Same input, same output.** Deterministic classifiers are reproducible and auditable. LLM classifications carry confidence scores and can be re-run.
5. **Flat, orthogonal facets over hierarchies.** 3-4 independent dimensions with 5-15 values each. No deep taxonomy trees.
6. **The pipeline is a Protocol.** Classifiers are injected, not hardcoded — same pattern as `PolicyGate` and `CommandHandler`.

## Architecture

### One Pipeline, Two Modes

The `ClassifierPipeline` is a single service configured differently depending on when it runs:

| | **Ingestion mode** | **Enrichment mode** |
|---|---|---|
| **When** | Inline during mutation handler, before store write | Async worker, post-ingestion |
| **Classifiers** | Deterministic only (structural, keyword, source system, **graph propagation**) | Deterministic + LLM fallback |
| **LLM** | `None` — never called | `LLMFacetClassifier` — called when confidence < threshold |
| **Latency** | Microseconds | Seconds (LLM round-trip) |
| **What it sets** | `domain`, `content_type`, `signal_quality` where deterministic confidence is high | Fills gaps, adds `scope`, refines low-confidence facets, generates `auto_summary` |
| **Idempotent?** | Yes — deterministic classifiers produce same output | Yes — re-enrichment merges, doesn't overwrite high-confidence tags |

This is the same pattern as Feast's On-Demand Feature Views: same transformation definition, different execution context controlled by a flag (`write_to_online_store`). LlamaIndex's `IngestionPipeline` does the same thing — extractors accept `llm=None` to disable LLM and fall back to heuristics.

```python
# At ingestion: deterministic-only, inline
ingestion_pipeline = ClassifierPipeline(
    classifiers=[structural, keyword_domain, source_system, graph_propagator],
    llm_classifier=None,        # no LLM at ingestion
)

# As enrichment worker: deterministic + LLM fallback, async
enrichment_pipeline = ClassifierPipeline(
    classifiers=[structural, keyword_domain, source_system, graph_propagator],
    llm_classifier=llm_facet,   # fills gaps where deterministic confidence is low
    llm_threshold=0.7,          # skip LLM if deterministic confidence >= 0.7
)
```

The enrichment worker runs the same deterministic classifiers first (they're idempotent — re-running is free). Then, only for items where the deterministic layer flagged `needs_llm_review` or confidence is below threshold, the LLM fires. This means the enrichment worker naturally handles two cases:
- **New items never classified** — full pipeline runs, LLM fills gaps
- **Items already classified at ingestion** — deterministic classifiers produce same results (no-op), LLM only fills the facets that ingestion couldn't resolve

### How It Fits in the Mutation Pipeline

```
Command arrives
  -> Stage 1: Validate (existing)
  -> Stage 2: Policy Check (existing)
  -> Stage 3: Idempotency Check (existing)
  -> Stage 4: Execute handler
       handler calls ClassifierPipeline.classify(content, context)
         -> Deterministic classifiers run (microseconds)
         -> Graph propagator checks neighbor tags (fast graph query)
         -> In enrichment mode only: LLM fires if confidence < threshold
         -> Results merged into item metadata as ContentTags
       handler writes to store with enriched metadata
  -> Stage 5: Emit Event (existing)
```

### ClassifierProtocol

```python
class Classifier(Protocol):
    """Single classifier that produces tags for content."""

    @property
    def name(self) -> str:
        """Classifier name for audit trail."""
        ...

    def classify(
        self,
        content: str,
        *,
        context: ClassificationContext | None = None,
    ) -> ClassificationResult:
        """Classify content and return tagged result."""
        ...


@dataclass
class ClassificationContext:
    """Contextual hints available to classifiers."""
    title: str = ""
    source_system: str = ""       # "dbt", "unity_catalog", "git", "obsidian"
    file_path: str = ""           # original file path if available
    entity_type: str = ""         # known entity type from schema
    node_id: str = ""             # graph node ID if this item is already in the graph
    existing_tags: ContentTags | None = None  # tags from prior classification (ingestion or previous enrichment)
    existing_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClassificationResult:
    """Output of a single classifier."""
    tags: dict[str, list[str]]    # facet -> values, e.g. {"domain": ["data-pipeline"], "content_type": ["code"]}
    confidence: float = 1.0       # 1.0 for deterministic, <1.0 for LLM
    classifier_name: str = ""
    needs_llm_review: bool = False  # flag for items the deterministic layer can't handle
```

### ClassifierPipeline

```python
class ClassifierPipeline:
    """Runs classifiers in order, merges results. Single service, two modes."""

    def __init__(
        self,
        classifiers: list[Classifier],
        llm_classifier: Classifier | None = None,
        llm_threshold: float = 0.7,  # skip LLM if deterministic confidence >= this
    ) -> None:
        self._classifiers = classifiers
        self._llm_classifier = llm_classifier
        self._llm_threshold = llm_threshold

    @property
    def mode(self) -> str:
        return "enrichment" if self._llm_classifier else "ingestion"

    def classify(
        self,
        content: str,
        *,
        context: ClassificationContext | None = None,
    ) -> MergedClassification:
        # 1. Run all deterministic classifiers (including graph propagator)
        results = [c.classify(content, context=context) for c in self._classifiers]

        # 2. Merge results (later classifiers can override earlier ones)
        merged = self._merge(results)

        # 3. In enrichment mode: if any facet has low confidence, route to LLM
        if self._llm_classifier and self._should_use_llm(merged):
            llm_result = self._llm_classifier.classify(content, context=context)
            merged = self._merge_llm(merged, llm_result)

        return merged

    def _should_use_llm(self, merged: MergedClassification) -> bool:
        """LLM fires when deterministic confidence is below threshold or explicitly requested."""
        if any(r.needs_llm_review for r in merged.results):
            return True
        return merged.min_confidence < self._llm_threshold
```

### Graph-Propagated Tags

This is the fourth deterministic classifier — it doesn't look at content at all. It looks at the graph neighborhood.

**The insight:** If a Databricks job node is connected via `READS_FROM` edges to three tables all tagged `domain: ["data-pipeline"]`, that job should inherit `data-pipeline` without needing keyword matching or an LLM. High-confidence tags on neighbors are strong signals.

This follows the pattern from Neo4j GDS label propagation (seed labels + weighted relationships) and Atlan's tag propagation through lineage graphs. But we don't need a full label propagation algorithm — a single-hop neighbor vote is sufficient and keeps it fast enough for inline ingestion.

```python
class GraphNeighborClassifier:
    """Infer tags from connected nodes' existing tags."""

    def __init__(
        self,
        graph_store: GraphStore,
        min_neighbor_confidence: float = 0.8,  # only propagate from high-confidence neighbors
        min_vote_fraction: float = 0.5,         # majority of neighbors must agree
        confidence_decay: float = 0.85,          # propagated tags are slightly less confident
    ) -> None:
        self._store = graph_store
        self._min_neighbor_confidence = min_neighbor_confidence
        self._min_vote_fraction = min_vote_fraction
        self._confidence_decay = confidence_decay

    @property
    def name(self) -> str:
        return "graph_neighbor"

    def classify(self, content: str, *, context=None) -> ClassificationResult:
        if not context or not context.node_id:
            return ClassificationResult(tags={}, confidence=0.0, classifier_name=self.name)

        # Get immediate neighbors (1-hop)
        edges = self._store.get_edges(context.node_id, direction="both")
        if not edges:
            return ClassificationResult(tags={}, confidence=0.0, classifier_name=self.name)

        neighbor_ids = [e["target_id"] if e["source_id"] == context.node_id else e["source_id"] for e in edges]
        neighbors = self._store.get_nodes_bulk(neighbor_ids)

        # Collect neighbor tags by facet, weighted by their classification confidence
        facet_votes: dict[str, dict[str, float]] = {}  # facet -> {value -> vote_count}
        total_neighbors = len(neighbors)

        for node in neighbors:
            props = node.get("properties", {})
            neighbor_tags = props.get("content_tags", {})
            neighbor_conf = float(props.get("classification_confidence", 0.0))

            if neighbor_conf < self._min_neighbor_confidence:
                continue  # only propagate from high-confidence neighbors

            for facet in ["domain", "content_type", "scope"]:
                values = neighbor_tags.get(facet, [])
                if isinstance(values, str):
                    values = [values]
                for value in values:
                    facet_votes.setdefault(facet, {}).setdefault(value, 0.0)
                    facet_votes[facet][value] += 1.0

        # Keep values where majority of neighbors agree
        tags: dict[str, list[str]] = {}
        for facet, votes in facet_votes.items():
            for value, count in votes.items():
                if count / total_neighbors >= self._min_vote_fraction:
                    tags.setdefault(facet, []).append(value)

        if not tags:
            return ClassificationResult(tags={}, confidence=0.0, classifier_name=self.name)

        return ClassificationResult(
            tags=tags,
            confidence=self._confidence_decay,  # propagated = slightly less confident than direct
            classifier_name=self.name,
        )
```

**Confidence decay** is key: propagated tags get `confidence_decay` (default 0.85) applied, so they're treated as slightly less reliable than direct classification. This means:
- A directly classified `domain: ["data-pipeline"]` with confidence 0.95 won't be overridden by a propagated tag
- But a propagated tag at 0.85 is above the LLM threshold (0.7), so it won't trigger an unnecessary LLM call
- Tags that propagate across multiple hops would decay further (0.85 * 0.85 = 0.72), approaching the LLM threshold — which is correct, because multi-hop inference is less reliable

**When propagation is most valuable:**
- Lineage-connected nodes (job → table → downstream job) share domain tags
- Entity aliases resolve cross-system identity, then tags propagate to the canonical entity
- New nodes added to an existing subgraph immediately inherit context from their neighborhood

**Propagation modes** (configurable per deployment, following Atlan's pattern):

| Mode | Behavior | Use case |
|------|----------|----------|
| `lineage` | Propagate along directional edges only (source → target) | Data pipeline graphs where flow direction matters |
| `bidirectional` | Propagate across all edges regardless of direction | General knowledge graphs |
| `none` | Disabled | When you don't trust graph structure for classification |

## Tag Taxonomy

### Design: Flat Facets, Not Hierarchies

Each item gets tagged across 3-4 orthogonal dimensions. Facets compose as pre-filters:
`domain:data-pipeline AND content_type:error-resolution AND outcome:success`

Every production system we researched (Langfuse, LangSmith, OpenTelemetry) uses flat tags. Hierarchies compound classification error and create "where does this go?" ambiguity.

### Facet 1: `domain` — What area of knowledge

What it answers: "What part of the system/problem space does this relate to?"

| Value | Deterministic signals | LLM needed? |
|-------|----------------------|-------------|
| `data-pipeline` | keywords: dbt, spark, airflow, dag, etl, transform | No |
| `infrastructure` | keywords: k8s, docker, terraform, deploy, cluster, ec2 | No |
| `api` | keywords: endpoint, route, REST, GraphQL, request, response | No |
| `frontend` | keywords: react, component, CSS, DOM, render, UI | No |
| `backend` | keywords: service, handler, middleware, ORM, migration | No |
| `ml-ops` | keywords: model, training, inference, feature, embedding | No |
| `security` | keywords: auth, token, RBAC, permission, CVE, vulnerability | No |
| `observability` | keywords: log, metric, trace, alert, dashboard, latency | No |
| `testing` | keywords: test, assert, fixture, mock, coverage | No |
| `documentation` | file_path: `docs/`, `*.md`, `README`; keywords: guide, reference | No |

Multi-label: a trace about "debugging a data pipeline deployment" gets `["data-pipeline", "infrastructure"]`.

**When LLM fires:** Content about organizational process, cross-cutting concerns, or domain-ambiguous architecture decisions where keywords alone don't resolve.

### Facet 2: `content_type` — What kind of knowledge

What it answers: "What shape of information is this? What can I do with it?"

This is the most useful facet for retrieval. An agent looking for "how to fix X" wants `error-resolution`, not `pattern`.

| Value | Deterministic signals | LLM needed? |
|-------|----------------------|-------------|
| `pattern` | structured: has "approach", "strategy", repeated use across traces | Sometimes |
| `decision` | keywords: "decided", "chose", "trade-off", "ADR"; structural: pros/cons lists | Sometimes |
| `error-resolution` | keywords: error, exception, stack trace, "fixed by", "root cause" | No |
| `discovery` | keywords: "found that", "learned", "TIL", "turns out" | Sometimes |
| `procedure` | structural: numbered steps, "step 1", ordered lists, checklists | No |
| `constraint` | keywords: "must not", "always", "never", "requirement", "limitation" | Sometimes |
| `configuration` | file_path: `.yaml`, `.toml`, `.env`, `config`; structural: key-value pairs | No |
| `code` | structural: code fences, function definitions, imports | No |

**When LLM fires:** Distinguishing `pattern` from `procedure`, or `decision` from `discovery` when the text doesn't contain obvious structural signals.

### Facet 3: `scope` — How broadly applicable

What it answers: "Should this be retrieved for other projects/teams, or only this one?"

| Value | Deterministic signals | LLM needed? |
|-------|----------------------|-------------|
| `universal` | No project-specific identifiers, general programming/engineering concepts | Sometimes |
| `org` | References org-specific tools, internal URLs, team names | Sometimes |
| `project` | References specific repo, service, or module names from context | No (if entity aliases are resolved) |
| `ephemeral` | Scratch notes, debug sessions, WIP traces with no outcome | Sometimes |

**When LLM fires:** Deciding if a debugging session produced a generalizable insight (`universal`) or was a one-off fix (`project`). This is a judgment call.

### Facet 4: `signal_quality` — Should this be retrieved at all?

What it answers: "Is this worth putting in a pack, or is it noise?"

This is not a tag the user sets. It's computed from deterministic signals + feedback loop.

| Value | How determined |
|-------|---------------|
| `high` | Has a successful outcome, referenced by other traces, manually curated |
| `standard` | Default. Passes basic quality checks (non-trivial content, has structure) |
| `low` | Very short content (<50 chars), no outcome, no structure, no references |
| `noise` | Flagged by effectiveness analysis (success_rate < 0.3, appearances >= 2) |

**Deterministic only.** No LLM needed. Computed from:
- Content length and structure (trivial content = `low`)
- Outcome status from trace (success → boost, failure with resolution → boost, abandoned → lower)
- Effectiveness feedback loop (`noise_candidates` from `effectiveness.py` → `noise`)
- Reference count from graph edges (more incoming edges → `high`)

### Tag Storage

Tags are stored as first-class fields, not buried in metadata:

```python
class ContentTags(TrellisModel):
    """Classification tags attached to any stored item."""
    domain: list[str] = Field(default_factory=list)       # multi-label
    content_type: str | None = None                        # single-label
    scope: str | None = None                               # single-label
    signal_quality: str = "standard"                       # computed
    custom: dict[str, list[str]] = Field(default_factory=dict)  # extension point
    classified_by: list[str] = Field(default_factory=list) # audit: which classifiers ran
    classification_version: str = "1"                      # for re-classification
```

This goes on Document metadata, Evidence metadata, Entity properties, and Trace metadata. Not a new table — embedded in the existing metadata/properties JSON columns, but with a defined schema so retrieval can filter on it.

## How Tags Flow Through Retrieval

### Pre-Filter, Then Score

The key change to `PackBuilder` and search strategies: accept tag filters and apply them *before* similarity scoring.

```python
# Current: search everything, score, budget-trim
pack = builder.build(intent="deploy checklist", domain="platform")

# Proposed: narrow by tags first, then score within that subset
pack = builder.build(
    intent="deploy checklist",
    domain="platform",
    tag_filters={
        "domain": ["infrastructure", "observability"],
        "content_type": ["procedure", "constraint"],
        "signal_quality": ["high", "standard"],  # exclude low/noise
    },
)
```

This flows down to strategies:

- **KeywordSearch**: adds `WHERE` clauses on tag fields in the document store query
- **SemanticSearch**: passes tag filters as metadata filters to vector store (Weaviate/Pinecone/LanceDB all support this natively)
- **GraphSearch**: filters nodes by tag properties before traversal

The budget problem improves because we're scoring a focused candidate set, not everything.

### Default Behavior: Exclude Noise

If no `signal_quality` filter is specified, the default should be `["high", "standard", "low"]` — excluding `noise`. Noise items are only retrievable if explicitly requested. This is proactive noise filtering without deleting anything (traces remain immutable).

### Importance Score Enhancement

Current: `auto_importance` is a single float from the LLM.

Proposed: importance becomes a composite:

```python
def compute_importance(tags: ContentTags, base_importance: float) -> float:
    """Composite importance from tags + LLM score."""
    score = base_importance  # LLM-assigned (or 0.0 if no LLM ran)

    # Boost by signal quality
    quality_boost = {"high": 0.3, "standard": 0.0, "low": -0.2, "noise": -0.5}
    score += quality_boost.get(tags.signal_quality, 0.0)

    # Boost by scope (universal knowledge is more reusable)
    scope_boost = {"universal": 0.15, "org": 0.05, "project": 0.0, "ephemeral": -0.2}
    score += scope_boost.get(tags.scope or "project", 0.0)

    return max(0.0, min(1.0, score))
```

This gives retrieval a richer signal without requiring the LLM to make all the judgment calls.

## Deterministic Classifiers: Concrete Implementations

### 1. StructuralClassifier

Classifies based on content structure — no keyword matching, no NLP.

```python
class StructuralClassifier:
    """Classify by content structure: code fences, lists, error patterns."""

    def classify(self, content: str, *, context=None) -> ClassificationResult:
        tags = {}

        # Code detection
        if re.search(r"```\w+\n", content) or re.search(r"^(def |class |import |from )", content, re.M):
            tags["content_type"] = ["code"]

        # Procedure detection (numbered steps)
        if re.search(r"^\d+\.\s", content, re.M) and content.count("\n") > 3:
            tags["content_type"] = ["procedure"]

        # Error resolution detection
        if re.search(r"(traceback|exception|error|stack trace)", content, re.I) and \
           re.search(r"(fix|resolve|solution|root cause|fixed by)", content, re.I):
            tags["content_type"] = ["error-resolution"]

        # Configuration detection
        if context and context.file_path:
            ext = Path(context.file_path).suffix
            if ext in (".yaml", ".yml", ".toml", ".ini", ".env", ".json"):
                tags["content_type"] = ["configuration"]

        # Signal quality from length
        if len(content.strip()) < 50:
            tags["signal_quality"] = ["low"]

        return ClassificationResult(tags=tags, confidence=0.95, classifier_name="structural")
```

### 2. KeywordDomainClassifier

Maps keyword dictionaries to domain tags. Fast, auditable, configurable.

```python
class KeywordDomainClassifier:
    """Classify domain by keyword dictionary lookup."""

    # Default dictionaries — configurable via constructor
    DOMAIN_KEYWORDS: dict[str, list[str]] = {
        "data-pipeline": ["dbt", "spark", "airflow", "dag", "etl", "transform", "lineage", "warehouse"],
        "infrastructure": ["kubernetes", "k8s", "docker", "terraform", "deploy", "cluster", "ec2", "vpc"],
        "api": ["endpoint", "route", "REST", "graphql", "request", "response", "middleware"],
        "security": ["auth", "token", "rbac", "permission", "cve", "vulnerability", "encrypt"],
        "testing": ["pytest", "unittest", "assert", "fixture", "mock", "coverage", "test_"],
        "observability": ["logging", "metric", "tracing", "alert", "dashboard", "prometheus", "grafana"],
        # ... etc
    }

    def classify(self, content: str, *, context=None) -> ClassificationResult:
        content_lower = content.lower()
        matches = {}
        for domain, keywords in self.DOMAIN_KEYWORDS.items():
            hits = sum(1 for kw in keywords if kw.lower() in content_lower)
            if hits >= 2:  # require 2+ keyword hits to tag a domain
                matches[domain] = hits

        if not matches:
            return ClassificationResult(tags={}, confidence=0.5, needs_llm_review=True)

        domains = sorted(matches, key=matches.get, reverse=True)[:3]  # top 3 domains
        return ClassificationResult(
            tags={"domain": domains},
            confidence=min(0.95, 0.6 + 0.1 * max(matches.values())),
            classifier_name="keyword_domain",
        )
```

### 3. SourceSystemClassifier

Uses `ClassificationContext.source_system` and `file_path` — no content analysis needed.

```python
class SourceSystemClassifier:
    """Classify based on source system and file path."""

    SOURCE_DOMAIN_MAP = {
        "dbt": ["data-pipeline"],
        "unity_catalog": ["data-pipeline", "infrastructure"],
        "git": [],  # needs further classification
        "obsidian": ["documentation"],
        "openlineage": ["data-pipeline", "observability"],
    }

    def classify(self, content: str, *, context=None) -> ClassificationResult:
        if not context:
            return ClassificationResult(tags={}, confidence=0.3, needs_llm_review=True)

        tags = {}

        # Source system -> domain
        if context.source_system:
            domains = self.SOURCE_DOMAIN_MAP.get(context.source_system, [])
            if domains:
                tags["domain"] = domains

        # File path -> content_type and domain
        if context.file_path:
            if "/tests/" in context.file_path or context.file_path.startswith("test_"):
                tags["domain"] = tags.get("domain", []) + ["testing"]
                tags["content_type"] = ["code"]
            if "/docs/" in context.file_path or context.file_path.endswith(".md"):
                tags["content_type"] = ["documentation"]

        return ClassificationResult(
            tags=tags,
            confidence=0.9 if tags else 0.3,
            classifier_name="source_system",
            needs_llm_review=not bool(tags),
        )
```

### 4. LLM Classifier (Selective)

Wraps the existing `EnrichmentService` but only fires when deterministic classifiers flag `needs_llm_review` or confidence is below threshold.

The existing `EnrichmentService` prompt would be updated to output tags in the faceted format instead of free-form `auto_tags`. The `DEFAULT_CLASSIFICATIONS` list becomes the `content_type` facet values.

```python
class LLMFacetClassifier:
    """LLM classifier that fills in facets the deterministic layer couldn't resolve."""

    def __init__(self, enrichment_service: EnrichmentService) -> None:
        self._enrichment = enrichment_service

    async def classify(self, content: str, *, context=None) -> ClassificationResult:
        # Only classify the facets that are missing/low-confidence
        # The prompt is tuned to output structured facet tags, not free-form
        result = await self._enrichment.enrich(content, title=context.title if context else "")
        return ClassificationResult(
            tags={
                "domain": [normalize_tag(t) for t in result.auto_tags],
                "content_type": [result.auto_class] if result.auto_class else [],
                "scope": self._infer_scope(result),
            },
            confidence=min(result.tag_confidence, result.class_confidence),
            classifier_name="llm_facet",
        )
```

## Relationship to Existing Enrichment

The `EnrichmentService` doesn't get replaced — it becomes the LLM backend for the enrichment-mode `ClassifierPipeline`. The pipeline is the new unified interface; `EnrichmentService` is demoted from "the enrichment system" to "the LLM classifier implementation."

| Today | After |
|-------|-------|
| `EnrichmentService` is the whole enrichment system | `ClassifierPipeline` is the unified interface; `EnrichmentService` is one classifier inside it |
| `auto_tags` (free-form LLM tags) | `ContentTags.domain` + `ContentTags.content_type` (faceted, mostly deterministic) |
| `auto_class` (LLM picks from list) | `ContentTags.content_type` (deterministic first, LLM fallback) |
| `auto_importance` (LLM float) | `compute_importance()` (composite of tags + LLM score) |
| `auto_summary` (LLM text) | Unchanged — summaries still need LLM, generated in enrichment mode |
| Runs only async, post-ingestion | Ingestion mode: deterministic-only, inline. Enrichment mode: deterministic + LLM, async |
| Every item gets an LLM call | ~70% classified by deterministic layer; LLM fires only for ambiguous items |

The `EnrichmentService` prompt evolves to output faceted tags (`domain`, `content_type`, `scope`) instead of free-form `auto_tags`. The `DEFAULT_CLASSIFICATIONS` list becomes the `content_type` controlled vocabulary. The `LLMFacetClassifier` wraps `EnrichmentService` and conforms to the `Classifier` protocol.

### Migration Path

1. `ClassifierPipeline` ships with deterministic classifiers only (ingestion mode)
2. `EnrichmentService` prompt is updated to output faceted tags
3. `LLMFacetClassifier` wraps `EnrichmentService`, conforming to `Classifier` protocol
4. Enrichment worker is updated to use `ClassifierPipeline(llm_classifier=llm_facet)` instead of calling `EnrichmentService` directly
5. Old `auto_tags`/`auto_class` fields remain readable but new code writes `ContentTags`

## Feedback Loop: Classification Improves Over Time

The effectiveness analysis (`effectiveness.py`) already identifies noise candidates. This feeds back into classification:

1. Items flagged as noise get `signal_quality: "noise"` tag
2. Default retrieval excludes noise
3. Keyword dictionaries can be tuned based on which domains correlate with success/failure
4. The `llm_threshold` on `ClassifierPipeline` can be adjusted — if LLM classifications don't correlate with better outcomes, raise the threshold to use LLM less
5. Graph propagation means fixing one node's tags can cascade improvements to its neighbors

## Validation: Why This Tagging Approach

### Evidence From Production Systems

The flat faceted approach is validated by multiple production systems at scale:

**Datadog Unified Service Tagging** uses flat `key:value` pairs with three mandatory facets (`env`, `service`, `version`) plus recommended facets like `team` and `region`. This is the closest production analog to our `domain`/`content_type`/`scope`/`signal_quality` structure. Their documented failure modes — vocabulary drift, high cardinality explosion, inconsistent naming — are addressed in our design by controlled vocabularies enforced at write time through the governed mutation pipeline.

**Obsidian + Dataview** implements exactly the flat faceted pattern for knowledge management: YAML frontmatter with `type`, `domain`, `status`, `scope` fields, queried via `TABLE FROM "notes" WHERE type = "meeting-notes" AND domain = "data-platform"`. This is the personal knowledge management version of what we're building for agents.

**Honeycomb** provides the counterpoint: arbitrarily wide structured events with 100+ dimensions, no controlled vocabulary. This works when you have a columnar query engine optimized for high cardinality. We don't — our SQLite/Postgres stores need bounded facets for efficient `WHERE` clauses.

### Why Four Facets

Research on flat faceted classification consistently finds that **2-4 orthogonal facets with 5-15 values each outperform deep hierarchical taxonomies**. The reasons:

- Hierarchies force single-path classification ("is this infrastructure or data-pipeline?"). Flat multi-label tagging allows both.
- Each level of hierarchy multiplies classification error. Two-level hierarchies have 2x the misclassification rate of flat facets.
- Flat facets compose naturally as pre-filters: `domain:X AND content_type:Y` is a SQL `WHERE` clause. Hierarchies require tree traversal.

Our four facets are designed to be orthogonal:

| Facet | Answers | Orthogonal because |
|-------|---------|-------------------|
| `domain` | What area? | A `procedure` can be about any domain |
| `content_type` | What shape? | An `error-resolution` can be in any domain, at any scope |
| `scope` | How broadly applicable? | A `universal` pattern can be about infrastructure or ML |
| `signal_quality` | Worth retrieving? | Noise exists in every domain, every content type |

### Known Limitations and Mitigations

**Vocabulary drift** is the #1 failure mode of flat taxonomies. Different callers use different terms for the same concept. Mitigated by:
- Controlled vocabularies enforced by `ContentTags` schema validation (`extra="forbid"`)
- `normalize_tag()` already exists and handles casing/formatting
- `custom` dict provides an escape hatch without polluting core facets

**Cross-facet relationships** (e.g., "Spark" implies both `data-pipeline` and a technology) are not expressible in flat facets. Mitigated by:
- Multi-label `domain` facet (Spark content gets `["data-pipeline"]` not `["data-pipeline", "spark"]`)
- Graph propagation handles implied relationships through edges rather than tag hierarchy
- Keyword dictionaries encode the domain-keyword mapping explicitly

**Facet value growth** — if a facet exceeds ~30 values, users can't hold the vocabulary in their heads. Mitigated by:
- Starting with 8-10 values per facet
- `custom` dict absorbs long-tail needs
- `classification_version` field enables re-classification when vocabulary evolves

**LLM non-determinism** — even with `temperature=0.3`, LLM outputs vary slightly across calls. Mitigated by:
- LLM only fills gaps; deterministic classifiers set the baseline
- `classified_by` audit trail records which classifier set each tag
- `classification_version` enables re-running if prompts change
- Enrichment mode merges LLM results without overwriting high-confidence deterministic tags

### What We Considered and Rejected

**Deep hierarchical taxonomy** (e.g., `infrastructure/kubernetes/networking/ingress`): Rejected because it forces single-path classification and compounds error at each level. Every production knowledge graph system we evaluated uses flat or 2-level-max taxonomies.

**Free-form tags only** (current `auto_tags` approach): Rejected because free-form tags fragment without governance. "data-pipeline" vs "data_pipeline" vs "etl" vs "data-engineering" all mean the same thing. Controlled vocabularies solve this.

**LLM-only classification** (current `EnrichmentService`): Rejected as the sole approach because it's slow, expensive, non-deterministic, and unnecessary for ~70% of items where structural/keyword signals are sufficient. Kept as a fallback for the remaining ~30%.

**Embedding-based classification** (classify by nearest-neighbor in vector space): Rejected for the deterministic layer because it requires a model, adds latency, and isn't auditable. May be worth exploring later as a fifth classifier for borderline cases between deterministic and LLM.

## Implementation Order

1. **`ContentTags` schema + `ClassifierProtocol` + `ClassifierPipeline`** — Define the data model, protocol, and unified pipeline. No behavior change yet.
2. **`StructuralClassifier` + `KeywordDomainClassifier` + `SourceSystemClassifier`** — Ship deterministic classifiers. Wire into mutation handlers for ingestion-mode classification.
3. **`GraphNeighborClassifier`** — Add graph-propagated tagging. Requires items to be in the graph first, so runs after initial store write or on second pass.
4. **Tag-aware retrieval** — Add `tag_filters` to `PackBuilder.build()` and flow through to strategies. Default exclusion of `noise`.
5. **`LLMFacetClassifier`** — Adapt `EnrichmentService` prompt to output faceted tags. Wire into `ClassifierPipeline` as enrichment-mode fallback.
6. **Enrichment worker migration** — Update enrichment worker to use `ClassifierPipeline(llm_classifier=llm_facet)` instead of calling `EnrichmentService` directly.
7. **Composite importance** — Replace flat `auto_importance` with `compute_importance()`.
8. **Feedback-driven tuning** — Connect effectiveness analysis to `signal_quality` updates and classifier threshold adjustment.
