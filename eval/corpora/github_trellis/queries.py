"""Ground-truth queries for the trellis-ai GitHub Phase B-2 scenario.

12 hand-authored queries covering: topic content discovery, multi-PR
series identification, cross-PR lineage tracing (via the
``wasInformedBy`` edges parsed from PR-body cross-references),
single-PR bug-fix identification, and author-attribution edge cases.

Mirror the structure of
:mod:`eval.corpora.jaffle_shop.queries` so the GitHub scenario can use
the same grading shape (``required_coverage`` is a list of entity_ids
that must appear in the pack at coverage threshold ≥ 0.6).

Difficulty tiers are informational only.
"""

from __future__ import annotations

from dataclasses import dataclass

# Domain tag for PackBuilder filtering. Single-domain corpus (one repo).
GITHUB_DOMAIN = "github_trellis"


@dataclass(frozen=True)
class GitHubPRQuery:
    """One ground-truth retrieval query against the trellis-ai PR corpus."""

    intent: str
    required_coverage: list[str]
    difficulty: str  # "easy" | "medium" | "hard"
    skill: str
    rationale: str


def _pr(n: int) -> str:
    """Shortcut to spell out PR entity_ids consistently."""
    return f"github.pr.{n}"


def _user(login: str) -> str:
    return f"github.user.{login}"


