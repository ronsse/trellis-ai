"""The serving boundary — which stored metadata keys may reach a pack.

Search strategies build a :class:`~trellis.schemas.pack.PackItem` by splatting
the store's metadata mapping wholesale (``metadata={"source_strategy": ...,
**metadata}``). That is the right default — a strategy cannot know which keys a
consumer will find useful, and an allowlist would silently drop every key added
later. But it means *any* key written onto a document is served, and some keys
exist precisely because they must not be.

Today that is exactly one key. Shadow tags (#321) record what an LLM said about
a document so a deterministic vocabulary can later be mined from the evidence;
the whole guarantee of shadow mode is that the corpus accrues **without
changing what any pack contains**. Keeping them out of tag *filters* is
structural — filters address ``$.content_tags.<facet>`` and the shadow record
is a sibling top-level key — but keeping them out of served *payloads* is not,
because the splat does not care what a key means. This module is that second
guarantee.

**Enforced in PackBuilder, not per strategy.** ``PackBuilder`` takes its
strategies by injection and exposes ``add_strategy``, so the set is open: a
rule applied inside the three built-in strategies would silently not hold for
the fourth. Filtering where the builder collects results covers every strategy,
including ones added later and out of tree.

**Scope of the claim.** This says shadow tags never reach a *pack*, not that
they are confidential. ``GET /api/v1/documents`` returns full document metadata
to a read-scoped caller, and that is correct — it is the same access path as
the content the tags describe, which is exactly why the content-revealing half
of a shadow verdict lives on the document rather than in the event log (see
:mod:`trellis.classify.shadow`). The invariant here is about retrieval, not
secrecy.

Deny-list, not allow-list, and deliberately so: a new metadata key should be
servable by default (that is what metadata is for), and the rare key that must
not be is a decision someone makes explicitly, once, here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trellis.schemas.classification import SHADOW_TAGS_KEY

if TYPE_CHECKING:
    from collections.abc import Iterable

    from trellis.schemas.pack import PackItem

#: Metadata keys stripped from every :class:`~trellis.schemas.pack.PackItem`.
#:
#: * :data:`~trellis.schemas.classification.SHADOW_TAGS_KEY` — LLM tags
#:   recorded for measurement only. Serving them would defeat the point of
#:   shadow mode, whose corpus is supposed to accrue with retrieval held still.
NON_SERVABLE_METADATA_KEYS: frozenset[str] = frozenset({SHADOW_TAGS_KEY})


def strip_non_servable(items: Iterable[PackItem]) -> list[PackItem]:
    """Return ``items`` with every non-servable metadata key removed.

    Items that carry no denied key are passed through **unchanged** rather than
    copied — the common case by far, so the boundary costs nothing when there
    is nothing to strip.
    """
    out: list[PackItem] = []
    for item in items:
        metadata = item.metadata
        if metadata and not NON_SERVABLE_METADATA_KEYS.isdisjoint(metadata):
            out.append(
                item.model_copy(
                    update={
                        "metadata": {
                            k: v
                            for k, v in metadata.items()
                            if k not in NON_SERVABLE_METADATA_KEYS
                        }
                    }
                )
            )
        else:
            out.append(item)
    return out


__all__ = ["NON_SERVABLE_METADATA_KEYS", "strip_non_servable"]
