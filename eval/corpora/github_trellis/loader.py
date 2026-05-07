"""trellis-ai GitHub PR corpus loader for Phase B-2.

Reads the committed PR snapshot ``snapshot_raw.json``, builds
:class:`EntityDraft` and :class:`EdgeDraft` records, submits them
through the governed mutation pipeline (per CLAUDE.md hard rule), and
indexes PR bodies into the document store for KeywordSearch /
SemanticSearch.

Mapping per
[`adr-graph-ontology.md`](../../../docs/design/adr-graph-ontology.md):

| Source               | entity_type    | canonical              | schema_alignment       |
|----------------------|----------------|------------------------|------------------------|
| Merged PR            | ``github_pr``  | ``CreativeWork``       | ``schema.org/CreativeWork`` |
| Author               | ``github_user``| ``Person`` / ``Agent`` | ``schema.org/Person``  |

Edges (canonical PROV-O verbs emitted directly — no loader-side
canonicalization needed, unlike the dbt extractor):

| From | Edge              | To   | Source                                     |
|------|-------------------|------|--------------------------------------------|
| PR   | ``wasAttributedTo`` | User | ``author.login`` from snapshot           |
| PR   | ``wasInformedBy``   | PR   | Cross-references (``#NNN``) parsed from body |

Cross-reference parsing: PR bodies in this project frequently mention
other PRs by ``#NNN``. The loader scans bodies with a regex, validates
the referenced number against the set of known PR numbers in the
snapshot (so we don't create dangling edges to issue numbers that
don't exist in the corpus), and creates ``wasInformedBy`` edges. The
relation is "this PR was informed by that PR" — bidirectional
neutral, but PROV-O's ``wasInformedBy`` is directional from the
informed activity to the informer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from trellis.extract.commands import result_to_batch
from trellis.mutate.commands import CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import create_curate_handlers
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)
from trellis.schemas.well_known import (
    schema_alignment_for_edge_kind,
    schema_alignment_for_entity_type,
)
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


DEFAULT_SNAPSHOT_PATH = Path(__file__).parent / "snapshot_raw.json"

# entity_type strings — domain-specific names per CLAUDE.md "Entity types
# are any string at the storage and API layers." schema_alignment metadata
# carries the canonical mapping for downstream consumers.
ENTITY_TYPE_PR = "github_pr"
ENTITY_TYPE_USER = "github_user"

# Edge kinds — emitted directly as canonical PROV-O verbs (no loader
# canonicalization needed; the strings here are already the ADR §3.2 form).
EDGE_KIND_ATTRIBUTED = "wasAttributedTo"
EDGE_KIND_INFORMED_BY = "wasInformedBy"

# Cross-reference regex — matches `#NNN` where NNN is 1-5 digits and not
# preceded by a word character (so we don't match things like `abc#123`
# inside URLs). Trailing word-boundary so `#1234abc` doesn't match.
_PR_REF_PATTERN = re.compile(r"(?<!\w)#(\d{1,5})\b")


@dataclass
class GitHubLoadResult:
    """Counts surfaced after loading the GitHub PR corpus into a registry."""

    prs_extracted: int
    users_extracted: int
    edges_extracted: int
    nodes_created: int
    edges_created: int
    documents_indexed: int
    cross_reference_edges: int
    attribution_edges: int

    def as_metrics(self, prefix: str = "corpus") -> dict[str, float]:
        return {
            f"{prefix}.prs_extracted": float(self.prs_extracted),
            f"{prefix}.users_extracted": float(self.users_extracted),
            f"{prefix}.edges_extracted": float(self.edges_extracted),
            f"{prefix}.nodes_created": float(self.nodes_created),
            f"{prefix}.edges_created": float(self.edges_created),
            f"{prefix}.documents_indexed": float(self.documents_indexed),
            f"{prefix}.cross_reference_edges": float(self.cross_reference_edges),
            f"{prefix}.attribution_edges": float(self.attribution_edges),
        }


def _pr_entity_id(pr_number: int) -> str:
    """Stable entity id for a PR. Mirrors dbt's ``unique_id`` style."""
    return f"github.pr.{pr_number}"


def _user_entity_id(login: str) -> str:
    return f"github.user.{login}"


