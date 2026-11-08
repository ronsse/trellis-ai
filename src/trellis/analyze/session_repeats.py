"""How often a session is re-served what an earlier pack of it already served.

Session dedup (``PackBuilder._is_suppressed``) suppresses by ``item_id`` and by
content hash. MinHash semantic dedup collapses a near-duplicate cluster to one
member, but only within a single pack. So a later pack of the same session can
serve a *different* member of a cluster an earlier pack served: a different id,
nearly the same text, and no rule that catches it (#1208). Closing that gap
would change what packs serve and may need near-duplicate signatures on
``PACK_ASSEMBLED``, which the holdout readout reads. This module is the
measurement that says whether that is worth doing. It builds no fix.

**Read-only.** It reads ``PACK_ASSEMBLED`` and the document store and writes
nothing. Held-out packs are dropped (:func:`~trellis.core.pack_holdout.drop_holdout`),
a sectioned pack is read through the same typed rows a flat one is (#958), and a
pack with no ``session_id`` is counted and ignored.

**The classes.** Every item a pack served is judged against the *earlier* packs
of its session that fall inside ``window_minutes`` (the session-dedup window),
and lands in exactly one class, in this precedence:

1. ``exact_repeat``: the same ``item_id`` was served earlier in the window.
   Session dedup should have prevented it (unless ``refresh`` was set, the
   content hash changed, or the event cap was hit). Counted, not explained.
2. ``unreadable``: the document store has no readable row for the item, so its
   text cannot be judged. Never guessed into another class. A graph entity
   lands here, because it is not a document row.
3. ``near_duplicate``: a *different* ``item_id`` served earlier whose text
   :class:`~trellis.classify.dedup.minhash.MinHashIndex` matches at the
   threshold the pack builder uses.
4. ``chunk_sibling``: not a near-duplicate, but a different chunk of the same
   parent, or the parent and its own chunk, was served earlier.
5. ``fresh``: anything else.

**The predicate is the builder's.** The index is built from the same
:class:`~trellis.retrieve.pack_builder.SemanticDedupConfig` defaults the
builder factory passes, and it compares the same text the builder compares: the
document's content cut to the excerpt window, with a leading YAML frontmatter
block stripped. A test pins the analyzer's verdict against
``PackBuilder._semantic_dedup_tracked`` over the same texts.

**What it cannot see.** Text is the document row *as it is now*; the store keeps
no history, so a document edited since the serving is judged as edited. The scan
is capped (a truncated window is stated, not hidden). A pre-#958 sectioned pack
carries no per-item rows and is counted, not read.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import Field

from trellis.classify.dedup.minhash import MinHashIndex
from trellis.core.base import TrellisModel
from trellis.core.knobs import resolve_excerpt_max_chars
from trellis.core.pack_holdout import drop_holdout
from trellis.feedback.pack_rows import ServedRow, served_rows_from_payload
from trellis.retrieve.concentration import parent_id_of
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.retrieve.pack_builder import (
    DEFAULT_SESSION_DEDUP_WINDOW_MINUTES,
    SemanticDedupConfig,
    _strip_dedup_frontmatter,
)
from trellis.stores.base.document import DocumentStore
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    EventLog,
    EventType,
    ScanCoverage,
    resolve_scan_window,
    scan_events,
)

if TYPE_CHECKING:
    from trellis.stores.base.event_log import Event

logger = structlog.get_logger()

#: A session's top-N list stops here. Counts only, no ids.
TOP_SESSIONS = 5
#: Near-duplicate pairs a top session carries in JSON (ids; never printed).
_MAX_PAIRS_PER_SESSION = 5
#: The pack builder's token estimate, restated only in the note below.
_CHARS_PER_TOKEN = 4

_Status = Literal["ok", "empty"]

STATUS_OK: _Status = "ok"
STATUS_EMPTY: _Status = "empty"

_SESSION_HASH_DOMAIN = b"trellis.analyze.session_repeats\x00"
_SESSION_HASH_CHARS = 12


def session_hash(session_id: str) -> str:
    """An opaque, stable handle for a session id.

    The report names a session only by this, so a text report can be pasted
    without carrying the id. It is a handle, not a secret: a session id is not
    private, but it is also not needed to act on a count.
    """
    digest = hashlib.sha256(_SESSION_HASH_DOMAIN + session_id.encode("utf-8"))
    return digest.hexdigest()[:_SESSION_HASH_CHARS]


class ClassTally(TrellisModel):
    """Items and estimated tokens that fell into one class."""

    items: int = 0
    #: Sum of the serving rows' ``estimated_tokens``: the builder's own
    #: ~4 chars/token estimate, as it was when the pack was assembled.
    estimated_tokens: int = 0


class NearDuplicatePair(TrellisModel):
    """One later-pack item and the earlier-served sibling it matched.

    Carried in JSON only; the text report never prints an item id.
    """

    item_id: str
    sibling_item_id: str
    similarity: float


class SessionRepeatCount(TrellisModel):
    """One session's near-duplicate count, under an opaque handle."""

    session: str
    packs: int
    later_pack_items: int
    near_duplicates: int
    pairs: list[NearDuplicatePair] = Field(default_factory=list)


