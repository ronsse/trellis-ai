"""How many stored documents say the same thing as another in different words.

Ingest dedup is lexical: an exact content hash, then a character-trigram
:class:`~trellis.classify.dedup.minhash.MinHashIndex` at its shipped Jaccard
threshold. A reworded fact passes both. This module counts that residue: pairs
of whole documents whose stored embeddings are close (cosine at or above a
threshold) that the shipped MinHash predicate does *not* already catch. It is a
measurement for one candidate answer to "what should be trimmed"; it builds no
merge and changes no serving.

**Read-only.** It calls only ``list_documents``, ``count`` and ``get`` on the
document store and ``get`` and ``query`` on the vector store. It writes nothing,
emits no event and builds no ``MutationExecutor``.

**Population.** Whole documents only (``include_chunks=False``), walked newest
first up to ``max_docs`` (a hit cap is reported). Each listed document lands in
exactly one class, in this precedence: ``archived``, ``superseded`` (a
lifecycle stamp naming a successor), ``noise`` (``signal_quality`` noise),
``unreadable`` (content that is not text), then ``no_embedding`` (the vector
store has no row under the doc id), ``embedding_unusable`` (a row that cannot be
read as a non-zero numeric vector), or ``embedded``. Only embedded documents
are paired.

**Embeddings and cost.** A document's vector row is keyed by its ``doc_id``
(the embed-on-ingest hook writes them 1:1). The ABC has no enumeration, so each
embedded document is read with ``get`` and then used as the probe of one
``query(top_k=neighbors)``. Neighbours outside the population (chunks, other
item kinds, skipped documents) are discarded, and the cosine is recomputed from
the two fetched vectors rather than trusting a backend's score. Cost is ``n``
gets plus ``n`` queries (each a full scan on the SQLite backend) and at most
``n * neighbors`` cosines; there is no all-pairs loop. Because a query returns
only ``neighbors`` rows, a document with many close rows can push a real pair
out of its list, so every count is a lower bound.

**The lexical predicate is ingest's.** A default-built ``MinHashIndex`` (the
one ``save_memory`` seeds) over each candidate's full content; a pair is
lexical when either member's query returns the other.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import structlog
from pydantic import Field

from trellis.classify.dedup.minhash import MinHashIndex
from trellis.core.base import TrellisModel
from trellis.retrieve.lifecycle import declared_successor, is_archived
from trellis.retrieve.noise import DEFAULT_SIGNAL_QUALITY_SPEC, passes_signal_quality
from trellis.stores.base.document import DocumentStore
from trellis.stores.base.vector import VectorStore

logger = structlog.get_logger()

#: The two cosine floors reported unless the caller names others.
DEFAULT_THRESHOLDS: tuple[float, ...] = (0.90, 0.95)
#: Documents read before the walk stops and says so.
DEFAULT_MAX_DOCS = 5000
#: Rows each per-document similarity query returns.
DEFAULT_NEIGHBORS = 20
#: Pairs carried per threshold and kind (ids and cosine only).
SAMPLE_PAIRS = 10
#: Groups listed per threshold, largest first.
TOP_GROUPS = 10

_PAGE_SIZE = 500
_NO_PREFIX = "(no prefix)"
_ID_PREFIX_SEPARATOR = ":"

_Status = Literal["ok", "empty"]

STATUS_OK: _Status = "ok"
STATUS_EMPTY: _Status = "empty"

CANNOT_TELL: tuple[str, ...] = (
    (
        "Whether a residue pair states the same fact, or one is an update, a "
        "correction or a contradiction of the other: cosine measures topic and "
        "wording closeness, not agreement."
    ),
    (
        "Whether merging or trimming either member would help retrieval outcomes; "
        "no pack, feedback or outcome is read."
    ),
    (
        "Anything about unembedded documents: they are counted and never paired, "
        "so a paraphrase involving one is invisible here."
    ),
    (
        "The thresholds are uncalibrated: no labelled sample says what share of "
        "pairs at 0.90 or 0.95 are true paraphrases for this embedder."
    ),
    (
        "Pairs a neighbour query did not return: each document is probed with a "
        "bounded top-k query, so every count is a lower bound."
    ),
)


class SkippedCounts(TrellisModel):
    """Listed documents left out of the population, by reason."""

    archived: int = 0
    superseded: int = 0
    noise: int = 0
    #: Content that is not a string; it cannot be judged lexically.
    unreadable: int = 0

    @property
    def total(self) -> int:
        return self.archived + self.superseded + self.noise + self.unreadable


class PairSample(TrellisModel):
    """One pair by id with its cosine. Never document text."""

    doc_id: str
    other_doc_id: str
    cosine: float


class GroupCount(TrellisModel):
    """Residue membership for one source system (or id prefix)."""

    group: str
    documents_embedded: int
    documents_in_residue: int


class ThresholdResult(TrellisModel):
    """Pair counts at one cosine floor."""

    threshold: float
    pairs: int = 0
    #: Pairs the shipped MinHash predicate already catches at ingest.
    lexical_pairs: int = 0
    #: Pairs it does not: the paraphrase residue.
    residue_pairs: int = 0
    documents_in_residue: int = 0
    #: Connected components of the residue-pair graph.
    clusters: int = 0
    largest_cluster: int = 0
    by_group: list[GroupCount] = Field(default_factory=list)
    residue_sample: list[PairSample] = Field(default_factory=list)
    lexical_sample: list[PairSample] = Field(default_factory=list)


class NearDuplicatesBasis(TrellisModel):
    """The predicates used, echoed so a reader can audit them."""

    minhash_threshold: float
    minhash_shingle_size: int
    minhash_num_perm: int
    neighbors: int
    group_key: str = "metadata.source_system, else the doc id before the first ':'"


class NearDuplicatesReport(TrellisModel):
    """The measurement. Counts only; no merge is implied."""

    status: _Status = STATUS_OK
    empty_reason: str = ""
    basis: NearDuplicatesBasis
    max_docs: int
    cap_hit: bool = False
    #: Whole documents in the store, counted beside the cap.
    documents_in_store: int = 0
    documents_considered: int = 0
    skipped: SkippedCounts = Field(default_factory=SkippedCounts)
    no_embedding: int = 0
    embedding_unusable: int = 0
    embedded: int = 0
    #: Probe queries that raised; that document's neighbours are unknown.
    neighbor_queries_failed: int = 0
    #: Candidate pairs whose two vectors differ in length; not compared.
    dimension_mismatches: int = 0
    thresholds: list[ThresholdResult] = Field(default_factory=list)
    cannot_tell: list[str] = Field(default_factory=lambda: list(CANNOT_TELL))
    notes: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Doc:
    doc_id: str
    content: str
    group: str
    unit: Any  # a normalised numpy vector


def group_of(doc_id: str, metadata: Mapping[str, Any] | None) -> str:
    """``metadata["source_system"]`` if a non-empty string, else the id prefix."""
    source = (metadata or {}).get("source_system")
    if isinstance(source, str) and source:
        return source
    if _ID_PREFIX_SEPARATOR in doc_id:
        return doc_id.split(_ID_PREFIX_SEPARATOR, 1)[0] or _NO_PREFIX
    return _NO_PREFIX


def _list_population(store: DocumentStore, max_docs: int) -> list[dict[str, Any]]:
    """Whole documents, newest first, distinct by id, at most ``max_docs``."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    while len(rows) < max_docs:
        requested = min(_PAGE_SIZE, max_docs - len(rows))
        page = store.list_documents(
            limit=requested, offset=offset, include_chunks=False
        )
        if not page:
            break
        offset += len(page)
        fresh = 0
        for row in page:
            doc_id = row.get("doc_id")
            if isinstance(doc_id, str) and doc_id not in seen:
                seen.add(doc_id)
                rows.append(row)
                fresh += 1
        if len(page) < requested or fresh == 0:
            break
    return rows[:max_docs]


