"""Tests for MinHash/LSH fuzzy duplicate detection."""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from trellis.classify.dedup.minhash import MinHashIndex, _char_shingles


class TestCharShingles:
    def test_normal_text(self):
        shingles = _char_shingles("hello world", k=3)
        assert "hel" in shingles
        assert "ell" in shingles
        assert "orl" in shingles

    def test_short_text(self):
        shingles = _char_shingles("ab", k=3)
        assert shingles == {"ab"}

    def test_empty_text(self):
        assert _char_shingles("") == set()

    def test_case_normalisation(self):
        assert _char_shingles("ABC", k=3) == _char_shingles("abc", k=3)


class TestMinHashIndex:
    def test_init_validates_perm_bands(self):
        with pytest.raises(ValueError, match="divisible"):
            MinHashIndex(num_perm=100, num_bands=7)

    def test_add_and_query_exact_duplicate(self):
        index = MinHashIndex(threshold=0.8)
        index.add("doc1", "The quick brown fox jumps over the lazy dog today")
        matches = index.query("The quick brown fox jumps over the lazy dog today")
        assert len(matches) == 1
        assert matches[0][0] == "doc1"
        assert matches[0][1] >= 0.99  # near-perfect match

    def test_fuzzy_duplicate_detected(self):
        """Minor casing/punctuation variations should be caught."""
        index = MinHashIndex(threshold=0.7)
        original = (
            "The quick brown fox jumps over the lazy dog"
            " in the park today and every day"
        )
        index.add("doc1", original)
        # Same content with minor punctuation/casing changes
        matches = index.query(
            "the quick brown fox jumps over the lazy dog"
            " in the park today and every day!"
        )
        assert len(matches) >= 1
        assert matches[0][0] == "doc1"

    def test_different_content_no_match(self):
        index = MinHashIndex(threshold=0.8)
        index.add("doc1", "The quick brown fox jumps over the lazy dog today here")
        matches = index.query(
            "Machine learning algorithms for natural language processing tasks"
        )
        assert len(matches) == 0

    def test_entropy_filter_skips_short_content(self):
        index = MinHashIndex(min_shingles=5)
        added = index.add("doc1", "hi")
        assert added is False
        assert index.size == 0

    def test_find_duplicate_returns_best_match(self):
        index = MinHashIndex(threshold=0.7)
        index.add("doc1", "The quick brown fox jumps over the lazy dog in the park")
        index.add(
            "doc2",
            "A completely different document about machine learning and AI models",
        )
        result = index.find_duplicate(
            "The quick brown fox jumps over the lazy dog in the park"
        )
        assert result is not None
        assert result[0] == "doc1"

    def test_find_duplicate_returns_none_when_no_match(self):
        index = MinHashIndex(threshold=0.9)
        index.add("doc1", "The quick brown fox jumps over the lazy dog in the park")
        result = index.find_duplicate(
            "Completely unrelated content about quantum physics and space"
        )
        assert result is None

    def test_remove_document(self):
        index = MinHashIndex(threshold=0.8)
        index.add("doc1", "The quick brown fox jumps over the lazy dog today here")
        assert index.size == 1
        assert index.remove("doc1") is True
        assert index.size == 0
        assert index.remove("doc1") is False  # already removed

    def test_remove_prevents_future_matches(self):
        index = MinHashIndex(threshold=0.8)
        index.add("doc1", "The quick brown fox jumps over the lazy dog today here")
        index.remove("doc1")
        matches = index.query("The quick brown fox jumps over the lazy dog today here")
        assert len(matches) == 0

    def test_exclude_ids(self):
        index = MinHashIndex(threshold=0.8)
        index.add("doc1", "The quick brown fox jumps over the lazy dog today here")
        matches = index.query(
            "The quick brown fox jumps over the lazy dog today here",
            exclude_ids={"doc1"},
        )
        assert len(matches) == 0

    def test_stats(self):
        index = MinHashIndex(num_perm=64, num_bands=8, threshold=0.85)
        index.add("doc1", "The quick brown fox jumps over the lazy dog in the park")
        stats = index.stats()
        assert stats["documents"] == 1
        assert stats["num_perm"] == 64
        assert stats["num_bands"] == 8
        assert stats["threshold"] == 0.85

    def test_multiple_documents(self):
        index = MinHashIndex(threshold=0.8)
        texts = [
            "The quick brown fox jumps over the lazy dog in the park",
            "A fast auburn fox leaps above the sleepy hound in the yard",
            "Machine learning models for natural language processing tasks",
            "Deep neural networks used in computer vision applications today",
        ]
        for i, text in enumerate(texts):
            index.add(f"doc{i}", text)
        assert index.size == 4

    def test_casing_variation_detected(self):
        """Same content in different cases should be a fuzzy match."""
        index = MinHashIndex(threshold=0.8)
        index.add("doc1", "The Quick Brown Fox Jumps Over The Lazy Dog Today Here")
        matches = index.query("the quick brown fox jumps over the lazy dog today here")
        assert len(matches) >= 1
        assert matches[0][1] >= 0.95