class DedupBasis(TrellisModel):
    """The predicate the near-duplicate class used, echoed so it can be audited."""

    threshold: float
    num_perm: int
    num_bands: int
    shingle_size: int
    min_shingles: int
    excerpt_max_chars: int


class SessionRepeatsReport(TrellisModel):
    """The measurement. Counts and estimated tokens per class, no fix implied."""

    status: _Status = STATUS_OK
    empty_reason: str = ""
    window_days: int
    window_minutes: int
    basis: DedupBasis

    packs_scanned: int = 0
    packs_held_out: int = 0
    packs_without_session: int = 0
    #: Packs with a session id but no per-item rows (a pre-#958 sectioned
    #: pack, or an empty one). Counted, not read.
    packs_without_rows: int = 0
    #: Sessions with at least two packs that carry item rows.
    sessions_multi_pack: int = 0
    #: Items served by a pack that has an earlier pack of the same session
    #: inside the window. The denominator of every share below.
    later_pack_items: int = 0
    #: Items of a second-or-later pack whose session has no earlier pack inside
    #: the window; session dedup would not have applied, so they are not judged.
    outside_window_items: int = 0

    exact_repeat: ClassTally = Field(default_factory=ClassTally)
    near_duplicate: ClassTally = Field(default_factory=ClassTally)
    chunk_sibling: ClassTally = Field(default_factory=ClassTally)
    fresh: ClassTally = Field(default_factory=ClassTally)
    unreadable: ClassTally = Field(default_factory=ClassTally)

    #: ``near_duplicate.items / later_pack_items``; ``None`` with no later item.
    near_duplicate_share: float | None = None
    top_sessions: list[SessionRepeatCount] = Field(default_factory=list)
    #: Served rows that carried no usable ``estimated_tokens``; counted as 0.
    rows_without_token_estimate: int = 0
    scan: ScanCoverage = Field(default_factory=ScanCoverage)
    notes: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Pack:
    at: datetime
    pack_id: str
    rows: tuple[ServedRow, ...]


@dataclass(frozen=True, slots=True)
class _Doc:
    #: What the builder would compare: the excerpt cut, frontmatter stripped.
    text: str
    parent: str


