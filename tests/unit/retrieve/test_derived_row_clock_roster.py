"""Roster guard: every derived-row producer states whose clock its row carries.

``updated_at`` / ``created_at`` name two different facts. As store *columns*
they are the row's write clock; as *metadata keys* they are the **source's**
clock, and :func:`~trellis.retrieve.strategies.resolve_recency_stamp` (#417)
prefers the bag over the column on both document-backed axes. So a row whose
content Trellis composed out of another record has a decision to make that a
primary write does not: propagate the source's clock, or let the axis score
the derived row by the instant the composing pass happened to run.

#417 fixed that for the strategies and left one producer — chunking —
explicitly open as #463. The plan for #463 says the thing this module exists
to honour, and it is not "fix the chunk path":

    **Then sweep the roster, do not fix the one case.** Three successive
    ``updated_at`` reader lists in this repo were each wrong, and #443
    declared 3 control keys against 6 sites. Find **every** derived-row
    path, not just chunking.

The sweep found **three** derived-row producers, and they resolve three
different ways — which is the finding, and is why a patch to the chunk path
alone would have been the wrong shape of answer:

``build_vector_row``
    Already correct, and correct in the *callee*, so all three of its call
    sites inherit it.

``build_trace_metadata``
    Was wrong, fixed here. Latent — zero production rows — but the worker is
    a **backfill by design**, so its first run over an existing trace store
    would have stamped a whole history with one import instant: #417's own
    measured shape, guaranteed rather than merely possible.

``ingest_corpus.sync._write_chunks``
    **Declined, on measurement.** The stamp collapses a chunk onto exactly
    its parent's recency multiplier, after which the longer parent wins the
    tiebreak on raw relevance — and the stamped parents are the worst-cited
    class in the corpus. The full reasoning sits at the code, in
    ``_write_chunks``; the number carrying the decision is a stamped parent
    at 1 helpful citation in 219 servings.
    :class:`TestTheDeclinedDecisionIsPinned` runs a real corpus sync and
    asserts the chunk rows carry **no** clock key while their parent does, so
    a later silent "fix" fails here and sends its author to that comment.

Dispositions, and why the non-derived ones are on the roster at all
-------------------------------------------------------------------

Every scanned site is classified, never filtered. ``advisory_store.put`` is
not a document row and ``parameter_store.put`` is not either — but a name
filter is exactly how a producer hides, so they are *rostered* as
:data:`NOT_A_DOCUMENT_ROW` rather than excluded by a predicate that would
also swallow the next real one.

:data:`IN_PLACE_REPUT` is the disposition with an executable proof of its own
kind: every such site is re-asserted by AST to actually pass a literal
``preserve_updated_at=True``. A re-put that stopped passing it would become a
silent write-clock bump — the #406 shape — and would still read as a correct
roster entry.

**Guarding the guard.** A scanner that under-collects makes every assertion
below vacuous, and #457 shipped three vacuity guards that all stayed green
while its scan dropped 148 branches to 123, because each guard divided by the
population the bug had already truncated. Four things answer that here:

#. Floors on both scans' raw site counts, and a floor per disposition — a
   scan that merely *shrinks* fails rather than passing more easily.
#. Roster-to-scan matching in **both directions, with counts**. A key present
   in one and not the other names itself; a site count that changes under a
   key (an advisory writer growing a fourth ``put``) fails too, because
   "three ``put``\\ s here are all the same non-document write" is a claim
   about three sites and not about a name.
#. The one floor a scan cannot compute for itself: the hand-written roster
   is asserted to be non-empty and to cover a stated total, so a scanner
   returning nothing cannot satisfy the other checks by division.
#. A **synthetic tree** run through the *shipped* scanners
   (:class:`TestTheScanCatchesANewProducer`) — a new module with a new
   derived-row producer must be found and must come back unrostered. The
   proof asserts the synthetic site is genuinely absent from the real roster
   first, so it cannot itself go vacuous.

**Coverage, stated exactly.** Two scans over all of ``src/``. Scan A finds
every ``.put(...)`` whose receiver name contains ``store`` or ``doc``, plus
every ``build_vector_row(...)`` call. Scan B finds every ``metadata=`` /
``meta=`` keyword whose value is a **call** — the half that catches
``trace_embed``, which reaches its row through a governed ``evidence.ingest``
command and never touches a ``.put(``. What is *not* covered: a producer that
composes a metadata bag into a local variable, passes it to a governed
command through a differently-named keyword, and writes no ``.put`` — Scan B
catches the composing call only where it is inlined at the keyword. That
residue is the reason the roster is checked by execution where it can be,
rather than by declaration alone.
"""