def _unit_vector(row: Mapping[str, Any] | None) -> Any | None:
    """The row's vector normalised, or ``None`` when it is unusable."""
    if row is None:
        return None
    raw = row.get("vector")
    try:
        vec = np.asarray(raw, dtype=float)
    except (TypeError, ValueError):
        return None
    if vec.ndim != 1 or vec.size == 0 or not np.all(np.isfinite(vec)):
        return None
    norm = float(np.linalg.norm(vec))
    if norm == 0.0 or not math.isfinite(norm):
        return None
    return vec / norm


def _classify(
    rows: list[dict[str, Any]], vectors: VectorStore, report: NearDuplicatesReport
) -> list[_Doc]:
    docs: list[_Doc] = []
    for row in rows:
        doc_id = row["doc_id"]
        metadata = row.get("metadata")
        meta = metadata if isinstance(metadata, dict) else None
        if is_archived(meta):
            report.skipped.archived += 1
            continue
        if declared_successor(meta) is not None:
            report.skipped.superseded += 1
            continue
        if not passes_signal_quality(meta, DEFAULT_SIGNAL_QUALITY_SPEC):
            report.skipped.noise += 1
            continue
        content = row.get("content")
        if not isinstance(content, str):
            report.skipped.unreadable += 1
            continue
        try:
            vector_row = vectors.get(doc_id)
        except Exception:
            # GRACEFUL-DEGRADATION: one unreadable vector row must not abort a
            # measurement; it is counted as unusable instead.
            logger.warning("near_duplicates_vector_read_failed")
            report.embedding_unusable += 1
            continue
        if vector_row is None:
            report.no_embedding += 1
            continue
        unit = _unit_vector(vector_row)
        if unit is None:
            report.embedding_unusable += 1
            continue
        docs.append(_Doc(doc_id, content, group_of(doc_id, meta), unit))
    return docs


