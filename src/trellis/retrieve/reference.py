"""Reference documents: embed and serve a caller-written summary, keep the body.

A reference document is **one stored row**. ``content`` is the full text,
``metadata["summary"]`` is a short summary the caller wrote, and
``metadata["uri"]`` is a link to the original. When the row is a
*summary-served reference* the summary stands in for the body wherever the
body would have been embedded or shown in a pack; the body stays in the
document store and ``get_items`` returns it.

Every site that makes that choice calls this module, so the rule has one
definition:

* the embedder input and the vector row's excerpt
  (:func:`~trellis.retrieve.embed_ingest_hook.build_vector_row`, shared by the
  live hook, the evidence handler and ``trellis admin reindex-vectors``), and
  the row ``POST /vectors`` writes for a stored document, through the same
  :func:`shape_vector_row`;
* the pack excerpt on the axes that read the document row
  (:class:`~trellis.retrieve.strategies.KeywordSearch`, the public-only reads);
* the link line a pack renders and the ``Source`` line ``get_items`` renders.

A row is summary-served only when **all four** hold (:func:`summary_of`): its
form is ``reference``, its ``summary`` is a non-empty string, that summary is at
most :data:`~trellis.retrieve.embed_limits.EMBED_INPUT_CHAR_CAP` characters (it
is embedded whole or not at all), and it is not a chunk. The rule reads stored
metadata, so it applies to *any* non-chunk row that carries both the reference
form and a string summary, whoever wrote it: a document already stored, and a
corpus file whose frontmatter names both. Such a row's keyword excerpt changes
at deploy and its embedding at its next re-embed. The form condition is required
because ``summary`` is a common frontmatter key: a corpus file that carries
``summary:`` and does not also name the reference form, and its chunks, embed
and serve exactly as before. Anything else is untouched.

``excerpt_source`` is the one marker a summary-served item carries
(:data:`EXCERPT_SOURCE_KEY`). The formatters key off it, never off the form or
a bare ``uri``, and never off the summary, which is kept out of every copy
written per serve (:func:`pack_metadata`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trellis.retrieve.embed_limits import (
    EMBED_INPUT_CHAR_CAP,
    VECTOR_METADATA_EXCERPT_CHARS,
)
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.schemas.document_metadata import document_form_of

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "EXCERPT_SOURCE_KEY",
    "EXCERPT_SOURCE_SUMMARY",
    "REFERENCE_FORM",
    "SUMMARY_KEY",
    "URI_KEY",
    "clean_uri",
    "fetched_body",
    "is_summary_served",
    "pack_metadata",
    "served_text",
    "shape_vector_row",
    "summary_link_line",
    "summary_of",
    "summary_refusal",
]

#: ``document_form`` of a reference document.
REFERENCE_FORM = "reference"
#: Metadata key holding the caller-written summary.
SUMMARY_KEY = "summary"
#: Metadata key holding the link to the original.
URI_KEY = "uri"
#: Marker stamped on a vector row and on a keyword-axis pack item whose excerpt
#: is cut from the summary. A reserved name: a stored value under it is never
#: trusted, it is stripped and re-stamped.
EXCERPT_SOURCE_KEY = "excerpt_source"
EXCERPT_SOURCE_SUMMARY = "summary"

#: A link longer than this is not shown: a truncated link points nowhere.
URI_SHOW_MAX_CHARS = 500


def summary_of(metadata: Mapping[str, Any] | None) -> str | None:
    """The stripped summary of a summary-served reference, else ``None``.

    The single predicate. ``None`` for a document of any other form, for a
    reference with no usable summary, for one whose summary is longer than
    :data:`~trellis.retrieve.embed_limits.EMBED_INPUT_CHAR_CAP` (a summary is
    embedded whole or the row is not summary-served, so no path embeds a cut
    one), and for a chunk (it has ``parent_doc_id``), so those behave exactly as
    they did before reference documents existed.
    """
    if not metadata or document_form_of(metadata) != REFERENCE_FORM:
        return None
    if metadata.get("parent_doc_id"):
        return None
    summary = metadata.get(SUMMARY_KEY)
    if isinstance(summary, str):
        stripped = summary.strip()
        if stripped and len(stripped) <= EMBED_INPUT_CHAR_CAP:
            return stripped
    return None


def is_summary_served(metadata: Mapping[str, Any] | None) -> bool:
    """``True`` iff :func:`summary_of` finds a summary."""
    return summary_of(metadata) is not None


def served_text(content: str, metadata: Mapping[str, Any] | None) -> str:
    """The text to embed or excerpt: the summary of a reference, else ``content``."""
    summary = summary_of(metadata)
    return content if summary is None else summary


def pack_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """A document's metadata as a pack item on a document-backed axis carries it.

    The marker is stamped for a summary-served reference and stripped from
    anything else (a stored ``excerpt_source`` is not a fact about this serve),
    and the summary is withheld from a summary-served item: the excerpt already
    holds it, and the bag is returned to REST callers whole. Every other key,
    and every key of any other document, is copied as it was.
    """
    bag = dict(metadata or {})
    served = is_summary_served(bag)
    bag.pop(EXCERPT_SOURCE_KEY, None)
    if served:
        bag.pop(SUMMARY_KEY, None)
        bag[EXCERPT_SOURCE_KEY] = EXCERPT_SOURCE_SUMMARY
    return bag


def shape_vector_row(
    row: dict[str, Any], content: str, metadata: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Set what a vector row says of its document's served text; return ``row``.

    ``row`` is the row's metadata so far, ``content`` and ``metadata`` are the
    *document's*, never the caller's. The row's ``content`` excerpt is cut from
    :func:`served_text`. The marker is the row's to stamp from the document, so a
    value already under its name is dropped and only a summary-served document
    gets one; and a served summary is dropped from the row, because the excerpt
    is already cut from it. Nothing else is touched. Both writers of a vector
    row (:func:`~trellis.retrieve.embed_ingest_hook.build_vector_row` and
    ``POST /vectors``) call this, so the two cannot disagree about it.
    """
    served = is_summary_served(metadata)
    row.pop(EXCERPT_SOURCE_KEY, None)
    if served:
        row.pop(SUMMARY_KEY, None)
    row["content"] = truncate_excerpt(
        served_text(content, metadata), VECTOR_METADATA_EXCERPT_CHARS
    )
    if served:
        row[EXCERPT_SOURCE_KEY] = EXCERPT_SOURCE_SUMMARY
    return row


