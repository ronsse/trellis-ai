"""Enforcement for the chunk-visibility rule (#396).

:data:`trellis.ingest_corpus.models.CHUNK_ID_SEPARATOR` used to carry a
hand-maintained roster of which surfaces filter chunk rows and which do
not. The roster was wrong twice in three changes — first claiming
``GET /api/v1/documents`` filtered when it applied no filter at all
(#385), then enumerating only the ``list_documents`` callers and omitting
every ``DocumentStore.search`` caller, one of which was a second
unfiltered REST surface (#396). A snapshot of call sites goes stale on
the next caller; both times it was already stale in the commit that
wrote it.

So the comment states a rule and this module enforces it:

    A surface that hands back *whole document rows* excludes chunks by
    default. A surface that feeds the pack budget keeps them, because
    there the chunk is the retrievable unit and the excerpt is what the
    budget prices.

Both halves are tested, but not equally tightly, and the asymmetry is
deliberate. The pack half is enforced as stated — behaviourally,
:class:`~trellis.retrieve.strategies.KeywordSearch` must still return chunk
rows. The whole-row half is enforced one step short of the rule: the scan
requires every ``search`` / ``list_documents`` call it detects in
:mod:`trellis_api.routes` and :mod:`trellis_cli` to **name**
``include_chunks``, not to set it ``False``, because a walker living in
those packages that needs chunk rows is a correct caller. Requiring the
decision to be visible is the enforceable part; which way it goes is a
judgement a reviewer makes at the call site.

``trellis.mcp`` is out of scope on purpose and not by exception: it hands
back *packs*, not rows. Its one whole-row read is the fuzzy-dedup index
seed, which named ``include_chunks=False`` when #402 repaired it — that
call used to be a ``DocumentStore.search("")`` returning zero rows on
every backend, so the question the rule asks had no answer there.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import KeywordSearch
from trellis.stores.sqlite.document import SQLiteDocumentStore

#: Packages that hand document rows to a caller. Directory names, not a
#: list of functions — "is this a surface?" is a judgement call and a
#: judgement call is what rotted the roster.
_ROW_SURFACE_PACKAGES = ("trellis_api/routes", "trellis_cli")

#: The two ``DocumentStore`` reads that can serve a chunk row. ``count`` is
#: excluded because it returns a number rather than rows, so a page of
#: fragments is not the failure mode — but the exclusion is narrower than
#: it looks. The ABC binds ``count`` to whichever ``list_documents`` call it
#: is *reported beside*, and two of the four ``count`` sites in these
#: packages are not beside a listing at all: ``trellis admin stats`` and
#: ``GET /api/v1/admin/stats`` report the corpus total unfiltered. On the
#: reference deployment that is 1,319 against ``GET /api/v1/documents``'s
#: 579 — two operator surfaces disagreeing about how many documents exist,
#: which is #385's defect class in a different shape. Filed as #412 rather
#: than folded in: a store total arguably *should* be the store total, and
#: deciding that is not a chunk-filter change.
_ROW_READS = frozenset({"search", "list_documents"})


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src"
    assert root.is_dir(), f"src/ not found at {root}"
    return root


def _is_document_store_read(node: ast.Call) -> bool:
    """Does *node* look like a ``DocumentStore`` row read?

    Receiver name only. ``.search()`` is also ``re.Pattern.search``, so
    some discriminator is needed, and an earlier version also matched on
    keyword-only parameter names (``limit`` / ``filters`` / ``query``).
    That found the same call sites and carried a trap: ``SearchStrategy``
    has the same ``search(query, *, limit, filters)`` signature and no
    ``include_chunks``, so routing a command through a strategy — the
    obvious future fix for ``trellis retrieve pack``, which reaches past
    ``PackBuilder`` today — would have made this test demand a parameter
    that does not exist, and the repair would have been to weaken it.

    The cost of the narrower rule is that a document store reached through
    a receiver not named ``*store*`` slips the scan.
    :func:`test_the_scan_finds_the_call_sites_it_is_meant_to_police` is the
    guard against that class of drift.
    """
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _ROW_READS
        and "store" in ast.unparse(func.value)
    )


def _row_read_call_sites() -> list[tuple[Path, ast.Call]]:
    src = _src_root()
    sites: list[tuple[Path, ast.Call]] = []
    for package in _ROW_SURFACE_PACKAGES:
        package_dir = src / package
        assert package_dir.is_dir(), f"package not found: {package_dir}"
        for py_file in sorted(package_dir.rglob("*.py")):
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            sites.extend(
                (py_file, node)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and _is_document_store_read(node)
            )
    return sites


def test_the_scan_finds_the_call_sites_it_is_meant_to_police() -> None:
    """Guard against the enforcement quietly matching nothing.

    A structural test that stops finding call sites — because a helper
    was renamed, a package moved, or the detection heuristic drifted —
    passes for the wrong reason. This is the floor that makes the failure
    visible instead.
    """
    sites = _row_read_call_sites()
    assert len(sites) >= 6, (
        "chunk-visibility scan found only "
        f"{len(sites)} document-store row reads across {_ROW_SURFACE_PACKAGES}; "
        "the detection heuristic has probably stopped matching."
    )
    files = {p.name for p, _ in sites}
    assert {"explore.py", "retrieve.py"} <= files, (
        f"expected the known REST/CLI row surfaces in the scan; got {sorted(files)}"
    )


def test_row_surfaces_name_include_chunks_explicitly() -> None:
    """Every row read in a row-serving package decides chunk visibility.

    Not "excludes chunks" — a walker that needs chunk rows is a correct
    caller. The rule is that the decision is *made*, at the call site,
    where a reviewer sees it. Inheriting the store's ``include_chunks=True``
    default is how ``GET /api/v1/documents`` and ``GET /api/v1/search``
    both ended up serving a corpus that is 56% fragments without anyone
    choosing it.
    """
    offenders = [
        f"{path.relative_to(_src_root())}:{node.lineno} {ast.unparse(node.func)}(...)"
        for path, node in _row_read_call_sites()
        if "include_chunks" not in {kw.arg for kw in node.keywords if kw.arg}
    ]
    assert not offenders, (
        "DocumentStore row reads in a row-serving package must name "
        "`include_chunks` explicitly (see CHUNK_ID_SEPARATOR in "
        "trellis/ingest_corpus/models.py). Offenders:\n"
        + "\n".join(f"  {o}" for o in offenders)
    )


@pytest.fixture
def chunked_store(tmp_path: Path) -> SQLiteDocumentStore:
    """A parent document and one chunk sliced from it, both matching."""
    store = SQLiteDocumentStore(str(tmp_path / "docs.db"))
    store.put("corpus:notes:doc0", "retrieval budget prices the excerpt")
    store.put(
        "corpus:notes:doc0#chunk-0",
        "retrieval budget prices the excerpt",
        {"parent_doc_id": "corpus:notes:doc0", "chunk_index": 0},
    )
    return store


def test_keyword_retrieval_axis_still_returns_chunk_rows(
    chunked_store: SQLiteDocumentStore,
) -> None:
    """The other half of the rule, and the one with a cost if broken.

    ``KeywordSearch`` feeds ``PackBuilder``, where the excerpt is what the
    token budget prices and the chunk is the retrievable unit — a whole
    corpus document is often unaffordable at pack width. Filtering chunks
    here would read as cleanup and be a recall regression. Pinned so the
    tidy-up that follows every operator-surface fix cannot reach it.
    """
    items = KeywordSearch(chunked_store).search("retrieval budget", limit=10)
    ids = {item.item_id for item in items}
    assert "corpus:notes:doc0#chunk-0" in ids, (
        "KeywordSearch must keep serving chunk rows to the pack builder; "
        f"got {sorted(ids)}"
    )


def test_a_chunk_row_survives_pack_assembly(
    chunked_store: SQLiteDocumentStore,
) -> None:
    """The same guarantee one layer out, where it is likelier to be lost.

    The strategy-level test above cannot see the *collect seam* —
    ``_apply_collect_gates(strip_non_servable(strategy.search(...)))`` in
    :meth:`~trellis.retrieve.pack_builder.PackBuilder.build`. That seam is
    the house pattern for cross-cutting exclusions precisely because the
    strategy set is injected and open (#338 moved the noise rule there for
    that reason), so an ``exclude_chunks(...)`` added beside the archived
    and noise partitions would look like it was following convention, would
    cost the pack its retrievable unit, and would leave the strategy test
    green. Assert the property where a reader would actually break it.
    """
    pack = PackBuilder(strategies=[KeywordSearch(chunked_store)]).build(
        intent="retrieval budget"
    )

    ids = {item.item_id for item in pack.items}
    assert "corpus:notes:doc0#chunk-0" in ids, (
        "a chunk row must survive the PackBuilder collect seam; "
        f"pack served {sorted(ids)}"
    )