def _candidate_pairs(
    docs: list[_Doc],
    vectors: VectorStore,
    *,
    floor: float,
    neighbors: int,
    report: NearDuplicatesReport,
) -> dict[tuple[str, str], float]:
    """Unordered pairs inside the population with cosine >= ``floor``."""
    by_id = {doc.doc_id: doc for doc in docs}
    pairs: dict[tuple[str, str], float] = {}
    for doc in docs:
        try:
            hits = vectors.query(doc.unit.tolist(), top_k=neighbors)
        except Exception:
            # GRACEFUL-DEGRADATION: counted, so a failed probe is visible.
            logger.warning("near_duplicates_neighbor_query_failed")
            report.neighbor_queries_failed += 1
            continue
        for hit in hits:
            other_id = hit.get("item_id")
            other = by_id.get(other_id) if isinstance(other_id, str) else None
            if other is None or other.doc_id == doc.doc_id:
                continue
            key = (min(doc.doc_id, other.doc_id), max(doc.doc_id, other.doc_id))
            if key in pairs:
                continue
            if other.unit.shape != doc.unit.shape:
                report.dimension_mismatches += 1
                continue
            cosine = float(np.dot(doc.unit, other.unit))
            if cosine >= floor:
                pairs[key] = cosine
    return pairs


def _lexical_pairs(
    pairs: Mapping[tuple[str, str], float], by_id: Mapping[str, _Doc]
) -> set[tuple[str, str]]:
    """The pairs ingest's MinHash predicate already catches."""
    index = MinHashIndex()
    members = sorted({doc_id for pair in pairs for doc_id in pair})
    for doc_id in members:
        index.add(doc_id, by_id[doc_id].content)
    matches: dict[str, set[str]] = {
        doc_id: {
            match_id
            for match_id, _ in index.query(by_id[doc_id].content, exclude_ids={doc_id})
        }
        for doc_id in members
    }
    return {(a, b) for a, b in pairs if b in matches[a] or a in matches[b]}


def _components(pairs: Sequence[tuple[str, str]]) -> list[int]:
    """Sizes of the connected components of an undirected edge list."""
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for a, b in pairs:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_a] = root_b
    sizes: dict[str, int] = defaultdict(int)
    for node in list(parent):
        sizes[find(node)] += 1
    return sorted(sizes.values(), reverse=True)


def _sample(pairs: list[tuple[tuple[str, str], float]]) -> list[PairSample]:
    ranked = sorted(pairs, key=lambda item: (-item[1], item[0]))[:SAMPLE_PAIRS]
    return [
        PairSample(doc_id=a, other_doc_id=b, cosine=round(cosine, 4))
        for (a, b), cosine in ranked
    ]


def _threshold_result(
    threshold: float,
    pairs: Mapping[tuple[str, str], float],
    lexical: set[tuple[str, str]],
    by_id: Mapping[str, _Doc],
    embedded_by_group: Mapping[str, int],
) -> ThresholdResult:
    kept = [(key, cos) for key, cos in pairs.items() if cos >= threshold]
    lexical_rows = [(key, cos) for key, cos in kept if key in lexical]
    residue_rows = [(key, cos) for key, cos in kept if key not in lexical]
    sizes = _components([key for key, _ in residue_rows])
    in_residue = {doc_id for key, _ in residue_rows for doc_id in key}
    per_group: dict[str, int] = defaultdict(int)
    for doc_id in in_residue:
        per_group[by_id[doc_id].group] += 1
    groups = sorted(per_group.items(), key=lambda item: (-item[1], item[0]))
    return ThresholdResult(
        threshold=threshold,
        pairs=len(kept),
        lexical_pairs=len(lexical_rows),
        residue_pairs=len(residue_rows),
        documents_in_residue=len(in_residue),
        clusters=len(sizes),
        largest_cluster=sizes[0] if sizes else 0,
        by_group=[
            GroupCount(
                group=group,
                documents_embedded=embedded_by_group.get(group, 0),
                documents_in_residue=count,
            )
            for group, count in groups[:TOP_GROUPS]
        ],
        residue_sample=_sample(residue_rows),
        lexical_sample=_sample(lexical_rows),
    )


