# trellis-ai GitHub corpus fixture

Phase B-2 of [`docs/design/plan-real-corpus-eval.md`](../../../docs/design/plan-real-corpus-eval.md).

## What's in `snapshot_raw.json`

A snapshot of all merged PRs in the `ronsse/trellis-ai` repository at
the time of capture. Fetched via `gh` CLI and committed as a fixture
so the scenario is reproducible without API access.

**Counts (captured 2026-05-07):**

- 88 merged PRs (sole entity type — this repo is PR-driven; 0 issues)
- 2 unique authors
- 245K chars of PR body content (~2.8K chars per PR average)
- 753 total changedFiles across all PRs

## Why this corpus

Picked over candidates like `pydantic/pydantic` or `tiangolo/fastapi`
because:

1. **Self-contained.** Trellis evaluating itself avoids any licensing
   question and the test setup needs only the snapshot we ship.
2. **Manageable scale.** 88 entities is comparable to Jaffle Shop (21)
   in entity count but much richer per-entity (long PR bodies vs.
   one-sentence dbt descriptions).
3. **Realistic content.** PR bodies in this project are thoughtful —
   typical body has summary, decision rationale, before/after numbers,
   and cross-references to related PRs. That's good content for
   keyword + semantic retrieval to chew on.
4. **Cross-PR references.** PR bodies frequently mention other PRs
   (`#42`, `(see #54)`, etc.). The extractor parses these into
   `wasInformedBy` edges, giving GraphSearch real traversable signal
   without needing a separate issues entity type.

## Schema mapping

The `GitHubExtractor` maps the raw GitHub structure onto canonical
PROV-O / schema.org names per
[`adr-graph-ontology.md`](../../../docs/design/adr-graph-ontology.md):

| GitHub object | `entity_type` | Canonical | Properties |
|---|---|---|---|
| Merged PR | `github_pr` | `CreativeWork` (via schema_alignment) | title, body, labels, branch info, additions/deletions, changedFiles count |
| Author | `github_user` | `Person` | login |

Edges:

| Source | Edge | Target | Notes |
|---|---|---|---|
| PR | `wasAttributedTo` | User | PR has an author |
| PR | `wasInformedBy` | PR | Cross-references parsed from body (`#NNN`) |

`wasAttributedTo` and `wasInformedBy` are PROV-O canonical edge kinds
per ADR §3.2 — the extractor emits them directly without a
loader-side canonicalization (unlike the dbt extractor which still
emits snake_case `depends_on`).

## What's intentionally NOT in the snapshot

To keep the corpus small + the API budget low, this fixture omits:

- **Per-PR commits.** Would require one API call per PR (88 calls).
  Commits add another entity layer (`Activity`) but for Phase B-2's
  first chart they'd over-complicate the convergence math.
- **Per-PR file lists.** Same reason — fetching changed files per PR
  is N×88 expensive. The total `changedFiles` count is captured as a
  property; individual file entities can be added in a follow-up.
- **Issues / linked-issue resolution.** This repo has 0 issues.
- **Reviewers.** Code review activity isn't surfaced.

If a future iteration wants any of these, regenerate the snapshot via
the script in this directory (TODO: ship a regen script) and extend
the extractor.

## Regenerating

```bash
gh pr list --state merged --limit 200 \
    --json number,title,body,author,labels,createdAt,mergedAt,headRefName,baseRefName,additions,deletions,changedFiles \
    > eval/corpora/github_trellis/snapshot_raw.json
```

Requires `gh` CLI authenticated with read access to the repository.
