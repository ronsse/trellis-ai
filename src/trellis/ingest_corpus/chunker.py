"""Deterministic paragraph-aware chunking for long documents.

Documents at or under :data:`CHUNK_THRESHOLD_CHARS` are stored whole and
embedded whole — chunking only kicks in past the embedder input cap,
where content would otherwise be semantically unretrievable (ADR §3).

The split is a pure function of the content string: greedy paragraph
packing toward :data:`CHUNK_TARGET_CHARS`, closing a chunk before it
would exceed the target, with a paragraph that alone exceeds
:data:`CHUNK_MAX_CHARS` hard-split at character granularity. Each chunk
after the first is prefixed with up to :data:`CHUNK_OVERLAP_CHARS` of
the preceding text so a sentence cut at a boundary stays retrievable
from both sides. Re-chunking unchanged content therefore yields
byte-identical spans — the property idempotent re-sync relies on.
"""

from __future__ import annotations

import re

from trellis.ingest_corpus.models import ChunkSpan
from trellis.retrieve.embed_ingest_hook import EMBED_INPUT_CHAR_CAP

#: Content length above which a document is chunked. Equal to the embed
#: hook's input cap: anything longer would be silently truncated at
#: embedding time, which is exactly the failure chunking exists to fix.
CHUNK_THRESHOLD_CHARS = EMBED_INPUT_CHAR_CAP

#: Greedy packing target per chunk (ADR §3: "~2-4k chars").
CHUNK_TARGET_CHARS = 3000

#: Hard cap for a single paragraph before character-level splitting.
CHUNK_MAX_CHARS = 6000

#: Overlap prefix carried into each chunk after the first.
CHUNK_OVERLAP_CHARS = 200

#: A paragraph boundary: one or more blank (or whitespace-only) lines.
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n+")

# Every produced chunk is at most CHUNK_MAX_CHARS + CHUNK_OVERLAP_CHARS
# characters — comfortably under the embed cap by construction.
assert CHUNK_MAX_CHARS + CHUNK_OVERLAP_CHARS < EMBED_INPUT_CHAR_CAP


def _paragraph_spans(content: str) -> list[tuple[int, int]]:
    """Spans of paragraph blocks (text between blank-line breaks)."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for match in _PARAGRAPH_BREAK.finditer(content):
        if match.start() > pos:
            spans.append((pos, match.start()))
        pos = match.end()
    if pos < len(content):
        spans.append((pos, len(content)))
    return spans


def _split_oversize(start: int, end: int) -> list[tuple[int, int]]:
    """Hard-split an oversize paragraph into CHUNK_MAX_CHARS windows."""
    return [
        (pos, min(pos + CHUNK_MAX_CHARS, end))
        for pos in range(start, end, CHUNK_MAX_CHARS)
    ]


def chunk_spans(content: str) -> list[ChunkSpan]:
    """Chunk *content* into embeddable spans; ``[]`` when short enough.

    Deterministic: equal input strings produce equal span lists.
    """
    if len(content) <= CHUNK_THRESHOLD_CHARS:
        return []

    # Pack paragraphs into core (overlap-free) spans.
    cores: list[tuple[int, int]] = []
    current: tuple[int, int] | None = None
    for para_start, para_end in _paragraph_spans(content):
        if para_end - para_start > CHUNK_MAX_CHARS:
            if current is not None:
                cores.append(current)
                current = None
            cores.extend(_split_oversize(para_start, para_end))
            continue
        if current is None:
            current = (para_start, para_end)
        elif para_end - current[0] <= CHUNK_TARGET_CHARS:
            current = (current[0], para_end)
        else:
            cores.append(current)
            current = (para_start, para_end)
    if current is not None:
        cores.append(current)

    return [
        ChunkSpan(
            index=i,
            start=start if i == 0 else max(0, start - CHUNK_OVERLAP_CHARS),
            end=end,
        )
        for i, (start, end) in enumerate(cores)
    ]