@pytest.fixture
def _preempt_aggressively():
    """Force the interpreter to switch threads often.

    The races below live in two-bytecode check-then-act windows. At the
    default 5ms switch interval they almost never interleave, so a test
    without this passes against the unlocked implementation and proves
    nothing.
    """
    original = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(original)


@pytest.mark.usefixtures("_preempt_aggressively")
class TestThreadSafety:
    """A single index is shared across the worker threads of an
    ``http``-transport MCP server, where concurrent ``save_memory``
    calls interleave ``add()``, ``query()`` and ``remove()``.

    Only :meth:`~MinHashIndex.stats` iterates shared mutable state at the
    Python level, so it is the only method that raises today. The rest of
    the index's check-then-act sequences are racy by construction but are
    de facto serialised by the GIL — they are not de facto safe, and a
    free-threaded interpreter removes that accident. The lock covers all
    of them; only the ``stats`` test below fails against the unlocked
    implementation.
    """

    def test_concurrent_add_of_colliding_content_keeps_every_doc(self):
        """Concurrent adds of identical content must all reach the index.

        Every document hashes to the same LSH bucket, so all threads race
        the ``if band_hash not in bucket: bucket[band_hash] = set()``
        check-then-act in ``add()``. ``num_bands=1`` removes the safety
        net where ``query`` unions candidates across bands and recovers a
        doc dropped from one of them.

        This is an invariant guard, not a regression test: under the GIL
        the two-bytecode window never opens wide enough to lose a doc, so
        it passes against the unlocked implementation too. It exists to
        catch a future refactor (or a free-threaded runtime) that widens
        the window.
        """
        index = MinHashIndex(num_perm=32, num_bands=1, threshold=0.8)
        n_docs = 64
        content = "the quick brown fox jumps over the lazy dog in the park today"
        barrier = threading.Barrier(n_docs)

        def add_doc(i: int) -> None:
            barrier.wait()
            index.add(f"doc{i}", content)

        with ThreadPoolExecutor(max_workers=n_docs) as pool:
            list(pool.map(add_doc, range(n_docs)))

        assert index.size == n_docs
        matches = index.query(content)
        assert len(matches) == n_docs, (
            f"{n_docs - len(matches)} doc(s) dropped from the LSH bucket"
        )

    def test_concurrent_remove_and_stats(self):
        """``stats()`` must not iterate a band dict that ``remove()`` mutates.

        This is the real regression test. Unlocked,
        ``sum(1 for s in band.values() ...)`` is a Python-level generator
        over a dict view that yields between items, so a concurrent
        ``del self._buckets[band_idx][band_hash]`` raises
        ``RuntimeError: dictionary changed size during iteration`` —
        which would surface as an ``INTERNAL_ERROR`` mid-tool-call.
        Reproduces on every run against the unlocked implementation.
        """
        index = MinHashIndex(threshold=0.8)
        n_docs = 128
        for i in range(n_docs):
            index.add(f"seed{i}", f"machine learning models for language tasks {i}")

        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def remover() -> None:
            barrier.wait()
            for i in range(n_docs):
                index.remove(f"seed{i}")

        def reader() -> None:
            barrier.wait()
            try:
                for _ in range(n_docs):
                    index.stats()
            except BaseException as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(remover), pool.submit(reader)]
            for f in futures:
                f.result()

        assert not errors, f"concurrent remove/stats raised: {errors[:3]}"
        assert index.size == 0
