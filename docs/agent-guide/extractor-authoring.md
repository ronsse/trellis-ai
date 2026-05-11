# Authoring an Extractor

> **Who this is for:** anyone building a new ingestion path from a domain-specific source into Trellis. Confluence, Jira, Unity Catalog, query-log analyzers, internal APIs — anything that produces structural facts the substrate doesn't already know about.

> **What this covers:** the `Extractor` protocol contract, tier selection, idempotency, plugin registration, telemetry obligations, and two annotated reference implementations.

> **What this is not:** a guide to *what* to model (read [modeling-guide.md](modeling-guide.md) first) or *what shape* a specific source should take (read [source-modeling-cookbook.md](source-modeling-cookbook.md)).

---

## The contract at a glance

```python
@runtime_checkable
class Extractor(Protocol):
    name: str                     # unique identifier, lowercase_with_underscores
    tier: ExtractorTier           # DETERMINISTIC | HYBRID | LLM
    supported_sources: list[str]  # source-hint strings the dispatcher routes to this extractor
    version: str                  # SemVer; bumped when output shape changes

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult: ...
```

Source: [`src/trellis/extract/base.py`](../../src/trellis/extract/base.py).

Three constraints that are NOT in the type signature but are part of the contract:

1. **Purity.** Extractors return drafts; they do not call any store. The CLI / API / dispatcher routes drafts through `MutationExecutor`. This guarantee is what makes idempotency, policy enforcement, and audit logging possible.
2. **Recoverable parse errors do not raise.** If part of the input is malformed, surface it via `ExtractionResult.unparsed_residue` and `overall_confidence < 1.0`. Reserve exceptions for "I genuinely cannot proceed" — wrong input type, missing required schema fields, network failure.
3. **Stable entity IDs.** Re-running the extractor against the same input must produce the same `EntityDraft.entity_id` values. The `MutationExecutor` relies on this for idempotency; without it, each refresh creates duplicates instead of versions.

---

## `ExtractionResult` — what extractors return

```python
class ExtractionResult(TrellisModel):
    entities: list[EntityDraft] = []
    edges: list[EdgeDraft] = []
    extractor_used: str
    tier: str
    llm_calls: int = 0
    tokens_used: int = 0
    overall_confidence: float = 1.0    # 0..1
    provenance: ExtractionProvenance
    unparsed_residue: list[dict] = []  # things you saw but didn't model
```

Each `EntityDraft` is the data minus the store identity — `entity_id`, `entity_type`, `name`, `properties`, optional `node_role`, optional `generation_spec`, `confidence`. Each `EdgeDraft` is `source_id`, `target_id`, `edge_kind`, optional `properties`, optional `allow_dangling`, `confidence`.

**Why `allow_dangling`.** Some inputs reference entities that arrive in a separate batch (cross-manifest dbt depends_on, OpenLineage events emitted out of order). Setting `allow_dangling=True` tells the FK pre-flight in `LinkCreateHandler` to skip the existence check. Use it for genuine cross-batch references, not as a workaround for missing data.

**Why `unparsed_residue`.** When you parse 95% of a payload but skip a section you don't model yet, dump the skipped section into `unparsed_residue`. The dispatcher emits `EXTRACTOR_FALLBACK` events for empty results so cold-start gaps surface immediately; `unparsed_residue` is the per-call equivalent for *partial* gaps. Effectiveness analysis (Phase-2 graduation tracking) uses both signals to find where deterministic coverage should expand.

---

## Tier selection

The tier you declare drives dispatcher routing priority and effectiveness analysis. Pick the lowest tier that genuinely fits — agents prefer cheaper, more reliable extractors when both can handle an input.