def clean_uri(value: object) -> str | None:
    """``value`` as a link safe to put in one markdown code span, or ``None``.

    A ``uri`` is a caller's string. It is collapsed to one line, stripped of
    control characters, and has its backticks escaped so it cannot end the code
    span it is rendered in. A non-string, an empty one, and one longer than
    :data:`URI_SHOW_MAX_CHARS` give ``None``.
    """
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in " ".join(value.split()) if ch.isprintable())
    cleaned = cleaned.replace("`", "%60")
    if not cleaned or len(cleaned) > URI_SHOW_MAX_CHARS:
        return None
    return cleaned


def summary_link_line(metadata: Mapping[str, Any] | None) -> str | None:
    """The line a pack puts under a summary-served excerpt, else ``None``.

    Keyed off the marker, so an item that merely carries a ``uri`` renders as
    it always did.
    """
    if not metadata or metadata.get(EXCERPT_SOURCE_KEY) != EXCERPT_SOURCE_SUMMARY:
        return None
    uri = clean_uri(metadata.get(URI_KEY))
    if uri is None:
        return "Summary only. Full text: get_items."
    return f"Summary only. Source: `{uri}`. Full text: get_items."


def fetched_body(content: str, metadata: Mapping[str, Any] | None) -> str:
    """The body ``get_items`` renders: a reference's link leads the full text."""
    if not is_summary_served(metadata):
        return content
    uri = clean_uri((metadata or {}).get(URI_KEY))
    return content if uri is None else f"Source: `{uri}`\n\n{content}"


def summary_refusal(metadata: Mapping[str, Any] | None, *, limit: int) -> str | None:
    """Why a reference document's summary may not be written, or ``None``.

    Applies only to a document whose form is ``reference`` and whose metadata
    carries a ``summary`` key: that summary must be a string, non-empty once
    stripped, and at most ``limit`` characters, because a summary is embedded
    whole or not at all.
    """
    if not metadata or document_form_of(metadata) != REFERENCE_FORM:
        return None
    if SUMMARY_KEY not in metadata:
        return None
    summary = metadata[SUMMARY_KEY]
    if not isinstance(summary, str):
        return f"summary must be a string, not {type(summary).__name__}"
    length = len(summary.strip())
    if length == 0:
        return "summary must not be empty"
    if length > limit:
        return f"summary is {length} characters; the limit is {limit}"
    return None