from __future__ import annotations

import ast
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple
from unittest.mock import MagicMock

import pytest

import trellis.retrieve.embed_ingest_hook as embed_hook_mod
from trellis.ingest_corpus.models import chunk_doc_id, corpus_doc_id
from trellis.ingest_corpus.sync import sync_corpus
from trellis.retrieve.embed_ingest_hook import build_vector_row
from trellis.schemas.enums import TraceSource
from trellis.schemas.trace import Trace, TraceContext
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.vector import SQLiteVectorStore
from trellis_workers.trace_embed.render import build_trace_metadata

#: ``.../src/trellis/retrieve/embed_ingest_hook.py`` up three.
SRC_ROOT = Path(embed_hook_mod.__file__).parents[2]

#: The two metadata keys ``resolve_recency_stamp`` reads out of the bag.
CLOCK_KEYS = ("updated_at", "created_at")

# --------------------------------------------------------------------------
# Dispositions
# --------------------------------------------------------------------------

#: Not a document row at all — a parameter, advisory, event or tuner-state
#: write, or a store backend reading its own persisted column back. Nothing
#: here is scored by a retrieval axis, so no source clock applies. Rostered
#: rather than filtered: a name filter is how a producer hides.
NOT_A_DOCUMENT_ROW = "not_a_document_row"

#: A caller-supplied write. The metadata is the caller's, the row is new, and
#: the column is the honest answer to "when did Trellis learn this?".
PRIMARY_WRITE = "primary_write"

#: A re-put of a row that already exists, passing ``preserve_updated_at=True``
#: so the write clock does not move. Verified by AST below, not taken on
#: faith.
IN_PLACE_REPUT = "in_place_reput"

#: A derived row that carries its source's clock. Exercised below.
DERIVED_PROPAGATES = "derived_propagates"

#: A derived row whose source has no clock to carry.
DERIVED_NO_SOURCE_CLOCK = "derived_no_source_clock"

#: A derived row that *could* carry its source's clock and deliberately does
#: not. Requires a reason, and is pinned by execution.
DERIVED_CLOCK_DECLINED = "derived_clock_declined"

DERIVED = frozenset(
    {DERIVED_PROPAGATES, DERIVED_NO_SOURCE_CLOCK, DERIVED_CLOCK_DECLINED}
)


class Site(NamedTuple):
    """One scanned write seam or metadata composition."""

    module: str
    function: str
    label: str
    lineno: int

    @property
    def key(self) -> str:
        """Roster key: stable across unrelated edits, unlike a line number."""
        return f"{self.module}::{self.function}::{self.label}"


class Rostered(NamedTuple):
    """One roster entry: how many sites carry this key, and their verdict."""

    count: int
    disposition: str
    reason: str = ""


