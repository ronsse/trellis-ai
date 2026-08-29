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

Both halves are tested. The first is structural — scanning the two
packages whose job is handing document rows to a caller
(:mod:`trellis_api.routes`, :mod:`trellis_cli`) and requiring every
``search`` / ``list_documents`` call in them to name ``include_chunks``,
so a new surface cannot inherit a default nobody chose. The second is
behavioural — :class:`~trellis.retrieve.strategies.KeywordSearch` must
still return chunk rows.

``trellis.mcp`` is out of scope on purpose and not by exception: it hands
back *packs*, not rows. Its single ``DocumentStore.search`` call is the
MinHash index seed, which reads zero rows on every backend (#402) and is
decided in a comment at the call site.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from trellis.retrieve.strategies import KeywordSearch
from trellis.stores.sqlite.document import SQLiteDocumentStore

#: Packages that hand document rows to a caller. Directory names, not a
#: list of functions — "is this a surface?" is a judgement call and a
#: judgement call is what rotted the roster.
_ROW_SURFACE_PACKAGES = ("trellis_api/routes", "trellis_cli")

#: The two ``DocumentStore`` reads that can serve a chunk row. ``count``
#: is excluded: it returns a number, not rows, and the ABC already binds
#: it to whichever ``list_documents`` call it is reported beside.
_ROW_READS = frozenset({"search", "list_documents"})

#: ``.search()`` is also ``re.Pattern.search``. A ``DocumentStore.search``
#: call is distinguished by naming one of these keyword-only parameters
#: (``re``'s ``search`` takes none) or by reading off something called a
#: store. Both conditions are checked — a call matching either is held to
#: the rule.
_STORE_SEARCH_KWARGS = frozenset({"limit", "filters", "include_chunks", "query"})


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src"
    assert root.is_dir(), f"src/ not found at {root}"
    return root


def _is_document_store_read(node: ast.Call) -> bool:
    """Does *node* look like a ``DocumentStore`` row read?"""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _ROW_READS:
        return False
    if func.attr == "list_documents":
        return True
    names = {kw.arg for kw in node.keywords if kw.arg}
    if names & _STORE_SEARCH_KWARGS:
        return True
    return "store" in ast.unparse(func.value)


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
