"""Retrieval helper — turn an agent intent into a formatted context pack.

Thin layer on top of :meth:`TrellisClient.assemble_pack` that:

* Centralizes the budget knobs your team cares about (``max_items``,
  ``max_tokens``) so every caller uses the same defaults.
* Pulls out the fields an agent actually needs (``pack_id``,
  ``items``) and drops transport noise.
* Gives you one place to add post-processing (e.g. filter by
  ``signal_quality``, join with your own metadata, render to markdown).

Swap the defaults to match your product's token budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trellis_sdk import TrellisClient

_DEFAULT_MAX_ITEMS = 25
_DEFAULT_MAX_TOKENS = 2000


@dataclass
class ContextPack:
    pack_id: str
    count: int
    items: list[dict[str, Any]]

    def summarize(self) -> str:
        if not self.items:
            return f"Pack {self.pack_id}: no items"
        lines = [f"Pack {self.pack_id}: {self.count} items"]
        for item in self.items:
            item_id = item.get("item_id", "?")
            item_type = item.get("item_type", "?")
            excerpt = (item.get("excerpt") or "").replace("\n", " ")[:60]
            score = item.get("relevance_score", 0.0)
            lines.append(
                f"  - [{item_type}] {item_id[:16]}... "
                f"(score={score:.2f}) {excerpt}"
            )
        return "\n".join(lines)


def get_context(
    client: TrellisClient,
    intent: str,
    *,
    domain: str | None = None,
    max_items: int = _DEFAULT_MAX_ITEMS,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> ContextPack:
    """Assemble a context pack and return it in a lean dataclass."""
    raw = client.assemble_pack(
        intent=intent,
        domain=domain,
        max_items=max_items,
        max_tokens=max_tokens,
    )
    return ContextPack(
        pack_id=raw.get("pack_id", ""),
        count=raw.get("count", 0),
        items=raw.get("items", []),
    )


__all__ = ["ContextPack", "get_context"]