#: Every derived-row producer, and everything the two scans find beside them.
#:
#: Keyed ``module::function::label`` rather than by line, because a line-keyed
#: roster rots on every unrelated edit above it and then gets "repaired" by
#: renumbering — which is indistinguishable from re-classifying.
ROSTER: dict[str, Rostered] = {
    # -- Derived rows ------------------------------------------------------
    "trellis/mutate/handlers.py::handle::build_vector_row": Rostered(
        1, DERIVED_PROPAGATES
    ),
    "trellis/retrieve/embed_ingest_hook.py::run_embed_on_ingest::build_vector_row": (
        Rostered(1, DERIVED_PROPAGATES)
    ),
    "trellis_cli/admin_reindex_vectors.py::run_reindex_vectors::build_vector_row": (
        Rostered(1, DERIVED_PROPAGATES)
    ),
    "trellis_workers/trace_embed/worker.py::_process_one::"
    "metadata=build_trace_metadata()": Rostered(1, DERIVED_PROPAGATES),
    "trellis_cli/ingest.py::ingest_dbt_manifest::doc_store.put": Rostered(
        1,
        DERIVED_NO_SOURCE_CLOCK,
        "The row is an entity's `description` lifted out of a dbt manifest. "
        "A manifest carries no per-entity clock, so there is no source stamp "
        "to propagate — the column is the only clock that exists.",
    ),
    "trellis/ingest_corpus/sync.py::_write_chunks::doc_store.put": Rostered(
        1,
        DERIVED_CLOCK_DECLINED,
        "Measured and refused (#463). Propagating the parent's stamp moves a "
        "chunk's recency multiplier 0.605 -> 0.336, exactly its parent's, "
        "after which the longer parent wins on raw relevance. A two-arm "
        "replay over 59 attributed packs: -49 chunk servings, +41 "
        "stamped-parent. Stamped parents are the worst-cited class in the "
        "corpus (1 helpful citation in 219 servings). Full reasoning at the "
        "code, in `_write_chunks`.",
    ),
    # -- Primary writes ----------------------------------------------------
    "trellis/ingest_corpus/sync.py::_apply_record::doc_store.put": Rostered(
        1, PRIMARY_WRITE
    ),
    "trellis/mutate/handlers.py::handle::document_store.put": Rostered(
        1, PRIMARY_WRITE
    ),
    "trellis_cli/demo.py::load::doc_store.put": Rostered(2, PRIMARY_WRITE),
    "trellis_cli/ingest.py::ingest_evidence::store.put": Rostered(1, PRIMARY_WRITE),
    # -- In-place re-puts --------------------------------------------------
    "trellis/classify/feedback.py::apply_noise_tags::document_store.put": Rostered(
        1, IN_PLACE_REPUT
    ),
    "trellis/classify/refresh.py::reclassify_item::document_store.put": Rostered(
        1, IN_PLACE_REPUT
    ),
    "trellis/core/derived_metadata.py::apply_derived_metadata::document_store.put": (
        Rostered(1, IN_PLACE_REPUT)
    ),
    "trellis/mcp/reconcile.py::mark_document_superseded::document_store.put": Rostered(
        1, IN_PLACE_REPUT
    ),
    "trellis/mcp/server.py::_commit_reconcile_verdict::document_store.put": Rostered(
        1, IN_PLACE_REPUT
    ),
    "trellis/mutate/handlers.py::_archive::store.put": Rostered(1, IN_PLACE_REPUT),
    "trellis/mutate/handlers.py::_restore::doc_store.put": Rostered(1, IN_PLACE_REPUT),
    "trellis_workers/session_capture/reconcile_pass.py::"
    "_withdraw_supersede_claim::doc_store.put": Rostered(1, IN_PLACE_REPUT),
    # -- Not document rows -------------------------------------------------
    "trellis/learning/tuners/promotion.py::promote_proposal::parameter_store.put": (
        Rostered(1, NOT_A_DOCUMENT_ROW)
    ),
    "trellis/learning/tuners/rollback.py::"
    "monitor_post_promotion::parameter_store.put": Rostered(1, NOT_A_DOCUMENT_ROW),
    "trellis/retrieve/effectiveness.py::"
    "run_advisory_fitness_loop::advisory_store.put": Rostered(3, NOT_A_DOCUMENT_ROW),
    "trellis_cli/analyze.py::_build_learning_registry::store.put": Rostered(
        1, NOT_A_DOCUMENT_ROW
    ),
    "trellis_cli/analyze.py::_build_schema_evolution_registry::store.put": Rostered(
        1, NOT_A_DOCUMENT_ROW
    ),
    "trellis_cli/classify.py::_domain_normalization_registry::store.put": Rostered(
        1, NOT_A_DOCUMENT_ROW
    ),
    "trellis_cli/classify.py::_tag_evolution_registry::store.put": Rostered(
        1, NOT_A_DOCUMENT_ROW
    ),
    "trellis/mutate/evidence_ingest.py::"
    "build_evidence_ingest_command::metadata=dict()": Rostered(
        1,
        NOT_A_DOCUMENT_ROW,
        "Command audit metadata, not the document's — the document's bag rides `args`.",
    ),
    "trellis/stores/arcadedb/vector.py::upsert_bulk::metadata=get()": Rostered(
        1, NOT_A_DOCUMENT_ROW
    ),
    "trellis/stores/base/event_log.py::emit::metadata=stamp_metadata()": Rostered(
        1, NOT_A_DOCUMENT_ROW
    ),
    "trellis/stores/pgvector/store.py::upsert_bulk::metadata=get()": Rostered(
        1, NOT_A_DOCUMENT_ROW
    ),
    "trellis/stores/sqlite/event_log.py::_row_to_event::metadata=loads()": Rostered(
        1, NOT_A_DOCUMENT_ROW
    ),
    "trellis/stores/sqlite/outcome.py::_row_to_outcome::metadata=loads()": Rostered(
        1, NOT_A_DOCUMENT_ROW
    ),
    "trellis/stores/sqlite/parameter.py::_row_to_params::metadata=loads()": Rostered(
        1, NOT_A_DOCUMENT_ROW
    ),
    "trellis/stores/sqlite/tuner_state.py::_row_to_proposal::metadata=loads()": (
        Rostered(1, NOT_A_DOCUMENT_ROW)
    ),
    "trellis/stores/sqlite/vector.py::upsert_bulk::metadata=get()": Rostered(
        1, NOT_A_DOCUMENT_ROW
    ),
}

