"""The Memories table's tags column must read a key a writer actually sets.

The tagging pipeline writes the 4 retrieval-shaping facets to
``metadata["content_tags"]`` (``trellis.classify.refresh``,
``ContentTags`` in ``trellis.schemas.classification``). No writer in
``src/trellis*`` sets a ``tags`` key, so a column that read
``metadata.tags`` was empty on every row regardless of whether the
pipeline ran (P0-2, docs/handoff review u2-ui-quality.md). This pins
that the page reads the real key, never the shadow-mode key
(``content_tags_shadow``, LLM proposals that were never applied — see
``trellis.retrieve.servable``), and labels the column with the ADR term
("Content tags", not "Tags").
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _tag_chips_body(page: str) -> str:
    found = re.search(
        r"function tagChips\(metadata\) \{(.*?)\n        \}\n", page, re.DOTALL
    )
    assert found is not None, "function tagChips not found in index.html"
    return found.group(1)


def test_tag_chips_reads_content_tags_not_the_dead_tags_key() -> None:
    page = INDEX_HTML.read_text(encoding="utf-8")
    body = _tag_chips_body(page)
    assert "metadata.content_tags" in body
    # The regression this pins: the old code read `metadata.tags`, a key
    # no writer sets, so every row's tag cell was silently empty.
    assert "metadata.tags" not in body


def test_tag_chips_never_reads_the_shadow_key() -> None:
    """``content_tags_shadow`` is a proposal, not an applied tag — reading
    it here would show an operator tags that never actually ran."""
    page = INDEX_HTML.read_text(encoding="utf-8")
    body = _tag_chips_body(page)
    assert "content_tags_shadow" not in body
    # No chip-rendering code anywhere reads the shadow key as data (a
    # `.content_tags_shadow` / `['content_tags_shadow']` property access);
    # the only mention allowed is the explanatory comment above.
    assert not re.search(r"[.\[]content_tags_shadow\b", page)


def test_tag_chips_covers_exactly_the_four_content_tags_facets() -> None:
    """The 4 facets from ``ContentTags`` (adr-terminology.md): domain,
    content_type, scope, signal_quality. Not retrieval_affinity/custom/
    classified_by/classification_version — those aren't retrieval-shaping
    facets a reviewer scans a memory row for."""
    page = INDEX_HTML.read_text(encoding="utf-8")
    facets = re.search(r"CONTENT_TAG_FACETS = \[([^\]]*)\];", page)
    assert facets is not None, "CONTENT_TAG_FACETS not found"
    values = [v.strip().strip("'\"") for v in facets.group(1).split(",")]
    assert values == ["domain", "content_type", "scope", "signal_quality"]


def test_memories_column_header_uses_the_adr_term() -> None:
    """adr-terminology.md: ContentTags, not a bare 'Tags' column header
    that invites confusion with an open free-text tag field."""
    page = INDEX_HTML.read_text(encoding="utf-8")
    assert "<th>Content tags</th>" in page
    assert "<th>Tags</th>" not in page