GROUND_TRUTH_QUERIES: list[GitHubPRQuery] = [
    # -----------------------------------------------------------------
    # Topic content discovery — semantic + keyword should solve these.
    # PR bodies contain rich content; queries pull a specific landmark.
    # -----------------------------------------------------------------
    GitHubPRQuery(
        intent=(
            "Which PR added a fail-fast registry validation and unified "
            "uvicorn logs at API startup?"
        ),
        required_coverage=[_pr(19)],
        difficulty="medium",
        skill="topic_content",
        rationale=(
            "PR #19 'API startup hardening' is the only one matching this "
            "specific combination. Distractors that mention 'registry' or "
            "'logging' or 'startup' but in unrelated contexts."
        ),
    ),
    GitHubPRQuery(
        intent=(
            "Which PR introduced the schema.org + PROV-O graph ontology "
            "alignment with Neo4j backend integration?"
        ),
        required_coverage=[_pr(16)],
        difficulty="medium",
        skill="topic_content",
        rationale=(
            "PR #16 'Graph ontology Phase 0-2, Neo4j backends, canonical "
            "DSL, e2e suite' covers this verbatim. Distractors include "
            "ADR PRs (#3, #4, #5) which are also ontology-shaped but "
            "earlier and narrower in scope."
        ),
    ),
    GitHubPRQuery(
        intent=(
            "Find the PR that first added bulk upsert methods "
            "(upsert_nodes_bulk, upsert_edges_bulk) to the GraphStore."
        ),
        required_coverage=[_pr(34)],
        difficulty="medium",
        skill="topic_content",
        rationale=(
            "PR #34 introduces the bulk methods. PR #44 'Simplify store "
            "bulk helpers' is a follow-up consolidator, not the introducer. "
            "Per-backend optimizations (#35, #41, #62, #63) are downstream."
        ),
    ),
    # -----------------------------------------------------------------
    # Multi-PR series — all the PRs in a coherent line of work.
    # KeywordSearch helps if titles share a phrase; cross-ref traversal
    # via `wasInformedBy` should pull adjacent series members.
    # -----------------------------------------------------------------
    GitHubPRQuery(
        intent=(
            "Show me the eval-framework Phase 1 through Phase 4 PRs that "
            "shipped scenarios 5.1, 5.2, and 5.3."
        ),
        required_coverage=[_pr(29), _pr(30), _pr(31), _pr(32)],
        difficulty="hard",
        skill="multi_pr_series",
        rationale=(
            "Four PRs explicitly named 'Eval Phase N: scenario 5.X'. "
            "Should be findable via keyword overlap on 'Phase' / 'eval' / "
            "'scenario' AND via the explicit cross-ref chain (each phase "
            "PR cites the previous)."
        ),
    ),
    GitHubPRQuery(
        intent=(
            "What were the Neo4j hardening Phase 1 and Phase 2 PRs that "
            "landed driver lifecycle, validate-on-startup, recommended "
            "config, and migrate-graph?"
        ),
        required_coverage=[_pr(49), _pr(50), _pr(51), _pr(52), _pr(53)],
        difficulty="hard",
        skill="multi_pr_series",
        rationale=(
            "5 PRs with explicit Phase numbering in titles (#49 1.5+2.2, "
            "#50 2.3, #51 1.1, #52 1.3, #53 2.1). Coverage threshold 0.6 → "
            "need 3 of 5. Tests whether the system can identify a coherent "
            "series across non-contiguous PR numbers."
        ),
    ),
    # -----------------------------------------------------------------
    # Cross-PR lineage — answers that REQUIRE traversing wasInformedBy
    # edges (the cross-references parsed from PR bodies).
    # -----------------------------------------------------------------
    GitHubPRQuery(
        intent=(
            "What PRs informed the v0.5.0 release backfill in PR #66? "
            "List the PRs cited in its body."
        ),
        required_coverage=[_pr(66), _pr(60), _pr(61), _pr(62), _pr(63), _pr(64)],
        difficulty="hard",
        skill="cross_pr_lineage",
        rationale=(
            "PR #66 'release: v0.5.0 — backfill #60-#64' explicitly cites "
            "those 5 PRs in its body. With seed extraction matching '#66' "
            "→ pr.66, GraphSearch traverses outgoing wasInformedBy edges "
            "to surface the cited 5. Coverage: 5 of 6 required (#66 itself "
            "+ 5 cited)."
        ),
    ),
    GitHubPRQuery(
        intent=(
            "Find PRs that reference or build on PR #34 (the bulk upsert "
            "introduction)."
        ),
        required_coverage=[_pr(34), _pr(36), _pr(46), _pr(47)],
        difficulty="hard",
        skill="reverse_lineage",
        rationale=(
            "Reverse traversal — find incoming wasInformedBy edges from "
            "other PRs into #34. Several follow-up PRs cite #34: #36 "
            "'switch ingest to upsert_nodes_bulk', #46 'StoreRegistry "
            "context manager' (cites in 'depends on bulk wiring' note), "
            "and #47 'vector index ONLINE wait' (companion landing). "
            "Bidirectional traversal in get_subgraph should land them."
        ),
    ),
    # -----------------------------------------------------------------
    # Single-PR identification — narrow content queries that should
    # return one specific PR.
    # -----------------------------------------------------------------
    GitHubPRQuery(
        intent=(
            "Which PR fixed the StoreRegistry silent SQLite fallback bug "
            "when given a plane-split config?"
        ),
        required_coverage=[_pr(33)],
        difficulty="medium",
        skill="bug_fix_identification",
        rationale=(
            "PR #33 verbatim. Distractors: #46 (StoreRegistry context "
            "manager — different concern) and #19 (registry validation — "
            "adjacent topic but different bug)."
        ),
    ),
    GitHubPRQuery(
        intent=(
            "Find the PR that stamped classifier mode (ingestion vs "
            "enrichment) on every classification, closing Logic Gap 1.2."
        ),
        required_coverage=[_pr(40)],
        difficulty="medium",
        skill="bug_fix_identification",
        rationale=(
            "PR #40 specifically. Body mentions 'Gap 1.2' explicitly. "
            "Distractor: PR #14 'Core Loop Audit — Logic Gaps 1.1, 2.1, "
            "2.3, 2.4, 3.2, 4.1' covers OTHER logic gaps but not 1.2."
        ),
    ),
    GitHubPRQuery(
        intent=(
            "Which PR added the wait-for-vector-index-ONLINE step after "
            "CREATE on the Neo4j vector store?"
        ),
        required_coverage=[_pr(47)],
        difficulty="medium",
        skill="bug_fix_identification",
        rationale=(
            "PR #47 verbatim. Specific Neo4j feature, narrow query."
        ),
    ),
    # -----------------------------------------------------------------
    # Author attribution — testing the wasAttributedTo edges.
    # -----------------------------------------------------------------
    GitHubPRQuery(
        intent="Which PRs were authored by app/dependabot, not by ronsse?",
        required_coverage=[],  # filled below — depends on the snapshot
        difficulty="hard",
        skill="author_attribution",
        rationale=(
            "Inverted attribution query — filter by author. Required "
            "coverage left empty here because the dependabot PR set is "
            "snapshot-dependent; the scenario fills it from the corpus "
            "at startup. Tests the rare-author identification path."
        ),
    ),
    # -----------------------------------------------------------------
    # ADR landings — meta query about the project's design history.
    # -----------------------------------------------------------------
    GitHubPRQuery(
        intent=(
            "Which PRs landed Architectural Decision Records (ADRs) for "
            "terminology, tag vocabulary, and storage planes?"
        ),
        required_coverage=[_pr(3), _pr(4), _pr(5)],
        difficulty="medium",
        skill="adr_landing",
        rationale=(
            "Three back-to-back PRs explicitly land ADRs: #3 Terminology "
            "ADR, #4 Tag Vocabulary Split Phase 0 (ADR + reserved "
            "namespaces), #5 Storage Planes & Substrates Phase A "
            "(ADR + config layout). Distractors include #16 which also "
            "ships an ADR (graph ontology) but isn't in the requested "
            "set — full coverage means recognizing 'terminology, tag "
            "vocabulary, storage planes' specifically."
        ),
    ),
]


def materialize_dependabot_query_coverage(
    snapshot_authors: dict[int, str],
) -> None:
    """Fill in the dependabot author-attribution query's required_coverage.

    Mutates the ``GROUND_TRUTH_QUERIES`` entry in place. Called by the
    GitHub scenario at setup time after loading the snapshot, since the
    PR set authored by ``app/dependabot`` is a property of the loaded
    corpus rather than a hand-authored constant.

    The snapshot maps PR number → author login.
    """
    dependabot_prs = sorted(
        num for num, login in snapshot_authors.items() if login == "app/dependabot"
    )
    coverage = [_pr(n) for n in dependabot_prs]
    for i, q in enumerate(GROUND_TRUTH_QUERIES):
        if q.skill == "author_attribution" and "dependabot" in q.intent:
            GROUND_TRUTH_QUERIES[i] = GitHubPRQuery(
                intent=q.intent,
                required_coverage=coverage,
                difficulty=q.difficulty,
                skill=q.skill,
                rationale=q.rationale,
            )
            break


__all__ = [
    "GROUND_TRUTH_QUERIES",
    "GITHUB_DOMAIN",
    "GitHubPRQuery",
    "materialize_dependabot_query_coverage",
]
