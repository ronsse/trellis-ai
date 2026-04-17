# Enriching Content for Tiered Retrieval

How to write and tag content so that it lands in the right sections during tiered context retrieval.

## Retrieval Affinity Tags

Every piece of content in the graph can carry a `retrieval_affinity` tag — a multi-label classification that tells the retrieval system which tier(s) the content is best suited for.

| Affinity | What It Means | Examples |
|----------|--------------|---------|
| `domain_knowledge` | Business concepts, ownership, governance, conventions | "Sportsbook uses _v{N} versioning", "PII requires restricted catalog" |
| `technical_pattern` | How-to, code patterns, SQL idioms, templates | "ROW_NUMBER/QUALIFY for dedup", "CTE decomposition style" |
| `operational` | Execution traces, incidents, error history, debug info | "Last run failed at struct navigation", "Timeout on large table scan" |
| `reference` | Entity metadata, schemas, configurations, lookup data | "bets_v7 has 12 columns", "UC table owner: data-eng@fanduel.com" |

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
# From fd-data-architecture-poc:
python -m fd_poc.trellis.pack_analysis

# Check: does the "ownership" gap still appear?
# If retrieval_affinity: [domain_knowledge] is set on ownership.md,
# and the scenario requests domain_knowledge sections, it should surface.
```

Once the CLI is available:
```bash
trellis analyze pack-sections --days 7
```