class _DocReader:
    """Read each served id once from the document store, as it is now.

    A missing row, a row whose content is not text and a store that raises all
    read as ``None``. The caller counts that as ``unreadable`` and never as a
    verdict, and one bad row must not abort a measurement.
    """

    def __init__(self, store: DocumentStore, *, excerpt_max_chars: int) -> None:
        self._store = store
        self._excerpt_max_chars = excerpt_max_chars
        self._cache: dict[str, _Doc | None] = {}

    def read(self, item_id: str) -> _Doc | None:
        if item_id not in self._cache:
            self._cache[item_id] = self._load(item_id)
        return self._cache[item_id]

    def parent(self, item_id: str) -> str:
        """The source document ``item_id`` is a serving of; its id scheme if unread."""
        doc = self.read(item_id)
        return doc.parent if doc is not None else parent_id_of(item_id, None)

    def _load(self, item_id: str) -> _Doc | None:
        try:
            row = self._store.get(item_id)
        except Exception:
            # GRACEFUL-DEGRADATION: one unreadable row must not abort a
            # measurement; it is counted as ``unreadable`` instead.
            logger.warning("session_repeats_document_read_failed")
            return None
        if row is None:
            return None
        content = row.get("content")
        if not isinstance(content, str):
            return None
        metadata = row.get("metadata")
        excerpt = truncate_excerpt(content, limit=self._excerpt_max_chars)
        return _Doc(
            text=_strip_dedup_frontmatter(excerpt),
            parent=parent_id_of(
                item_id, metadata if isinstance(metadata, Mapping) else None
            ),
        )


@dataclass(slots=True)
class _SessionTally:
    packs: int
    later_pack_items: int = 0
    near_duplicates: int = 0
    pairs: list[NearDuplicatePair] = field(default_factory=list)


@dataclass(slots=True)
class _Totals:
    exact_repeat: ClassTally = field(default_factory=ClassTally)
    near_duplicate: ClassTally = field(default_factory=ClassTally)
    chunk_sibling: ClassTally = field(default_factory=ClassTally)
    fresh: ClassTally = field(default_factory=ClassTally)
    unreadable: ClassTally = field(default_factory=ClassTally)
    later_pack_items: int = 0
    outside_window_items: int = 0
    rows_without_token_estimate: int = 0
    unreadable_ids: set[str] = field(default_factory=set)
    sessions: dict[str, _SessionTally] = field(default_factory=dict)


def _as_utc(moment: datetime) -> datetime:
    return (
        moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    )


def _tokens_of(row: ServedRow, totals: _Totals) -> int:
    raw = row.estimated_tokens
    if raw is None or isinstance(raw, bool) or not math.isfinite(raw):
        totals.rows_without_token_estimate += 1
        return 0
    return int(raw)


def _tally(into: ClassTally, tokens: int) -> None:
    into.items += 1
    into.estimated_tokens += tokens


def _collect_sessions(
    events: list[Event], report_counts: dict[str, int]
) -> dict[str, list[_Pack]]:
    """Group the served packs by ``session_id``, each group in time order."""
    sessions: dict[str, list[_Pack]] = defaultdict(list)
    for event in events:
        payload = event.payload or {}
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            report_counts["without_session"] += 1
            continue
        pack_id = event.entity_id or ""
        rows = tuple(
            row
            for row in served_rows_from_payload(
                pack_id, payload, include_sectioned=True, include_holdout=False
            )
            if row.item_id
        )
        if not rows:
            report_counts["without_rows"] += 1
            continue
        sessions[session_id].append(
            _Pack(at=_as_utc(event.occurred_at), pack_id=pack_id, rows=rows)
        )
    return {
        sid: sorted(packs, key=lambda p: (p.at, p.pack_id))
        for sid, packs in sessions.items()
    }


