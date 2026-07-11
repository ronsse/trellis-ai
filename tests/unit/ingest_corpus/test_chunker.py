"""Chunker determinism and span-shape tests."""

from __future__ import annotations

from itertools import pairwise

from trellis.ingest_corpus.chunker import (
    CHUNK_MAX_CHARS,
    CHUNK_OVERLAP_CHARS,
    CHUNK_TARGET_CHARS,
    CHUNK_THRESHOLD_CHARS,
    chunk_spans,
)
from trellis.retrieve.embed_ingest_hook import EMBED_INPUT_CHAR_CAP


def _long_document(paragraphs: int = 8, sentence_repeats: int = 40) -> str:
    para = "A sentence about systems and stores. " * sentence_repeats
    return "\n\n".join(f"## Section {i}\n\n{para.strip()}" for i in range(paragraphs))


class TestThreshold:
    def test_short_content_is_not_chunked(self):
        assert chunk_spans("short note") == []

    def test_content_at_cap_is_not_chunked(self):
        assert chunk_spans("x" * CHUNK_THRESHOLD_CHARS) == []

    def test_content_past_cap_is_chunked(self):
        assert len(chunk_spans("x" * (CHUNK_THRESHOLD_CHARS + 1))) >= 1


class TestDeterminism:
    def test_same_input_yields_identical_spans(self):
        content = _long_document()
        assert chunk_spans(content) == chunk_spans(content)

    def test_indices_are_sequential(self):
        spans = chunk_spans(_long_document())
        assert [s.index for s in spans] == list(range(len(spans)))


class TestSpanShape:
    def test_every_chunk_is_under_the_embed_cap(self):
        for content in (_long_document(), "y" * 50_000):
            for span in chunk_spans(content):
                assert span.end - span.start <= CHUNK_MAX_CHARS + CHUNK_OVERLAP_CHARS
                assert span.end - span.start < EMBED_INPUT_CHAR_CAP

    def test_spans_cover_all_paragraph_content(self):
        content = _long_document()
        spans = chunk_spans(content)
        covered = set()
        for span in spans:
            covered.update(range(span.start, span.end))
        # Every non-whitespace character of the parent appears in a chunk.
        missing = [
            i for i, ch in enumerate(content) if not ch.isspace() and i not in covered
        ]
        assert missing == []

    def test_spans_slice_reconstructs_chunk_content(self):
        content = _long_document()
        for span in chunk_spans(content):
            assert content[span.start : span.end]

    def test_chunks_after_first_carry_overlap_prefix(self):
        content = _long_document()
        spans = chunk_spans(content)
        assert len(spans) >= 2
        for prev, cur in pairwise(spans):
            assert cur.start < prev.end  # overlap into the previous chunk

    def test_multi_paragraph_chunks_respect_target(self):
        content = _long_document(paragraphs=20, sentence_repeats=10)
        spans = chunk_spans(content)
        # Paragraphs here are ~400 chars, so every chunk is paragraph-packed
        # and must stay within target (+ overlap prefix).
        for span in spans:
            assert span.end - span.start <= CHUNK_TARGET_CHARS + CHUNK_OVERLAP_CHARS


class TestOversizeParagraph:
    def test_single_giant_paragraph_is_hard_split(self):
        content = "z" * 20_000  # no paragraph breaks at all
        spans = chunk_spans(content)
        assert len(spans) >= 3
        for span in spans:
            assert span.end - span.start <= CHUNK_MAX_CHARS + CHUNK_OVERLAP_CHARS
        assert spans[-1].end == len(content)
