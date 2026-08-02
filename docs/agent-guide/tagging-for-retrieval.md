# Enriching Content for Tiered Retrieval

How to write and tag content so that it lands in the right sections during tiered context retrieval.

## Retrieval Affinity Tags

Every piece of content in the graph can carry a `retrieval_affinity` tag — a multi-label classification that tells the retrieval system which tier(s) the content is best suited for.

| Affinity | What It Means | Examples |
|----------|--------------|---------|
| `domain_knowledge` | Business concepts, ownership, governance, conventions | "The orders domain uses _v{N} versioning", "PII requires restricted catalog" |
| `technical_pattern` | How-to, code patterns, SQL idioms, templates | "ROW_NUMBER/QUALIFY for dedup", "CTE decomposition style" |
| `operational` | Execution traces, incidents, error history, debug info | "Last run failed at struct navigation", "Timeout on large table scan" |
| `reference` | Entity metadata, schemas, configurations, lookup data | "orders_v7 has 12 columns", "UC table owner: data-eng@example.com" |

Content can have **multiple affinities**. A "Foundation Deduplication Pattern" precedent is both `domain_knowledge` (it's a business convention) and `technical_pattern` (it's a code pattern).

## Tagging Knowledge Base Files

### Markdown Files (conventions, patterns, domains)

Add `retrieval_affinity` to the YAML frontmatter:

```yaml
---
title: "Data Ownership Model"
category: convention
domain: all
tags: [ownership, teams, governance]
retrieval_affinity: [domain_knowledge]
---
```

For content that serves multiple purposes:

```yaml
---
title: "Source System Lookback Windows"
category: pattern
domain: all
tags: [landing, ingestion, replay]
retrieval_affinity: [technical_pattern, domain_knowledge]
---
```

### Precedent YAML Files

Add `retrieval_affinity` to each precedent entry:

```yaml
- id: "precedent://foundation_deduplication_pattern"
  fact: >
    Foundation layer tables always deduplicate landing data using
    ROW_NUMBER with QUALIFY patterns.
  applies_to:
    - "uc://foundation"
  category: data_pattern
  retrieval_affinity: [technical_pattern, domain_knowledge]
```

### Ingestion Rules

Set a default `retrieval_affinity` in the metadata section of each ingestion rule. Per-item frontmatter tags override the default:

```yaml
- name: knowledge_docs
  source: knowledge_base
  metadata:
    retrieval_affinity: [domain_knowledge]  # default for this source
```

## What Happens Without Tags

If content has no `retrieval_affinity` tag, the `TierMapper` applies heuristic rules based on other properties:

| Property | Inferred Affinity |
|----------|------------------|
| `content_type=constraint` + `scope=org` | `domain_knowledge` |
| `content_type=code` or `content_type=pattern` | `technical_pattern` |
| `item_type=trace` or `content_type=error-resolution` | `operational` |
| `item_type=entity` | `reference` |

Explicit tags are always preferred over heuristics. If you want content to land in a specific tier reliably, tag it.

## Seeding Your Own Domains

`domain` is the primary retrieval slice. The `KeywordDomainClassifier` assigns a
`domain` when at least two of that domain's keywords appear in an item's
content. The built-in keyword map covers agent-centric defaults
(`data-pipeline`, `infrastructure`, `api`, `frontend`, `backend`, `ml-ops`,
`security`, `testing`, `observability`).

> **Where the auto-`domain` facet actually lands.** Classify-on-write
> (`TRELLIS_ENABLE_CLASSIFY_ON_INGEST=1`) **drops** the classifier-derived
> `domain` before persisting, and builds its pipeline from the built-in
> defaults *without* reading the `classify.domain_keywords` config below —
> `domain` is the one facet whose wrong value *hides* content rather than
> merely re-ranking it ([why](operations.md#document--content-tags-opt-in)).
> So today the config below shapes exactly one command, `trellis classify
> backfill`: a keyword hit always contributes `retrieval_affinity` and
> classifier confidence, and assigns `domain` only under
> `--include-domain`. The operator-set scalar
> `metadata['domain']` (the `--domain` / `--tag` flags) is a separate key and
> is never touched by any of this.

To register a domain specific to your deployment, add a
`classify.domain_keywords` section to `~/.trellis/config.yaml` — no code change.
`trellis admin init` writes a commented-out example you can uncomment:

```yaml
classify:
  domain_keywords:
    payments:
      - stripe
      - invoice
      - chargeback
      - refund
    compliance-workflow:
      - audit
      - attestation
      - soc2
      - evidence
```

Rules:

- **Domains stay free strings.** There is no enum, registry, or allow-list — any
  string is a valid domain name, consistent with the type-extensibility stance.
- **Config merges over the built-in defaults**, config winning on key collision.
  A colliding key (e.g. `api`) *replaces* the built-in keyword list for that
  domain rather than extending it. The `extra_domains` constructor argument of
  `KeywordDomainClassifier` merges last (after config) for programmatic callers.
- **Reserved policy namespaces are rejected loudly.** Domain names that collide
  with a reserved namespace (`sensitivity`, `regulatory`, `lifecycle`,
  `jurisdiction`, `authority`, `retention`, `redaction`) raise at config load
  time — those dimensions belong to `DataClassification` / `Lifecycle` or the
  policy system, not the `domain` facet. See
  [`adr-tag-vocabulary-split.md`](../design/adr-tag-vocabulary-split.md).

Any caller that goes through `StoreRegistry.build_ingestion_pipeline()` picks
the config up automatically; operators do not wire anything. Be aware that in
this tree that is currently `trellis classify backfill` and nothing else —
classify-on-write deliberately calls the config-free
`trellis.classify.ingest.build_ingest_classifier()` instead (see the note
above), and `trellis worker enrich` drives `EnrichmentService` off the `llm:`
block rather than a keyword pipeline.

To see which domains actually exist in a deployment, run
[`trellis analyze domains`](operations.md#analyze-domains) — a read-only usage
report across traces, documents, and pack feedback.

## Choosing the Right Affinity

Ask: **"When would an agent need this content?"**

- Before starting any work (understanding the domain, who owns what) → `domain_knowledge`
- While designing a solution (which patterns to use, what's been tried) → `technical_pattern`
- While writing code (exact column names, table schemas, SQL syntax) → `reference`
- When something goes wrong or reviewing past work → `operational`

If the answer is "multiple of the above," use multiple affinities.

## Validating Your Tags

After tagging content, run the pack analysis to verify content lands in expected sections:

```bash
# From the consumer deployment repo:
python -m <consumer_package>.trellis.pack_analysis

# Check: does the "ownership" gap still appear?
# If retrieval_affinity: [domain_knowledge] is set on ownership.md,
# and the scenario requests domain_knowledge sections, it should surface.
```

Once the CLI is available:
```bash
trellis analyze pack-sections --days 7
```
