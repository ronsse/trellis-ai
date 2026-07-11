"""Markdown format handler — frontmatter to metadata, wikilink capture.

YAML frontmatter keys become document metadata (sanitized to
JSON-serializable values — YAML happily parses dates and timestamps the
metadata JSON column cannot hold). ``[[wikilinks]]`` in the body are
collected into ``metadata.wikilinks`` as alias/edge *candidates* only:
no graph writes happen in a handler — extraction is a separate,
LLM-cost-gated pass (ADR §5).
"""

from __future__ import annotations

import re
from typing import Any

import yaml

#: Frontmatter block: a leading ``---`` line, YAML, a closing ``---`` or
#: ``...`` line. Anchored to the very start of the file per convention.
_FRONTMATTER = re.compile(
    r"\A---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.DOTALL
)

#: ``[[target]]``, ``[[target|alias]]``, ``[[target#heading]]`` — the
#: captured group is the bare link target.
_WIKILINK = re.compile(r"\[\[([^\]\[|#\n]+)(?:[#|][^\]\[\n]*)?\]\]")

#: Frontmatter keys the sync layer owns; a note that happens to declare
#: one cannot corrupt sync bookkeeping, so it is namespaced instead.
_RESERVED_KEYS = frozenset(
    {
        "source_path",
        "source_system",
        "chunk_count",
        "parent_doc_id",
        "chunk_index",
        "char_span",
    }
)


def _jsonify(value: Any) -> Any:
    """Coerce a YAML-parsed value into JSON-serializable form."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_jsonify(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    return str(value)  # dates, timestamps, anything exotic


class MarkdownHandler:
    """Pure parser for markdown notes (Obsidian-style vaults included)."""

    extensions: tuple[str, ...] = (".md", ".markdown")

    def parse(
        self, relpath: str, text: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        metadata: dict[str, Any] = {}
        warnings: list[dict[str, Any]] = []

        match = _FRONTMATTER.match(text)
        if match is not None:
            try:
                parsed = yaml.safe_load(match.group(1))
            except yaml.YAMLError as exc:
                parsed = None
                warnings.append(
                    {
                        "kind": "malformed_frontmatter",
                        "path": relpath,
                        "detail": str(exc).splitlines()[0],
                    }
                )
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    name = str(key)
                    if name in _RESERVED_KEYS:
                        name = f"frontmatter_{name}"
                    metadata[name] = _jsonify(value)
            elif parsed is not None:
                warnings.append(
                    {
                        "kind": "malformed_frontmatter",
                        "path": relpath,
                        "detail": f"expected a mapping, got {type(parsed).__name__}",
                    }
                )

        body = text[match.end() :] if match is not None else text
        wikilinks: list[str] = []
        seen: set[str] = set()
        for target in _WIKILINK.findall(body):
            name = target.strip()
            if name and name not in seen:
                seen.add(name)
                wikilinks.append(name)
        if wikilinks:
            metadata["wikilinks"] = wikilinks

        return metadata, warnings