def _build_pr_drafts(
    snapshot: list[dict[str, Any]],
) -> tuple[list[EntityDraft], list[EntityDraft], list[EdgeDraft], int, int]:
    """Convert the snapshot into entity / edge drafts.

    Returns ``(pr_entities, user_entities, edges, attribution_count,
    cross_reference_count)``.
    """
    pr_entities: list[EntityDraft] = []
    user_logins: dict[str, EntityDraft] = {}  # dedup users
    edges: list[EdgeDraft] = []
    attribution_count = 0
    cross_ref_count = 0

    known_pr_numbers: set[int] = {pr["number"] for pr in snapshot}
    pr_alignment = schema_alignment_for_entity_type("CreativeWork")
    user_alignment = schema_alignment_for_entity_type("Person")
    attributed_alignment = schema_alignment_for_edge_kind(EDGE_KIND_ATTRIBUTED)
    informed_alignment = schema_alignment_for_edge_kind(EDGE_KIND_INFORMED_BY)

    for pr in snapshot:
        pr_number = pr["number"]
        pr_id = _pr_entity_id(pr_number)
        title = pr.get("title", "") or ""
        body = pr.get("body", "") or ""
        labels = [lbl.get("name", "") for lbl in (pr.get("labels") or [])]

        properties: dict[str, Any] = {
            "name": title,
            "pr_number": pr_number,
            "title": title,
            "description": body,  # standard property name across extractors
            "body": body,
            "labels": labels,
            "head_ref": pr.get("headRefName", ""),
            "base_ref": pr.get("baseRefName", ""),
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "changed_files": pr.get("changedFiles", 0),
            "created_at": pr.get("createdAt", ""),
            "merged_at": pr.get("mergedAt", ""),
        }
        if pr_alignment is not None:
            properties["schema_alignment"] = pr_alignment

        pr_entities.append(
            EntityDraft(
                entity_id=pr_id,
                entity_type=ENTITY_TYPE_PR,
                name=f"PR #{pr_number}: {title}",
                properties=properties,
            )
        )

        # Author → User entity + wasAttributedTo edge
        author = pr.get("author") or {}
        login = author.get("login", "")
        if login:
            if login not in user_logins:
                user_props: dict[str, Any] = {"name": login, "login": login}
                if user_alignment is not None:
                    user_props["schema_alignment"] = user_alignment
                user_logins[login] = EntityDraft(
                    entity_id=_user_entity_id(login),
                    entity_type=ENTITY_TYPE_USER,
                    name=login,
                    properties=user_props,
                )
            edge_props: dict[str, Any] = {}
            if attributed_alignment is not None:
                edge_props["schema_alignment"] = attributed_alignment
            edges.append(
                EdgeDraft(
                    source_id=pr_id,
                    target_id=_user_entity_id(login),
                    edge_kind=EDGE_KIND_ATTRIBUTED,
                    properties=edge_props,
                )
            )
            attribution_count += 1

        # Cross-references in body → wasInformedBy edges
        # Skip self-references and references to non-existent PRs.
        seen_targets: set[int] = set()
        for match in _PR_REF_PATTERN.finditer(body):
            ref_num = int(match.group(1))
            if ref_num == pr_number:
                continue
            if ref_num in seen_targets:
                continue
            if ref_num not in known_pr_numbers:
                continue
            seen_targets.add(ref_num)
            informed_props: dict[str, Any] = {"reference_form": "body_mention"}
            if informed_alignment is not None:
                informed_props["schema_alignment"] = informed_alignment
            edges.append(
                EdgeDraft(
                    source_id=pr_id,
                    target_id=_pr_entity_id(ref_num),
                    edge_kind=EDGE_KIND_INFORMED_BY,
                    properties=informed_props,
                )
            )
            cross_ref_count += 1

    return (
        pr_entities,
        list(user_logins.values()),
        edges,
        attribution_count,
        cross_ref_count,
    )


def _execute_through_governed_pipeline(
    registry: StoreRegistry, result: ExtractionResult
) -> tuple[int, int]:
    """Submit drafts as a CommandBatch and return ``(nodes, edges)``."""
    handlers = create_curate_handlers(registry)
    executor = MutationExecutor(
        event_log=registry.operational.event_log,
        handlers=handlers,
    )
    batch = result_to_batch(result, requested_by="eval:github_loader")
    results = executor.execute_batch(batch)
    nodes = sum(
        1
        for r in results
        if r.operation == Operation.ENTITY_CREATE and r.status == CommandStatus.SUCCESS
    )
    edges = sum(
        1
        for r in results
        if r.operation == Operation.LINK_CREATE and r.status == CommandStatus.SUCCESS
    )
    return nodes, edges