def _classify_session(
    packs: list[_Pack],
    *,
    window: timedelta,
    reader: _DocReader,
    index: MinHashIndex,
    totals: _Totals,
    session: _SessionTally,
) -> None:
    for j in range(1, len(packs)):
        pack = packs[j]
        earlier = [p for p in packs[:j] if pack.at - p.at <= window]
        if not earlier:
            totals.outside_window_items += len(pack.rows)
            continue
        earlier_ids = {row.item_id for p in earlier for row in p.rows if row.item_id}
        earlier_parents = {reader.parent(item_id) for item_id in earlier_ids}
        for row in pack.rows:
            item_id = row.item_id
            if item_id is None:
                continue
            tokens = _tokens_of(row, totals)
            totals.later_pack_items += 1
            session.later_pack_items += 1
            if item_id in earlier_ids:
                _tally(totals.exact_repeat, tokens)
                continue
            doc = reader.read(item_id)
            if doc is None:
                _tally(totals.unreadable, tokens)
                totals.unreadable_ids.add(item_id)
                continue
            matches = [
                (match_id, similarity)
                for match_id, similarity in index.query(doc.text, exclude_ids={item_id})
                if match_id in earlier_ids
            ]
            if matches:
                _tally(totals.near_duplicate, tokens)
                session.near_duplicates += 1
                if len(session.pairs) < _MAX_PAIRS_PER_SESSION:
                    sibling, similarity = matches[0]
                    session.pairs.append(
                        NearDuplicatePair(
                            item_id=item_id,
                            sibling_item_id=sibling,
                            similarity=round(similarity, 3),
                        )
                    )
            elif doc.parent in earlier_parents:
                _tally(totals.chunk_sibling, tokens)
            else:
                _tally(totals.fresh, tokens)


def _notes(report: SessionRepeatsReport, *, unreadable_ids: int) -> list[str]:
    notes = [
        (
            "Text is each document's current row, cut to the excerpt window and "
            "with a leading frontmatter block stripped, as the pack builder's "
            "semantic dedup compares it. The store keeps no history, so an edited "
            "document is judged as edited."
        ),
        (
            f"Estimated tokens are the serving rows' own estimate (about "
            f"{_CHARS_PER_TOKEN} chars per token), summed per class."
        ),
        (
            "An exact repeat is counted, not explained: session dedup should have "
            "prevented it unless refresh was set, the content hash changed, or "
            "the event cap was hit."
        ),
    ]
    if report.scan.truncated:
        notes.append(f"Scan truncated: {report.scan.note}")
    if report.unreadable.items:
        notes.append(
            f"{report.unreadable.items} servings ({unreadable_ids} distinct ids) "
            "had no readable document row (a graph entity, a removed document, or "
            "a store error) and were not judged; they are in no other class."
        )
    if report.packs_without_rows:
        notes.append(
            f"{report.packs_without_rows} packs carried a session id but no "
            "per-item rows (assembled before #958, or empty) and were not read."
        )
    if report.rows_without_token_estimate:
        notes.append(
            f"{report.rows_without_token_estimate} served rows carried no "
            "estimated_tokens and count as 0 tokens."
        )
    return notes


