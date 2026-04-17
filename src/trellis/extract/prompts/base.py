"""PromptTemplate dataclass + render helper.

A template carries a static system prompt and a ``str.format``-style
user-message template.  The :func:`render` helper normalizes the
optional extractor parameters (type hints, domain, source system) into
the template variables so templates can reference them unconditionally.
"""

from __future__ import annotations

from dataclasses import dataclass

from trellis.llm.types import Message


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """A versioned system+user prompt template for an LLM-tier extractor.

    Attributes:
        name: Stable identifier (e.g. ``"entity_extraction"``).  Used
            for telemetry and, when a prompt library lands, lookup.
        version: Semver-ish version string.  Bump when the prompt text
            changes in a way that affects extracted output.
        system: Static system prompt.  Never formatted; treated as a
            literal string.
        user_template: User message body, formatted via
            :meth:`str.format`.  Must reference only the variables
            documented in :func:`render`.
    """

    name: str
    version: str
    system: str
    user_template: str


def render(
    template: PromptTemplate,
    *,
    text: str,
    entity_type_hints: list[str] | None = None,
    edge_kind_hints: list[str] | None = None,
    domain: str | None = None,
    source_system: str | None = None,
) -> list[Message]:
    """Render a template into an LLM-ready ``list[Message]``.

    All optional parameters collapse to the empty string when ``None``
    or empty so templates can include the placeholder unconditionally
    and still produce clean output.  The resulting messages pair:

    1. a ``system`` message with the template's literal ``system`` text;
    2. a ``user`` message with ``user_template`` formatted against the
       variables below.

    Template variables:

    * ``{text}`` — the raw input to extract from (required).
    * ``{type_hints}`` — rendered line like
      ``"Prefer these entity types: a, b, c"`` or ``""``.
    * ``{edge_hints}`` — rendered line like
      ``"Prefer these edge kinds: x, y"`` or ``""``.
    * ``{domain_line}`` — ``"Domain: <name>"`` or ``""``.
    * ``{source_line}`` — ``"Source system: <name>"`` or ``""``.
    """
    user_content = template.user_template.format(
        text=text,
        type_hints=_format_hints("Prefer these entity types", entity_type_hints),
        edge_hints=_format_hints("Prefer these edge kinds", edge_kind_hints),
        domain_line=f"Domain: {domain}" if domain else "",
        source_line=f"Source system: {source_system}" if source_system else "",
    )
    return [
        Message(role="system", content=template.system),
        Message(role="user", content=user_content),
    ]


def _format_hints(label: str, hints: list[str] | None) -> str:
    """Render a hint list as a single line, or empty string when absent."""
    if not hints:
        return ""
    return f"{label}: {', '.join(hints)}"
