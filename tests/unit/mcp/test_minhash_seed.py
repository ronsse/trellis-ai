"""The MCP fuzzy-dedup index seed (#402).

``_get_minhash_index`` used to seed itself with ``document_store.search("")``,
which returns ``[]`` on every backend by explicit early return — a pinned
store-contract property. So the seed loop never executed, the index only ever
held documents written by the same process, and ``save_memory``'s stage-2
fuzzy rejection was dead across process boundaries. No test caught it because
every unit test's ``tmp_path`` store is empty, where a seed that reads nothing
is indistinguishable from a seed that works.

Two things are pinned here, and the second matters as much as the first.

**The seed reads rows.** Against a stocked store the index is non-empty, it
excludes chunk rows, it honours its bound, and a memory that near-duplicates a
*stored* document — one this process never wrote — is rejected while a merely
similar one is stored. A test that only asserted "does not crash" would have
passed against the broken seed, and did.

**Seeding stays off unless asked for.** Repairing the seed switches on a
rejection path that has never run, on every deployment at once. Measured on
the reference deployment (1,475 rows, 735 whole documents, 8 weeks), a
complete seed would have rejected 13 of those 735 writes — 1.8%, every one a
genuine near-duplicate — but costs ~24 s of blocking CPU on the first
``save_memory`` of a process, growing with the corpus, once per session under
``stdio``. So ``TRELLIS_MINHASH_SEED_MAX_DOCS`` defaults to 0 and the
default is pinned behaviourally, not just as a config literal.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.core.write_config import MINHASH_SEED_MAX_DOCS_ENV
from trellis.stores.registry import StoreRegistry

save_memory = unwrap_tool(server_mod.save_memory)

#: A stored memory long enough to clear the index's entropy filter.
STORED = (
    "The pgvector contract fixture called _conn as an attribute, which it "
    "stopped being when connection pooling landed, so the suite errored "
    "instead of running and nobody noticed for months."
)

#: One transposed character. Exactly what fuzzy dedup exists to catch — the
#: content hash differs, so stage 1 lets it through.
TYPO = STORED.replace("attribute", "atribute")

#: Same subject, same vocabulary, different claim. Shares topic words with
#: ``STORED`` and must still be stored: the threshold is what separates a
#: duplicate from a neighbour, and a test that only pinned the rejection
#: would pass with the threshold set to zero.
NEIGHBOUR = (
    "The pgvector contract fixture needs a database with the vector "
    "extension already created, because register_vector runs as the pool's "
    "on_connect hook and every pooled connection fails without it."
)


@pytest.fixture
def _fresh_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``_get_minhash_index`` to rebuild inside the test."""
    monkeypatch.setattr(server_mod, "_minhash_index", None)


def _enable(monkeypatch: pytest.MonkeyPatch, max_docs: int = 500) -> None:
    monkeypatch.setenv(MINHASH_SEED_MAX_DOCS_ENV, str(max_docs))