def _index_documents(
    registry: StoreRegistry, result: ExtractionResult
) -> int:
    """Index PR bodies into the document store as ``doc:<entity_id>``."""
    document_store = registry.knowledge.document_store
    indexed = 0
    for entity in result.entities:
        # Only PRs have body content worth indexing as docs. Users get
        # no doc — they're identifier-only nodes for graph traversal.
        if entity.entity_type != ENTITY_TYPE_PR:
            continue
        title = entity.properties.get("title", "") or ""
        body = entity.properties.get("body", "") or ""
        if not body and not title:
            continue
        # Concatenate title + body for the indexed document content.
        # Title repetition gives keyword search a stronger signal on
        # short queries that mention the PR's topic.
        content = f"{title}\n\n{body}" if body else title
        document_store.put(
            doc_id=f"doc:{entity.entity_id}",
            content=content,
            metadata={
                "source": "github",
                "entity_id": entity.entity_id,
                "entity_type": entity.entity_type,
                "name": entity.name,
                "pr_number": entity.properties.get("pr_number"),
                "labels": entity.properties.get("labels", []),
                "content_type": "entity_summary",
                "content_tags": {"signal_quality": "standard"},
                "content": content,
            },
        )
        indexed += 1
    return indexed


def load_github_corpus(
    registry: StoreRegistry,
    *,
    snapshot_path: Path = DEFAULT_SNAPSHOT_PATH,
) -> GitHubLoadResult:
    """Load the trellis-ai GitHub PR snapshot into the registry."""
    if not snapshot_path.exists():
        msg = (
            f"GitHub PR snapshot not found at {snapshot_path}. "
            f"See eval/corpora/github_trellis/README.md for fetch steps."
        )
        raise FileNotFoundError(msg)

    raw = snapshot_path.read_text(encoding="utf-8")
    snapshot = json.loads(raw)

    logger.info(
        "github_loader.start",
        snapshot_path=str(snapshot_path),
        pr_count=len(snapshot),
    )

    pr_entities, user_entities, edges, attribution_count, cross_ref_count = (
        _build_pr_drafts(snapshot)
    )
    extraction = ExtractionResult(
        entities=[*pr_entities, *user_entities],
        edges=edges,
        extractor_used="github_trellis_loader",
        tier="deterministic",
        provenance=ExtractionProvenance(
            extractor_name="github_trellis_loader",
            extractor_version="0.1.0",
            source_hint="github-pr-snapshot",
        ),
    )
    nodes_created, edges_created = _execute_through_governed_pipeline(
        registry, extraction
    )
    documents_indexed = _index_documents(registry, extraction)

    result = GitHubLoadResult(
        prs_extracted=len(pr_entities),
        users_extracted=len(user_entities),
        edges_extracted=len(edges),
        nodes_created=nodes_created,
        edges_created=edges_created,
        documents_indexed=documents_indexed,
        cross_reference_edges=cross_ref_count,
        attribution_edges=attribution_count,
    )
    logger.info("github_loader.done", **result.as_metrics(prefix="corpus"))
    return result


def build_pr_name_index(registry: StoreRegistry) -> dict[str, str]:
    """Build a name→entity_id index for the loaded GitHub corpus.

    Mirrors :func:`eval.corpora.dbt_loader.build_name_index` for use
    with :func:`eval.corpora.dbt_loader.extract_seed_ids` (the regex
    extractor is corpus-agnostic — only the index needs to be
    corpus-specific).

    Indexes:
    - Each PR's ``#NNN`` form (e.g., ``"#42"``) → its entity_id
    - Each PR's bare number form (e.g., ``"42"``) → its entity_id
    - Each user's login → its entity_id
    """
    # GraphStore.query() defaults to limit=50 — too small for this
    # corpus (90 entities). 5000 is a generous ceiling for any
    # reasonable single-corpus scenario.
    index: dict[str, str] = {}
    for node in registry.knowledge.graph_store.query(limit=5000):
        entity_id = node["node_id"]
        properties = node.get("properties") or {}
        node_type = node.get("node_type", "")
        if node_type == ENTITY_TYPE_PR:
            pr_num = properties.get("pr_number")
            if pr_num is not None:
                index.setdefault(f"#{pr_num}", entity_id)
                index.setdefault(str(pr_num), entity_id)
        elif node_type == ENTITY_TYPE_USER:
            login = properties.get("login", "")
            if login:
                index.setdefault(login, entity_id)
    return index
