"""The two size limits a vector row is built under.

Defined here, not in :mod:`trellis.retrieve.embed_ingest_hook`, because
:mod:`trellis.retrieve.reference` needs them and the hook imports that module.
The hook re-exports both names, so every existing import keeps working.
"""

from __future__ import annotations

__all__ = ["EMBED_INPUT_CHAR_CAP", "VECTOR_METADATA_EXCERPT_CHARS"]

#: Cap on the characters sent to the embedder. Embedding models have
#: finite context windows (≈8k tokens for common models); 8000 chars
#: (≈2k tokens) keeps every provider comfortably inside its window while
#: covering far more content than a pack excerpt ever renders.
EMBED_INPUT_CHAR_CAP = 8000

#: Cap on the content excerpt stored in vector metadata. Matches
#: :data:`~trellis.retrieve.excerpts.EXCERPT_MAX_CHARS`, which is what
#: ``SemanticSearch`` renders into ``PackItem.excerpt``; storing more
#: duplicates the document store to no benefit. The stored string is the
#: *truncator's* output, so it is already ``<=`` this many characters —
#: a boundary-aware, size-marked excerpt rather than a raw slice.
VECTOR_METADATA_EXCERPT_CHARS = 500