def summarize_session_repeats(
    event_log: EventLog,
    document_store: DocumentStore,
    *,
    days: int = 30,
    window_minutes: int = DEFAULT_SESSION_DEDUP_WINDOW_MINUTES,
    limit: int = DEFAULT_SCAN_LIMIT,
    dedup_config: SemanticDedupConfig | None = None,
    now: datetime | None = None,
) -> SessionRepeatsReport:
    """Classify every later-pack item of every multi-pack session in the window.

    Args:
        event_log: Operational log holding ``PACK_ASSEMBLED``.
        document_store: Read for each served id's current text.
        days: Look-back window, as ``trellis analyze value``.
        window_minutes: How far back from a pack its session's earlier packs
            count. Defaults to the session-dedup window.
        limit: Cap on the ``PACK_ASSEMBLED`` scan; the newest are kept.
        dedup_config: The near-duplicate predicate. ``None`` is the config a
            default-built pack builder gets, which is what the question asks.
        now: Clock override for tests.

    Returns:
        A :class:`SessionRepeatsReport`. Nothing is written anywhere.
    """
    config = dedup_config or SemanticDedupConfig()
    excerpt_max_chars = resolve_excerpt_max_chars(None)
    since, until = resolve_scan_window(days, now=now)
    scan = scan_events(
        event_log,
        event_type=EventType.PACK_ASSEMBLED,
        since=since,
        until=until,
        limit=limit,
    )
    served, _ = drop_holdout(scan.events, [])
    counts = {"without_session": 0, "without_rows": 0}
    sessions = _collect_sessions(served, counts)
    multi = {sid: packs for sid, packs in sessions.items() if len(packs) >= 2}  # noqa: PLR2004

    reader = _DocReader(document_store, excerpt_max_chars=excerpt_max_chars)
    index = MinHashIndex(
        num_perm=config.num_perm,
        num_bands=config.num_bands,
        threshold=config.threshold,
        shingle_size=config.shingle_size,
        min_shingles=config.min_shingles,
    )
    # One global index over every served id of a multi-pack session: a
    # signature is computed once per id, and a match only counts when it was
    # served earlier in the *same* session's window (``earlier_ids``).
    seen: set[str] = set()
    for packs in multi.values():
        for pack in packs:
            for row in pack.rows:
                if row.item_id and row.item_id not in seen:
                    seen.add(row.item_id)
                    doc = reader.read(row.item_id)
                    if doc is not None:
                        index.add(row.item_id, doc.text)

    window = timedelta(minutes=window_minutes)
    totals = _Totals()
    for sid, packs in multi.items():
        session = _SessionTally(packs=len(packs))
        totals.sessions[sid] = session
        _classify_session(
            packs,
            window=window,
            reader=reader,
            index=index,
            totals=totals,
            session=session,
        )

    ranked = sorted(
        (
            (sid, tally)
            for sid, tally in totals.sessions.items()
            if tally.near_duplicates
        ),
        key=lambda pair: (
            -pair[1].near_duplicates,
            -pair[1].later_pack_items,
            session_hash(pair[0]),
        ),
    )[:TOP_SESSIONS]
    share = (
        totals.near_duplicate.items / totals.later_pack_items
        if totals.later_pack_items
        else None
    )
    report = SessionRepeatsReport(
        status=STATUS_OK if totals.later_pack_items else STATUS_EMPTY,
        empty_reason=(
            ""
            if totals.later_pack_items
            else "no multi-pack session has a later pack inside the window"
        ),
        window_days=days,
        window_minutes=window_minutes,
        basis=DedupBasis(
            threshold=config.threshold,
            num_perm=config.num_perm,
            num_bands=config.num_bands,
            shingle_size=config.shingle_size,
            min_shingles=config.min_shingles,
            excerpt_max_chars=excerpt_max_chars,
        ),
        packs_scanned=len(scan.events),
        packs_held_out=len(scan.events) - len(served),
        packs_without_session=counts["without_session"],
        packs_without_rows=counts["without_rows"],
        sessions_multi_pack=len(multi),
        later_pack_items=totals.later_pack_items,
        outside_window_items=totals.outside_window_items,
        exact_repeat=totals.exact_repeat,
        near_duplicate=totals.near_duplicate,
        chunk_sibling=totals.chunk_sibling,
        fresh=totals.fresh,
        unreadable=totals.unreadable,
        near_duplicate_share=share,
        top_sessions=[
            SessionRepeatCount(
                session=session_hash(sid),
                packs=tally.packs,
                later_pack_items=tally.later_pack_items,
                near_duplicates=tally.near_duplicates,
                pairs=tally.pairs,
            )
            for sid, tally in ranked
        ],
        rows_without_token_estimate=totals.rows_without_token_estimate,
        scan=scan.coverage,
    )
    report.notes = _notes(report, unreadable_ids=len(totals.unreadable_ids))
    logger.debug(
        "session_repeats_summarized",
        sessions=len(multi),
        later_pack_items=report.later_pack_items,
        near_duplicates=report.near_duplicate.items,
    )
    return report


__all__ = [
    "STATUS_EMPTY",
    "STATUS_OK",
    "TOP_SESSIONS",
    "ClassTally",
    "DedupBasis",
    "NearDuplicatePair",
    "SessionRepeatCount",
    "SessionRepeatsReport",
    "session_hash",
    "summarize_session_repeats",
]