#: The hand count a scan cannot compute for itself. Every other guard here
#: divides by the scan's own output, so a scan that returns nothing satisfies
#: them all; this one does not move unless a human moves it.
EXPECTED_ROSTER_KEYS = 34
EXPECTED_ROSTER_SITES = 37

#: Per-disposition site floors. Deliberately floors and not equalities for
#: the classes that grow with ordinary work, and an equality for the two
#: derived classes that are the subject of #463 — a fourth derived-row
#: producer appearing unannounced is the event this module exists to catch.
DISPOSITION_SITE_FLOOR = {
    NOT_A_DOCUMENT_ROW: 18,
    PRIMARY_WRITE: 5,
    IN_PLACE_REPUT: 8,
    DERIVED_PROPAGATES: 4,
    DERIVED_NO_SOURCE_CLOCK: 1,
    DERIVED_CLOCK_DECLINED: 1,
}

#: Floors on the two raw scans, before any classification.
MIN_WRITE_SEAMS = 27
MIN_METADATA_COMPOSERS = 10


# --------------------------------------------------------------------------
# The scans
# --------------------------------------------------------------------------


def _callee_name(node: ast.AST) -> str:
    """Rightmost name of a call target / receiver expression, or ``""``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _callee_name(node.func)
    if isinstance(node, ast.Subscript):
        return _callee_name(node.value)
    return ""


class _SiteVisitor(ast.NodeVisitor):
    """Collect both scans in one walk, tracking the enclosing function."""

    def __init__(self, module: str) -> None:
        self.module = module
        self.stack: list[str] = []
        self.seams: list[Site] = []
        self.composers: list[Site] = []

    def _enter(self, node: ast.AST) -> None:
        self.stack.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _enter  # noqa: N815
    visit_AsyncFunctionDef = _enter  # noqa: N815

    @property
    def _where(self) -> str:
        return self.stack[-1] if self.stack else "<module>"

    def visit_Call(self, node: ast.Call) -> None:
        # Scan A — write seams.
        if isinstance(node.func, ast.Attribute) and node.func.attr == "put":
            receiver = _callee_name(node.func.value)
            lowered = receiver.lower()
            # A wide net on purpose. This catches `parameter_store`,
            # `advisory_store` and the in-memory test doubles as well as the
            # document stores; they are *classified* below, never filtered,
            # because a name filter is how a producer hides.
            if "store" in lowered or "doc" in lowered:
                self.seams.append(
                    Site(self.module, self._where, f"{receiver}.put", node.lineno)
                )
        elif _callee_name(node.func) == "build_vector_row":
            self.seams.append(
                Site(self.module, self._where, "build_vector_row", node.lineno)
            )

        # Scan B — metadata composed inline at a keyword. This is the half
        # that sees a producer reaching its row through a governed command
        # instead of a `.put(`.
        for keyword in node.keywords:
            if keyword.arg in ("metadata", "meta") and isinstance(
                keyword.value, ast.Call
            ):
                label = f"metadata={_callee_name(keyword.value.func)}()"
                self.composers.append(
                    Site(self.module, self._where, label, keyword.value.lineno)
                )

        self.generic_visit(node)


def _scan_tree(root: Path) -> tuple[list[Site], list[Site]]:
    """Run both scans over every ``.py`` file under *root*."""
    seams: list[Site] = []
    composers: list[Site] = []
    for path in sorted(root.rglob("*.py")):
        visitor = _SiteVisitor(path.relative_to(root).as_posix())
        visitor.visit(ast.parse(path.read_text(), filename=str(path)))
        seams.extend(visitor.seams)
        composers.extend(visitor.composers)
    return seams, composers


def scan_write_seams() -> list[Site]:
    """Every store-ish ``.put`` and every ``build_vector_row`` call in ``src/``."""
    return _scan_tree(SRC_ROOT)[0]


def scan_metadata_composers() -> list[Site]:
    """Every ``metadata=<call>`` / ``meta=<call>`` keyword in ``src/``."""
    return _scan_tree(SRC_ROOT)[1]


def all_sites() -> list[Site]:
    seams, composers = _scan_tree(SRC_ROOT)
    return seams + composers


def unrostered(sites: list[Site], roster: dict[str, Rostered]) -> list[str]:
    """Keys the scan found that the roster does not classify."""
    return sorted({site.key for site in sites} - set(roster))


def sites_by_disposition(sites: list[Site]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for site in sites:
        entry = ROSTER.get(site.key)
        if entry is not None:
            counts[entry.disposition] += 1
    return counts


# --------------------------------------------------------------------------
# The roster holds
# --------------------------------------------------------------------------


class TestTheRosterIsComplete:
    def test_both_scans_clear_their_floors(self) -> None:
        """A scan that merely shrinks must fail, not pass more easily."""
        seams = scan_write_seams()
        composers = scan_metadata_composers()
        assert len(seams) >= MIN_WRITE_SEAMS, (
            f"write-seam scan collapsed to {len(seams)} sites; every check "
            f"below divides by this population"
        )
        assert len(composers) >= MIN_METADATA_COMPOSERS, (
            f"metadata-composer scan collapsed to {len(composers)} sites"
        )

    def test_the_hand_count_is_the_one_floor_a_scan_cannot_compute(self) -> None:
        """Asserted by hand, so an empty scan cannot satisfy it by division."""
        assert len(ROSTER) == EXPECTED_ROSTER_KEYS
        assert sum(entry.count for entry in ROSTER.values()) == EXPECTED_ROSTER_SITES

    def test_every_scanned_site_is_classified(self) -> None:
        """A new producer must be rostered, not silently inherit a default."""
        missing = unrostered(all_sites(), ROSTER)
        assert not missing, (
            "unclassified write seam(s) — decide whether each is a derived row "
            "carrying a source clock, and add it to ROSTER:\n  " + "\n  ".join(missing)
        )

    def test_every_roster_entry_still_matches_a_scanned_site(self) -> None:
        """The reverse direction: a stale entry is a lie about coverage."""
        found = {site.key for site in all_sites()}
        stale = sorted(set(ROSTER) - found)
        assert not stale, (
            "roster entries with no matching site — the code moved and the "
            "roster did not:\n  " + "\n  ".join(stale)
        )

    def test_site_counts_match_the_roster(self) -> None:
        """Counted, not named.

        Three ``advisory_store.put`` calls in one function are three claims,
        not one. A fourth appearing under the same key is a site nobody
        classified, and a key equality alone would not see it.
        """
        actual = Counter(site.key for site in all_sites())
        drifted = {
            key: (entry.count, actual[key])
            for key, entry in ROSTER.items()
            if actual[key] != entry.count
        }
        assert not drifted, f"site count changed under a roster key: {drifted}"

    def test_each_disposition_clears_its_floor(self) -> None:
        counts = sites_by_disposition(all_sites())
        for disposition, floor in DISPOSITION_SITE_FLOOR.items():
            assert counts[disposition] >= floor, (
                f"{disposition} fell to {counts[disposition]} sites (floor {floor})"
            )

    def test_declined_and_clockless_entries_carry_a_reason(self) -> None:
        """A disposition that removes information must say why.

        The two dispositions that *look* like an oversight are the two that
        have to argue for themselves; the others are legible from the name.
        """
        for key, entry in ROSTER.items():
            if entry.disposition in (DERIVED_CLOCK_DECLINED, DERIVED_NO_SOURCE_CLOCK):
                assert len(entry.reason.strip()) > 80, (
                    f"{key} is {entry.disposition} with no substantive reason"
                )


class TestTheScanCatchesANewProducer:
    """The vacuity proof: a synthetic tree through the *shipped* scanners.

    Asserting today's roster matches today's scan proves nothing about what
    happens when a producer is added. This adds one and checks both halves —
    that the scan sees it, and that the roster comparison reports it.
    """

    SYNTHETIC = '''
"""A module nobody has rostered."""

def compose_row_metadata(source):
    return {"title": source.title}


def write_derived_row(doc_store, source, executor):
    doc_store.put("derived:1", source.body, metadata={"title": source.title})
    executor.execute(build(metadata=compose_row_metadata(source)))
'''

    @pytest.fixture
    def tree(self, tmp_path: Path) -> Path:
        (tmp_path / "newpkg").mkdir()
        (tmp_path / "newpkg" / "producer.py").write_text(self.SYNTHETIC)
        return tmp_path

    def test_both_scans_find_the_new_producer(self, tree: Path) -> None:
        seams, composers = _scan_tree(tree)
        assert [site.key for site in seams] == [
            "newpkg/producer.py::write_derived_row::doc_store.put"
        ]
        assert [site.key for site in composers] == [
            "newpkg/producer.py::write_derived_row::metadata=compose_row_metadata()"
        ]

    def test_the_roster_comparison_reports_it(self, tree: Path) -> None:
        seams, composers = _scan_tree(tree)
        found = seams + composers
        # The proof cannot itself go vacuous: the synthetic keys must be
        # genuinely absent from the real roster before their absence means
        # anything.
        assert not ({site.key for site in found} & set(ROSTER))
        assert len(unrostered(found, ROSTER)) == 2


class TestInPlaceReputsActuallyPreserveTheClock:
    """``IN_PLACE_REPUT`` is a claim about an argument, so read the argument.

    The disposition says "this write does not move the row's clock". That is
    true only while the call passes ``preserve_updated_at=True``, and a
    re-put that quietly stopped passing it would still read as a correct
    roster entry while becoming a silent write-clock bump — #406's shape.
    """

    def test_every_such_site_passes_the_literal(self) -> None:
        expected = {
            key for key, entry in ROSTER.items() if entry.disposition == IN_PLACE_REPUT
        }
        assert expected, "no IN_PLACE_REPUT entries — the check would be vacuous"

        preserving = {
            site.key for site in _preserve_updated_at_sites() if site.key in expected
        }
        assert preserving == expected, (
            "rostered as an in-place re-put but not passing "
            f"preserve_updated_at=True: {sorted(expected - preserving)}"
        )


def _preserve_updated_at_sites() -> list[Site]:
    """Write seams passing a literal ``preserve_updated_at=True``."""
    sites: list[Site] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        module = path.relative_to(SRC_ROOT).as_posix()
        tree = ast.parse(path.read_text(), filename=str(path))
        visitor = _SiteVisitor(module)
        visitor.visit(tree)
        keeping = {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and any(
                kw.arg == "preserve_updated_at"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords
            )
        }
        sites.extend(site for site in visitor.seams if site.lineno in keeping)
    return sites


# --------------------------------------------------------------------------
# The derived rows, run rather than declared
# --------------------------------------------------------------------------


def _embed(text: str) -> list[float]:
    """Deterministic 8-dim embedding — geometry is irrelevant here."""
    vector = [0.0] * 8
    for index, char in enumerate(text[:256]):
        vector[index % 8] += ord(char) % 7
    norm = sum(value * value for value in vector) ** 0.5 or 1.0
    return [value / norm for value in vector]


class TestDerivedRowsPropagateTheSourceClock:
    """The ``DERIVED_PROPAGATES`` half, exercised.

    A roster entry naming a propagation that does not happen is exactly the
    failure #461 found one layer over: a declared accept event the tool never
    emits. So the two producers are called, and the bag is read.
    """

    def test_build_vector_row_carries_the_documents_source_clock(self) -> None:
        row = build_vector_row(
            "doc-1",
            "body text",
            {"created_at": "2024-02-03T10:00:00+00:00"},
            _embed,
        )
        assert row["metadata"]["created_at"] == "2024-02-03T10:00:00+00:00"

    def test_the_bags_clock_outranks_the_row_clock_argument(self) -> None:
        """``setdefault``, and the precedence is the point.

        The argument is only ever the row's write clock; a stamp already in
        the bag is the *source's*. The propagation lives in the callee, which
        is why all three ``build_vector_row`` call sites inherit it.
        """
        row = build_vector_row(
            "doc-1",
            "body text",
            {"created_at": "2024-02-03T10:00:00+00:00"},
            _embed,
            created_at="2026-09-12T00:00:00+00:00",
        )
        assert row["metadata"]["created_at"] == "2024-02-03T10:00:00+00:00"

    def test_a_bag_with_no_clock_falls_back_to_the_row_clock(self) -> None:
        row = build_vector_row(
            "doc-1", "body text", {}, _embed, created_at="2026-09-12T00:00:00+00:00"
        )
        assert row["metadata"]["created_at"] == "2026-09-12T00:00:00+00:00"

    def test_trace_summary_metadata_carries_the_traces_clock(self) -> None:
        """#463's fixed producer.

        Latent — zero production rows — but the worker is a backfill by
        design, so its first run over an existing trace store would stamp a
        whole history with one import instant.
        """
        trace = Trace(
            trace_id="trace-abc",
            source=TraceSource.AGENT,
            intent="rebuild the index",
            context=TraceContext(domain="trellis"),
            created_at=datetime(2024, 2, 3, 10, 0, tzinfo=UTC),
        )
        metadata = build_trace_metadata(trace)
        assert metadata["created_at"] == "2024-02-03T10:00:00+00:00"

    def test_the_trace_clock_survives_the_document_metadata_seam(self) -> None:
        """``DocumentMetadata`` is the seam the stamp has to cross.

        ``created_at`` is not a core field, so it lands in ``custom`` and
        re-flattens verbatim as a top-level key — which is the key
        ``resolve_recency_stamp`` reads. A seam that started nesting it would
        break the propagation while leaving the producer's own code correct.
        """
        trace = Trace(
            trace_id="trace-abc",
            source=TraceSource.AGENT,
            intent="rebuild the index",
            context=TraceContext(domain="trellis"),
            created_at=datetime(2024, 2, 3, 10, 0, tzinfo=UTC),
        )
        metadata = build_trace_metadata(trace)
        assert "created_at" in metadata, "the stamp did not survive to top level"
        assert isinstance(metadata["created_at"], str)


@pytest.fixture
def registry(tmp_path: Path) -> MagicMock:
    reg = MagicMock()
    reg.knowledge.document_store = SQLiteDocumentStore(tmp_path / "docs.db")
    reg.knowledge.vector_store = SQLiteVectorStore(tmp_path / "vectors.db")
    reg.operational.event_log = SQLiteEventLog(tmp_path / "events.db")
    reg.embedding_fn = _embed
    return reg


@pytest.fixture
def stamped_vault(tmp_path: Path) -> Path:
    """A long note whose frontmatter carries a source clock.

    The production shape: the markdown handler passes frontmatter through
    flat and ``created_at`` is not a reserved key, so the parent row's bag
    carries the *source's* clock while the column carries the sync's.
    """
    root = tmp_path / "vault"
    root.mkdir()
    body = "\n\n".join(
        f"## Section {index}\n\n" + ("Paragraph text about the subject. " * 60).strip()
        for index in range(4)
    )
    (root / "note.md").write_text(
        '---\ntitle: Stamped Note\ncreated_at: "2024-02-03T10:00:00+00:00"\n---\n\n'
        + body
        + "\n"
    )
    return root


class TestTheDeclinedDecisionIsPinned:
    """``DERIVED_CLOCK_DECLINED``, asserted as behaviour and not as prose.

    The decision is that a chunk row carries **no** source clock, so one
    document has two ages. That is a cost, it was measured, and it was taken
    deliberately — so it is pinned here rather than left to drift. A later
    agent who adds the one key to ``_write_chunks`` fails this test, and the
    failure names the comment carrying the measurement.
    """

    def test_the_parent_carries_the_source_clock(
        self, registry: MagicMock, stamped_vault: Path
    ) -> None:
        """Guard on the fixture: without this the next assertion is vacuous."""
        sync_corpus(registry, stamped_vault, source_system="obsidian")
        parent = registry.knowledge.document_store.get(
            corpus_doc_id("obsidian", "note.md")
        )
        assert parent is not None
        assert parent["metadata"]["created_at"] == "2024-02-03T10:00:00+00:00"

    def test_chunk_rows_carry_no_source_clock(
        self, registry: MagicMock, stamped_vault: Path
    ) -> None:
        report = sync_corpus(registry, stamped_vault, source_system="obsidian")
        chunks = report.counts()["chunks_written"]
        assert chunks > 1, "fixture produced no chunks to check"

        parent_id = corpus_doc_id("obsidian", "note.md")
        store = registry.knowledge.document_store
        for index in range(chunks):
            chunk = store.get(chunk_doc_id(parent_id, index))
            assert chunk is not None
            carried = sorted(key for key in CLOCK_KEYS if key in chunk["metadata"])
            assert not carried, (
                f"chunk {index} carries {carried} — #463 declined propagating "
                "the parent's clock to chunk rows, on measurement. If that "
                "decision is being reversed, the evidence to overturn is in "
                "the NO SOURCE CLOCK comment in ingest_corpus/sync.py::"
                "_write_chunks and in docs/design/decision-ledger.md, not here."
            )
