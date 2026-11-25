"""The rendered "Nearby" section, read the same way by every pack formatter.

The server renders the section once (:mod:`trellis.retrieve.nearby`) and puts
the text at ``pack.metadata["nearby"]["markdown"]``. The core formatters and
the SDK formatters both print that string through :func:`nearby_markdown`, so
neither re-derives it and the two cannot drift. ``PACK_ASSEMBLED.nearby``
carries the same block without the ``markdown`` key.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Key of the rendered text inside the ``nearby`` block.
NEARBY_MARKDOWN_KEY = "markdown"


def nearby_markdown(block: Mapping[str, Any] | None) -> str:
    """The section text a ``nearby`` block carries, or ``""`` when none."""
    if not isinstance(block, Mapping):
        return ""
    text = block.get(NEARBY_MARKDOWN_KEY)
    return text if isinstance(text, str) else ""


__all__ = ["NEARBY_MARKDOWN_KEY", "nearby_markdown"]
