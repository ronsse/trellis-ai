"""The trace detail links an evidence ref only to a view that can show it.

An ``EvidenceRef`` holds ``evidence_id`` and ``role``, and the evidence id is
not a node id: the node is ``evidence:<id>``, and only trace extraction writes
it. ``GET /traces/{trace_id}`` names in ``evidence_links`` what can show each
ref, and the page links there or shows the evidence id as text.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _evidence_section(page: str) -> str:
    found = re.search(
        r"let evidenceHtml = '';\n(.*?)evidenceHtml \+= '</div>';", page, re.DOTALL
    )
    assert found is not None, "the trace detail's evidence section not found"
    return found.group(1)


def test_an_evidence_ref_links_only_to_a_target_the_api_named() -> None:
    section = _evidence_section(INDEX_HTML.read_text(encoding="utf-8"))
    assert "JSON.stringify" not in section
    # The page reads the key the route writes, which is null when the route
    # could not read the knowledge stores.
    assert "const links = data.evidence_links || {};" in section
    # Each link renders under its own target's guard and carries that id.
    links = re.findall(
        r"if \((link\.\w+)\) \{\s*entry = `[^`]*"
        r'data-action="(\w+)" data-id="\$\{escHtml\((link\.\w+)\)\}"',
        section,
    )
    assert links == [
        ("link.document_id", "navigateToDocument", "link.document_id"),
        ("link.entity_id", "navigateToEntity", "link.entity_id"),
    ]
    assert section.count("data-action=") == len(links)
    # A ref nothing can show is text: nothing to click, so nothing to 404.
    tags = re.findall(r"`(<span[^>]*>)\$\{escHtml\(e\.evidence_id\)\}</span>`", section)
    assert any("data-action" not in tag for tag in tags), tags
    # The section writes markup, so each value it takes from the ref or its
    # link goes in through one escHtml call.
    values = re.findall(r"\$\{([^}]*\b(?:e|link)\.[^}]*)\}", section)
    assert values, "the evidence section interpolates nothing from the API"
    assert all(re.fullmatch(r"escHtml\([\w.]+\)", v) for v in values), values