def _notes(report: NearDuplicatesReport) -> list[str]:
    notes = [
        (
            "A pair is counted once, unordered. Lexical means the shipped ingest "
            "MinHash predicate (default MinHashIndex, full content) already "
            "catches it; residue means it does not."
        ),
    ]
    if report.cap_hit:
        notes.append(
            f"Document cap hit: {report.documents_considered} of "
            f"{report.documents_in_store} whole documents were read (newest "
            f"first, --max-docs {report.max_docs}); older ones are not counted."
        )
    if report.neighbor_queries_failed:
        notes.append(
            f"{report.neighbor_queries_failed} neighbour queries raised; those "
            "documents' pairs are missing from every count."
        )
    if report.dimension_mismatches:
        notes.append(
            f"{report.dimension_mismatches} candidate pairs had vectors of "
            "different lengths (mixed embedders) and were not compared."
        )
    return notes


def summarize_near_duplicates(
    document_store: DocumentStore,
    vector_store: VectorStore,
    *,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    max_docs: int = DEFAULT_MAX_DOCS,
    neighbors: int = DEFAULT_NEIGHBORS,
) -> NearDuplicatesReport:
    """Count embedding-close document pairs that ingest's MinHash does not catch.

    Args:
        document_store: Listed for whole documents (chunks excluded).
        vector_store: Read by doc id and probed with each document's vector.
        thresholds: Cosine floors to report, each its own block.
        max_docs: Most documents read; a hit cap is reported.
        neighbors: ``top_k`` of each probe query.

    Returns:
        A :class:`NearDuplicatesReport`. Nothing is written anywhere.
    """
    floors = tuple(sorted({float(t) for t in thresholds}))
    stats = MinHashIndex().stats()
    report = NearDuplicatesReport(
        basis=NearDuplicatesBasis(
            minhash_threshold=float(stats["threshold"]),
            minhash_shingle_size=int(stats["shingle_size"]),
            minhash_num_perm=int(stats["num_perm"]),
            neighbors=neighbors,
        ),
        max_docs=max_docs,
    )
    rows = _list_population(document_store, max_docs)
    report.documents_in_store = document_store.count(include_chunks=False)
    report.documents_considered = len(rows)
    report.cap_hit = report.documents_in_store > len(rows) and len(rows) >= max_docs

    docs = _classify(rows, vector_store, report)
    report.embedded = len(docs)
    by_id = {doc.doc_id: doc for doc in docs}
    embedded_by_group: dict[str, int] = defaultdict(int)
    for doc in docs:
        embedded_by_group[doc.group] += 1

    pairs = (
        _candidate_pairs(
            docs,
            vector_store,
            floor=floors[0],
            neighbors=neighbors,
            report=report,
        )
        if floors and len(docs) >= 2  # noqa: PLR2004
        else {}
    )
    lexical = _lexical_pairs(pairs, by_id) if pairs else set()
    report.thresholds = [
        _threshold_result(t, pairs, lexical, by_id, embedded_by_group) for t in floors
    ]
    if len(docs) < 2:  # noqa: PLR2004
        report.status = STATUS_EMPTY
        report.empty_reason = "fewer than two embedded documents in the population"
    report.notes = _notes(report)
    logger.debug(
        "near_duplicates_summarized",
        embedded=report.embedded,
        candidate_pairs=len(pairs),
    )
    return report


__all__ = [
    "CANNOT_TELL",
    "DEFAULT_MAX_DOCS",
    "DEFAULT_NEIGHBORS",
    "DEFAULT_THRESHOLDS",
    "SAMPLE_PAIRS",
    "STATUS_EMPTY",
    "STATUS_OK",
    "GroupCount",
    "NearDuplicatesBasis",
    "NearDuplicatesReport",
    "PairSample",
    "SkippedCounts",
    "ThresholdResult",
    "group_of",
    "summarize_near_duplicates",
]
