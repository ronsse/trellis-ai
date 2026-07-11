"""Format-handler registry for corpus ingestion.

Handlers are **pure**: text in, ``(metadata, warnings)`` out — no store
access, no I/O. They never rewrite content; the parent document stores
the file text verbatim so ``content_hash`` change detection means
exactly "the file changed" (ADR §4) and "show me the whole note" stays
faithful. What a handler extracts (frontmatter, wikilinks, speaker
turns) lands in document metadata.

Delivery order per ADR §2: markdown first; plaintext/transcript and PDF
(``handlers/pdf.py``, optional extra) are follow-up phases. Audio never
enters core — transcription is an external pre-step.
"""

from __future__ import annotations

from typing import Any, Protocol

from trellis.ingest_corpus.handlers.markdown import MarkdownHandler


class FormatHandler(Protocol):
    """A pure parser for one family of file formats."""

    #: Lower-case extensions (with dot) this handler accepts.
    extensions: tuple[str, ...]

    def parse(
        self, relpath: str, text: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Extract ``(metadata, warnings)`` from *text*.

        Warnings are report entries (``{"kind": ..., "path": ...}``),
        never exceptions — a malformed frontmatter block must not stop
        a vault sync.
        """
        ...


_HANDLERS: tuple[FormatHandler, ...] = (MarkdownHandler(),)

_BY_EXTENSION: dict[str, FormatHandler] = {
    ext: handler for handler in _HANDLERS for ext in handler.extensions
}


def handler_for(relpath: str) -> FormatHandler | None:
    """The handler registered for *relpath*'s extension, if any."""
    dot = relpath.rfind(".")
    if dot == -1:
        return None
    return _BY_EXTENSION.get(relpath[dot:].lower())


def supported_extensions() -> tuple[str, ...]:
    """All extensions the registry currently accepts, sorted."""
    return tuple(sorted(_BY_EXTENSION))


__all__ = [
    "FormatHandler",
    "MarkdownHandler",
    "handler_for",
    "supported_extensions",
]