@pytest.mark.usefixtures("_fresh_index")
class TestSeedReadsRows:
    def test_seed_loads_stored_documents(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The defect, stated as its own assertion.

        ``search("")`` returned no rows, so this was ``0`` on every
        deployment while every existing test still passed.
        """
        store = temp_registry.knowledge.document_store
        for i in range(3):
            store.put(f"doc-{i}", f"{STORED} Variation number {i} of the note.")
        _enable(monkeypatch)

        index = server_mod._get_minhash_index(temp_registry)

        assert index.size == 3

    def test_seed_excludes_chunk_rows(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Chunk fragments do not compete for the seed's budget.

        Not because a chunk would falsely match its parent — measured over
        the reference deployment's 740 chunk rows, parent-to-chunk Jaccard
        is median 0.294 and max 0.641, and zero pairs clear the 0.85
        threshold. Because a ``Fuzzy duplicate: <parent>#chunk-3`` verdict
        names a fragment when the whole document is in the index too, and
        because chunks are 56% of that corpus and the seed is O(rows).
        """
        store = temp_registry.knowledge.document_store
        store.put("parent", STORED)
        store.put("parent#chunk-0", STORED)
        _enable(monkeypatch)

        index = server_mod._get_minhash_index(temp_registry)

        assert index.size == 1
        assert index.find_duplicate(TYPO)[0] == "parent"

    def test_seed_honours_its_bound(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bound is the operator's cost ceiling, so it must bind."""
        store = temp_registry.knowledge.document_store
        for i in range(5):
            store.put(f"doc-{i}", f"{STORED} Variation number {i} of the note.")
        _enable(monkeypatch, max_docs=2)

        assert server_mod._get_minhash_index(temp_registry).size == 2

    def test_seed_pages_beyond_one_page(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A corpus larger than one page is fully seeded, not truncated.

        The page size is an implementation detail; that the walk continues
        past it is not. The predecessor's single ``limit=500`` read would
        have silently capped a larger store.
        """
        monkeypatch.setattr(server_mod, "_MINHASH_SEED_PAGE_SIZE", 2)
        store = temp_registry.knowledge.document_store
        for i in range(5):
            store.put(f"doc-{i}", f"{STORED} Variation number {i} of the note.")
        _enable(monkeypatch)

        assert server_mod._get_minhash_index(temp_registry).size == 5

    def test_seed_reports_its_size_and_cost_at_info(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """INFO, not DEBUG — nobody watches DEBUG, which is the whole story
        of how a seed that always read zero rows survived."""
        temp_registry.knowledge.document_store.put("doc-0", STORED)
        _enable(monkeypatch)
        recorded: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            server_mod.logger,
            "info",
            lambda event, **kw: recorded.append((event, kw)),
        )

        server_mod._get_minhash_index(temp_registry)

        (fields,) = [
            kw for event, kw in recorded if event == "minhash_index_initialized"
        ]
        assert fields["size"] == 1
        assert fields["rows_read"] == 1
        assert fields["max_docs"] == 500
        assert fields["seconds"] >= 0


@pytest.mark.usefixtures("_fresh_index")
class TestRejectionBehaviour:
    def test_seeded_index_rejects_a_near_duplicate_of_a_stored_document(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The guarantee the seed exists to deliver.

        The stored document was written straight to the store, so nothing
        in this process ever added it to the index — which is the situation
        a fresh MCP server is always in.
        """
        temp_registry.knowledge.document_store.put("prior", STORED)
        _enable(monkeypatch)

        result = save_memory(TYPO)

        assert result.startswith("Fuzzy duplicate")
        assert "prior" in result
        assert temp_registry.knowledge.document_store.count() == 1

    def test_a_merely_similar_memory_is_still_stored(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pins the threshold from the other side.

        Without this, a threshold of 0 would satisfy the rejection test
        above and quietly turn ``save_memory`` into a write-once surface.
        """
        temp_registry.knowledge.document_store.put("prior", STORED)
        _enable(monkeypatch)

        result = save_memory(NEIGHBOUR)

        assert result.startswith("Memory saved")
        assert temp_registry.knowledge.document_store.count() == 2

    def test_threshold_is_the_shipped_minhash_default(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The two documents above sit either side of 0.85, and the index
        the server builds uses that threshold. Pinned as a number so a
        change to :class:`MinHashIndex`'s default is a decision here too."""
        temp_registry.knowledge.document_store.put("prior", STORED)
        _enable(monkeypatch)

        index = server_mod._get_minhash_index(temp_registry)

        assert index.stats()["threshold"] == 0.85
        (_, similarity) = index.find_duplicate(TYPO)
        assert similarity >= 0.85
        assert index.find_duplicate(NEIGHBOUR) is None

    def test_default_posture_stores_a_near_duplicate_of_a_stored_document(
        self,
        temp_registry: StoreRegistry,
    ) -> None:
        """Default-off, pinned behaviourally rather than as a literal.

        This is the behaviour change the flag withholds: with no
        environment set, the same call that the seeded test above rejects
        is accepted, exactly as it is on every deployment today.
        """
        temp_registry.knowledge.document_store.put("prior", STORED)

        result = save_memory(TYPO)

        assert result.startswith("Memory saved")
        assert temp_registry.knowledge.document_store.count() == 2

    def test_same_process_writes_are_deduped_with_or_without_the_seed(
        self,
        temp_registry: StoreRegistry,
    ) -> None:
        """The half that always worked keeps working, unseeded.

        ``save_memory`` adds every stored memory to the index, so a repeat
        inside one process is caught with no seed at all. The flag governs
        reach across process boundaries, nothing else.
        """
        assert save_memory(STORED).startswith("Memory saved")

        assert save_memory(TYPO).startswith("Fuzzy duplicate")


@pytest.mark.usefixtures("_fresh_index")
class TestSeedFailureIsVisible:
    def test_rows_read_but_nothing_indexed_warns_as_a_defect(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A seed that reads rows and compares none is the #402 shape.

        Kept as its own event, separate from the off-by-default posture:
        "never switched on" and "switched on and reaching nothing" want
        completely different fixes, and one shared warning would let the
        second hide behind the first. Here the rows are below the entropy
        filter, which is the honest in-process way to produce the state.
        """
        store = temp_registry.knowledge.document_store
        store.put("tiny-a", "ab")
        store.put("tiny-b", "cd")
        _enable(monkeypatch)
        recorded: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            server_mod.logger,
            "warning",
            lambda event, **kw: recorded.append((event, kw)),
        )

        index = server_mod._get_minhash_index(temp_registry)

        assert index.size == 0
        (fields,) = [
            kw for event, kw in recorded if event == "minhash_index_seed_empty"
        ]
        assert fields["rows_read"] == 2
        assert fields["stored_documents"] == 2
        assert fields["issue"] == 402

    def test_a_seeded_store_warns_about_nothing(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Neither warning fires once the index actually holds the corpus —
        a warning that survives its own fix is noise."""
        temp_registry.knowledge.document_store.put("prior", STORED)
        _enable(monkeypatch)
        recorded: list[str] = []
        monkeypatch.setattr(
            server_mod.logger,
            "warning",
            lambda event, **kw: recorded.append(event),
        )

        server_mod._get_minhash_index(temp_registry)

        assert recorded == []

    def test_a_stalled_walk_stops_and_says_so(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pagination is ``offset`` over ``created_at DESC`` with no
        tiebreak on either shipped backend, so a page can repeat rows the
        walk has already seen. Stopping is the safe choice — the
        alternative re-reads forever — but it under-seeds, and an
        under-seeded index rejects less than the operator asked for. So
        the stop is a warning, not a shrug.
        """
        page = [{"doc_id": "same", "content": STORED}]
        monkeypatch.setattr(server_mod, "_MINHASH_SEED_PAGE_SIZE", 1)
        monkeypatch.setattr(
            temp_registry.knowledge.document_store,
            "list_documents",
            lambda **_: list(page),
        )
        _enable(monkeypatch, max_docs=10)
        recorded: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            server_mod.logger,
            "warning",
            lambda event, **kw: recorded.append((event, kw)),
        )

        index = server_mod._get_minhash_index(temp_registry)

        assert index.size == 1
        (fields,) = [
            kw for event, kw in recorded if event == "minhash_seed_walk_stalled"
        ]
        assert fields["indexed"] == 1


@pytest.mark.usefixtures("_fresh_index")
class TestAFailedSeedIsNotCached:
    """A seed that dies mid-walk must not leave a partial index behind.

    ``_get_minhash_index``'s docstring promises that a broken dedup path is
    *raised* rather than silently disabled, because silent disable means
    memories are stored without the fuzzy dedup the operator asked for and the
    duplicates it would have caught are invisible.

    Publishing the index to the module global before seeding it kept that
    promise for exactly one call: the raise fired once, and every call after it
    took the ``if _minhash_index is not None`` early return and got an index
    holding whatever prefix of the corpus the walk had reached. #402's repair
    is what made this reachable — the old ``search("")`` seed could not fail,
    so nothing had to survive a failure.
    """

    @staticmethod
    def _fail_after_the_first_page(
        store: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First page reads, every later page raises — on every attempt.

        Keyed on ``offset`` rather than on a call counter so a *retry* fails
        the same way the first attempt did; a counter would let the second
        call succeed for the wrong reason and hide what these tests assert.
        """
        real = store.list_documents

        def flaky(**kwargs: Any) -> list[dict[str, Any]]:
            if kwargs.get("offset", 0) > 0:
                msg = "transient store failure"
                raise RuntimeError(msg)
            return list(real(**kwargs))

        monkeypatch.setattr(store, "list_documents", flaky)

    def test_a_seed_failure_leaves_no_index_behind(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = temp_registry.knowledge.document_store
        for i in range(3):
            store.put(f"doc-{i}", f"{STORED} Variation number {i} of the note.")
        monkeypatch.setattr(server_mod, "_MINHASH_SEED_PAGE_SIZE", 2)
        _enable(monkeypatch, max_docs=10)
        self._fail_after_the_first_page(store, monkeypatch)

        with pytest.raises(McpError):
            server_mod._get_minhash_index(temp_registry)

        assert server_mod._minhash_index is None

    def test_the_call_after_a_seed_failure_raises_too(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The second call is the one the old code got wrong.

        It returned a silently under-seeded index instead of re-raising, so a
        deployment that hit one transient store error at boot ran the rest of
        the process with fuzzy dedup covering an arbitrary prefix of the
        corpus and said nothing.
        """
        store = temp_registry.knowledge.document_store
        for i in range(3):
            store.put(f"doc-{i}", f"{STORED} Variation number {i} of the note.")
        monkeypatch.setattr(server_mod, "_MINHASH_SEED_PAGE_SIZE", 2)
        _enable(monkeypatch, max_docs=10)
        self._fail_after_the_first_page(store, monkeypatch)

        with pytest.raises(McpError):
            server_mod._get_minhash_index(temp_registry)
        with pytest.raises(McpError):
            server_mod._get_minhash_index(temp_registry)

    def test_a_later_call_seeds_completely_once_the_store_recovers(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing cached means the retry is a real retry, not a repeat."""
        store = temp_registry.knowledge.document_store
        for i in range(3):
            store.put(f"doc-{i}", f"{STORED} Variation number {i} of the note.")
        monkeypatch.setattr(server_mod, "_MINHASH_SEED_PAGE_SIZE", 2)
        _enable(monkeypatch, max_docs=10)
        real = store.list_documents
        self._fail_after_the_first_page(store, monkeypatch)

        with pytest.raises(McpError):
            server_mod._get_minhash_index(temp_registry)
        monkeypatch.setattr(store, "list_documents", real)

        assert server_mod._get_minhash_index(temp_registry).size == 3