| Tier | Use when | Avoid when | Cost profile |
|---|---|---|---|
| `DETERMINISTIC` | Source has a stable, parsable schema. dbt manifests, OpenLineage events, OpenAPI specs, JSON exports, Unity Catalog API. | The format is heterogeneous or freeform. | Microseconds-to-milliseconds. Zero LLM tokens. |
| `HYBRID` | Most of the input is structured but parts need interpretation. Markdown with embedded structured frontmatter; Jira tickets where titles parse cleanly but descriptions need summarization. | The structured layer is so thin you're essentially doing LLM work. | Mixed. Set `llm_calls` honestly per-call; the dispatcher reports a "rules-handled-N% vs LLM-handled-M%" breakdown. |
| `LLM` | Input is genuinely unstructured prose, and the value of extraction outweighs the cost. Slack threads, meeting notes, free-text incident reports. | A deterministic extractor with `unparsed_residue` would meet the bar. | Seconds per call. Variable tokens. Gated by `context.allow_llm_fallback` (default `False`). |

**The graduation path.** Domains start LLM (because nothing else works), move to Hybrid (once you've seen enough examples to write rules for the common cases), and eventually become Deterministic (once the rule coverage hits ~95% and the LLM is doing nothing). The `EXTRACTOR_FALLBACK` and `EXTRACTION_REJECTED` events feed an analysis loop that surfaces which inputs would benefit from a lower-tier extractor — but you don't have to build the loop; just declare your tier honestly and the telemetry takes care of the rest.

---

## Purity rule — extractors must not write to stores

```python
# ❌ DON'T DO THIS
class BadExtractor:
    async def extract(self, raw_input, ...):
        graph_store = StoreRegistry.get_graph_store()
        graph_store.upsert_node(...)  # ← extractor writing directly
        return ExtractionResult(...)
```

The pure form returns drafts and lets `MutationExecutor` do the writing:

```python
# ✅ DO THIS
class GoodExtractor:
    async def extract(self, raw_input, ...):
        return ExtractionResult(
            entities=[EntityDraft(entity_id="...", entity_type="...", ...)],
            edges=[EdgeDraft(source_id="...", target_id="...", edge_kind="...")],
            ...
        )
```

Why this matters:

- **Idempotency.** `MutationExecutor` checks idempotency keys before executing. An extractor that writes directly bypasses the check, so a re-run produces duplicates.
- **Policy.** Mutation handlers run policy gates (redaction, retention, classification). Extractors that bypass them effectively disable those gates for their writes.
- **Audit.** Every write produces an `EventLog` entry. Direct extractor writes don't.
- **Batching.** The dispatcher can combine drafts from multiple extractors into a single `CommandBatch` with a uniform strategy (`SEQUENTIAL`, `STOP_ON_ERROR`, `CONTINUE_ON_ERROR`). Direct-writing extractors break the batch boundary.

The CLI helper `_run_extraction()` in [`src/trellis_cli/ingest.py`](../../src/trellis_cli/ingest.py) is the canonical pattern: it instantiates the extractor, calls `dispatcher.dispatch()`, converts the result to a `CommandBatch` via `result_to_batch()`, then executes through `MutationExecutor.execute_batch()`. Your extractor doesn't see any of that machinery; it just returns drafts.

---

## Idempotency keys — making re-runs safe

Every `Command` carries an `idempotency_key`. The `MutationExecutor` stores executed keys and skips re-execution when a duplicate arrives. The default `result_to_batch()` derives keys from the draft contents (entity_id + properties hash for entity creates; source+target+kind for link creates), but you can override.

**The rule for entity IDs:** generate them from stable, source-system-supplied identifiers, not from ingest-time tokens.

```python
# ✅ Stable: dbt's unique_id survives re-runs and is canonical across runs
entity_id = resource["unique_id"]  # "model.my_project.fct_orders"

# ❌ Unstable: a UUID minted at ingest time changes every run
entity_id = f"dbt:{uuid.uuid4()}"

# ❌ Unstable: a hash of the runtime timestamp
entity_id = hashlib.sha256(f"{name}:{datetime.now()}".encode()).hexdigest()
```

When the source supplies no stable identifier, derive one deterministically from the source's natural key:

```python
# Markdown doc with no front-matter id → derive from path
entity_id = f"doc:{path.relative_to(repo_root).as_posix()}"

# Git commit → use the commit hash
entity_id = f"commit:{commit.hexsha}"

# Confluence page → use the space + page_id
entity_id = f"confluence:{space}:{page_id}"
```

If two distinct entities can collide in your derivation (e.g., temp tables across runs with the same name), disambiguate via `(source_system, raw_id)` aliases — see the *Synthetic-identity collision* anti-pattern in [modeling-guide.md](modeling-guide.md).

---

## Plugin registration via entry points

Core ships `JSONRulesExtractor`; `trellis_workers.extract` ships `DbtManifestExtractor` and `OpenLineageExtractor`. Third-party extractors register themselves via Python entry points in `pyproject.toml`:

```toml
[project.entry-points."trellis.extractors"]
confluence = "my_org_trellis_extractors.confluence:ConfluenceExtractor"
jira = "my_org_trellis_extractors.jira:JiraExtractor"
```

The loader at [`src/trellis/plugins/loader.py`](../../src/trellis/plugins/loader.py) discovers all entries in the `trellis.extractors` group at runtime. No code change to Trellis core is needed to add an extractor — `pip install my-org-trellis-extractors` is enough.

To wire your extractor into the `ExtractorRegistry` programmatically (e.g., for tests):

```python
from trellis.extract.registry import ExtractorRegistry

registry = ExtractorRegistry()
registry.register(MyExtractor())
# or
registry.load_entry_points("trellis.extractors")
```

The `supported_sources` static attr is the routing key: when a caller passes `source_hint="confluence"` to the dispatcher, the dispatcher picks the highest-priority registered extractor whose `supported_sources` includes that string.

---

## Telemetry contract

Three events surface in the `EventLog` around extraction. Honest population of `overall_confidence`, `llm_calls`, and `tokens_used` is what makes them useful.

### `EXTRACTOR_FALLBACK`

Emitted by the dispatcher when:

- `context.prefer_tier` overrides natural tier-priority routing (reason: `"prefer_tier_override"`)
- The selected extractor returns an empty result (reason: `"empty_result"`) — preserved even when downstream validators reject the empty payload (per [the swarm 3 fix](../../src/trellis/extract/dispatcher.py))

The fallback signal is the "graduation lens" — it tells effectiveness analysis where a higher-tier extractor handled an input that a lower-tier could not. Empty deterministic results consistently followed by successful LLM-tier results are exactly the inputs that should drive new deterministic rules.

### `EXTRACTION_REJECTED`

Emitted by validators when a draft fails shape checks: missing required fields, bad references, malformed payloads. Distinct from `EXTRACTOR_FALLBACK` (graduation lens vs validation lens). Both can fire on the same input.

### `EXTRACTOR_USED`

Emitted on every successful extraction with `extractor_used`, `tier`, `entities_emitted`, `edges_emitted`, `llm_calls`, `tokens_used`, `overall_confidence`. The basis for tier-level cost dashboards.

### How to populate the telemetry fields

```python
result = ExtractionResult(
    entities=entities,
    edges=edges,
    extractor_used=self.name,
    tier=self.tier.value,
    # Real numbers, not zeros:
    llm_calls=actual_llm_calls,
    tokens_used=actual_tokens,
    overall_confidence=min(d.confidence for d in entities) if entities else 1.0,
    provenance=ExtractionProvenance(
        extractor_name=self.name,
        extractor_version=self.version,
        source_hint=source_hint,
    ),
)
```

Deterministic extractors report `llm_calls=0`, `tokens_used=0`, `overall_confidence=1.0` (the defaults). Hybrid and LLM extractors must populate honestly — the graduation-tracking heuristics misfire when cost numbers are zero.

---

## Worked example 1: `DbtManifestExtractor` (143 LOC, deterministic)

Source: [`src/trellis_workers/extract/dbt_manifest.py`](../../src/trellis_workers/extract/dbt_manifest.py).

**Shape**: parses a parsed dbt `manifest.json` dict and emits one `EntityDraft` per model / seed / snapshot / source / test, plus one `EdgeDraft` (`depends_on`) per entry in each resource's `depends_on.nodes` list.

**Key choices walked through:**

1. **`tier = ExtractorTier.DETERMINISTIC`.** Manifest JSON has a stable, well-documented schema. No interpretation needed.
2. **`supported_sources = ["dbt-manifest"]`.** A single, narrow routing string — the dispatcher uses this verbatim.
3. **`entity_id = resource["unique_id"]`.** dbt's `unique_id` (`model.my_project.fct_orders`) is the canonical stable identifier. Survives re-runs unchanged.
4. **`entity_type` derived per resource type** (`model` → `"dbt_model"`, `source` → `"dbt_source"`, etc.) via a static map. Each is a domain-specific open-string type, not in the well-known set — that's fine, the storage layer accepts any string.
5. **Routing properties populated from `metadata.adapter_type`.** The cross-database routing convention from [modeling-guide.md](modeling-guide.md#cross-database-routing-properties-for-queryable-datasets) is upheld: `source_system`, `database_name`, `schema_name`, and `physical_uri` all land on dataset-shaped entities.
6. **`allow_dangling=True` on `depends_on` edges.** Cross-manifest references (downstream project depending on an upstream package) reference IDs not in the same batch. Setting the flag prevents the FK pre-flight from rejecting them.
7. **Input validation up front.** `if not isinstance(raw_input, dict): raise TypeError(...)`. The extractor owns input validation — the dispatcher passes through whatever the caller gave.
8. **`context` is ignored** because deterministic extractors have no cost budget to honour. Marked with `del context` to make the no-op explicit.

What's not in this extractor and would be added at higher tiers:

- **No `llm_calls` / `tokens_used`** — both default to 0.
- **No `unparsed_residue`** — the parser handles 100% of the supported manifest shape; nothing is unmodeled.
- **No `confidence < 1.0`** — every emitted draft is exact.

---

## Worked example 2: `OpenLineageExtractor` (166 LOC, deterministic)

Source: [`src/trellis_workers/extract/openlineage.py`](../../src/trellis_workers/extract/openlineage.py).

**Shape**: parses a list of OpenLineage event dicts and emits `job` + `dataset` entities plus `reads_from` / `writes_to` edges, deduplicated by `(source_id, target_id, edge_kind)`.

**Differences from dbt:**

1. **List input, not dict input.** OL events arrive as a list (JSON array or NDJSON). Validation handles the list-vs-dict distinction.
2. **De-duplication inside the extractor.** A single batch can contain multiple events for the same job→dataset edge. The extractor maintains a `seen_edge_keys` set and emits each unique edge once.
3. **Identity derived from namespace + name.** No `unique_id` like dbt; the extractor synthesizes IDs as `f"dataset:{namespace}:{name}"` and `f"job:{namespace}:{name}"`. The namespace + name pair is the OpenLineage spec's natural key — stable across runs.
4. **Routing properties parsed from the namespace URI.** When the namespace looks like `snowflake://account.region`, the extractor lifts `"snowflake"` into `source_system` and constructs `physical_uri = "snowflake://account.region/db.public.orders"`. When the namespace is bare (`warehouse`), it becomes the system identifier itself.
5. **`allow_dangling=True` on every edge.** OpenLineage events stream out of order; a job referencing a dataset emitted in a prior batch is normal. Same flag, same reason as dbt's cross-manifest case.

---

## Building a new extractor: the minimum viable shape

For a deterministic extractor against a structured source, you need ~80-150 LOC. The skeleton:

```python
"""My source extractor."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from trellis.extract.base import ExtractorTier
from trellis.schemas import well_known as wk
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)

if TYPE_CHECKING:
    from trellis.extract.context import ExtractionContext

logger = structlog.get_logger(__name__)


class MySourceExtractor:
    name = "my_source"
    tier = ExtractorTier.DETERMINISTIC
    supported_sources: ClassVar[list[str]] = ["my-source"]
    version = "0.1.0"

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        del context  # deterministic — no cost budget

        # 1. Validate input type / required fields. Raise TypeError for
        #    unrecoverable input; everything else surfaces via residue.
        if not isinstance(raw_input, dict):
            msg = f"MySourceExtractor expects a parsed payload dict; got {type(raw_input).__name__}"
            raise TypeError(msg)

        entities: list[EntityDraft] = []
        edges: list[EdgeDraft] = []
        residue: list[dict] = []

        # 2. Walk the source. Emit one EntityDraft per real-world thing.
        #    Honour the four-question test (modeling-guide.md) — don't
        #    emit nodes for leaves that only have one inbound edge.
        for item in raw_input.get("items", []):
            stable_id = item.get("id")
            if not stable_id:
                residue.append({"reason": "missing_id", "item": item})
                continue

            entities.append(
                EntityDraft(
                    entity_id=f"my_source:{stable_id}",
                    entity_type="MyEntityType",
                    name=item.get("name", stable_id),
                    properties={
                        "raw_id": stable_id,
                        # ... domain-specific properties
                    },
                )
            )

            # 3. Edges for relationships. allow_dangling=True if the
            #    referenced entity may arrive in a separate batch.
            for related_id in item.get("relates_to", []):
                edges.append(
                    EdgeDraft(
                        source_id=f"my_source:{stable_id}",
                        target_id=f"my_source:{related_id}",
                        edge_kind="relates_to",
                    )
                )

        logger.info(
            "my_source_extracted",
            entities=len(entities),
            edges=len(edges),
            residue=len(residue),
            source_hint=source_hint,
        )

        return ExtractionResult(
            entities=entities,
            edges=edges,
            extractor_used=self.name,
            tier=self.tier.value,
            unparsed_residue=residue,
            provenance=ExtractionProvenance(
                extractor_name=self.name,
                extractor_version=self.version,
                source_hint=source_hint,
            ),
        )
```

Steps to ship:

1. **Pick a source-modeling recipe** from [source-modeling-cookbook.md](source-modeling-cookbook.md). This tells you what entity types and edges to emit and which fields are routing-relevant.
2. **Implement the protocol** as above.
3. **Test with the contract pattern**: small fixture, assert on entity IDs / types / routing properties (see [`tests/unit/workers/test_ingestion.py`](../../tests/unit/workers/test_ingestion.py) for the dbt + OL test shape).
4. **Register via entry-point** in your package's `pyproject.toml`.
5. **Declare in `sources.yaml`** when ready for deployment (or invoke directly via `trellis extract refresh --type my-source --path /path`).

---

## Hybrid and LLM extractors

The deterministic path covers most data-platform sources. Hybrid and LLM extractors carry extra responsibilities:

**Hybrid extractors** must:

- Honour `context.budget` (cost cap per call).
- Populate `llm_calls` and `tokens_used` with real numbers, not zeros.
- Surface ambiguous residue when the rules can't handle a section — don't fall through to the LLM silently for the easy cases.

**LLM extractors** must:

- Respect `context.allow_llm_fallback` (default `False`). If it's `False` and the dispatcher routed to you anyway, it's a routing bug — fail loudly rather than running expensive calls the caller didn't authorize.
- Set `overall_confidence` to the LLM's reported confidence, or to a heuristic based on output validation.
- Cap their token budget per call. Unbounded extractors break agent SLAs.

See [`src/trellis/extract/json_rules.py`](../../src/trellis/extract/json_rules.py) (407 LOC) for the most complex deterministic extractor as a reference for ambitious rules-based work. Hybrid and LLM reference implementations are not in core yet — when you build the first one, document the pattern here.

---

## Further reading

- [modeling-guide.md](modeling-guide.md) — what to model and how
- [source-modeling-cookbook.md](source-modeling-cookbook.md) — per-source recipes (docs, Jira, Confluence, SQL logs, Unity Catalog, git)
- [freshness-and-curation.md](freshness-and-curation.md) — how `trellis extract refresh` re-runs an extractor and diffs the result
- [`src/trellis/extract/base.py`](../../src/trellis/extract/base.py) — the Protocol definition
- [`src/trellis/extract/dispatcher.py`](../../src/trellis/extract/dispatcher.py) — routing + tier priority
- [`src/trellis_workers/extract/dbt_manifest.py`](../../src/trellis_workers/extract/dbt_manifest.py) and [`openlineage.py`](../../src/trellis_workers/extract/openlineage.py) — reference implementations
