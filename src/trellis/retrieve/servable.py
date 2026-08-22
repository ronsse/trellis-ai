"""The serving boundary — which stored metadata keys may reach an agent.

Search strategies build a :class:`~trellis.schemas.pack.PackItem` by splatting
the store's metadata mapping wholesale (``metadata={"source_strategy": ...,
**metadata}``). That is the right default — a strategy cannot know which keys a
consumer will find useful, and an allowlist would silently drop every key added
later. But it means *any* key written onto a document is served, and some keys
exist precisely because they must not be.

Today that is exactly one key. Shadow tags (#321) record what an LLM said about
a document so a deterministic vocabulary can later be mined from the evidence;
the entire guarantee of shadow mode is that the corpus accrues **without
changing what any agent sees**. Keeping them out of tag *filters* is structural
— filters address ``$.content_tags.<facet>`` and the shadow record is a sibling
top-level key — but keeping them out of served *payloads* is not, because the
splat does not care what a key means. This module is that second guarantee.

Deny-list, not allow-list, and deliberately so: a new metadata key should be
servable by default (that is what metadata is for), and the rare key that must
not be is a decision someone makes explicitly, once, here.
"""

from __future__ import annotations

from typing import Any

from trellis.schemas.classification import SHADOW_TAGS_KEY

#: Metadata keys stripped from every :class:`~trellis.schemas.pack.PackItem`.
#:
#: * :data:`~trellis.schemas.classification.SHADOW_TAGS_KEY` — LLM tags
#:   recorded for measurement only. Serving them would defeat the point of
#:   shadow mode (the corpus is supposed to accrue with retrieval held still)
#:   and would put open-vocabulary, subject-revealing tags into a payload the
#:   live tags were deliberately kept clean of.
NON_SERVABLE_METADATA_KEYS: frozenset[str] = frozenset({SHADOW_TAGS_KEY})


def servable_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return ``metadata`` minus every key that must never be served.

    Returns a new mapping; the caller's is never mutated. A ``None`` or empty
    input yields ``{}`` so call sites can splat the result unconditionally.
    """
    if not metadata:
        return {}
    if not NON_SERVABLE_METADATA_KEYS.intersection(metadata):
        # Fast path: nothing to strip, but still a copy — the caller splats
        # this into a PackItem and must not alias the store's dict.
        return dict(metadata)
    return {k: v for k, v in metadata.items() if k not in NON_SERVABLE_METADATA_KEYS}


__all__ = ["NON_SERVABLE_METADATA_KEYS", "servable_metadata"]
