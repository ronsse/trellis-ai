"""Output formatting utilities for the CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import typer

if TYPE_CHECKING:
    from collections.abc import Callable


def filter_fields(
    items: list[dict[str, Any]], fields: str | None
) -> list[dict[str, Any]]:
    """Filter dict items to only include specified fields.

    Args:
        items: List of dicts to filter.
        fields: Comma-separated field names, or None for all fields.

    Returns:
        Filtered list of dicts.
    """
    if not fields:
        return items

    field_list = [f.strip() for f in fields.split(",")]
    return [{k: v for k, v in item.items() if k in field_list} for item in items]


def truncate_values(
    items: list[dict[str, Any]], max_chars: int | None
) -> list[dict[str, Any]]:
    """Truncate string values in dicts to max_chars.

    Args:
        items: List of dicts.
        max_chars: Max characters for string values, or None for no truncation.

    Returns:
        Items with truncated string values.
    """
    if not max_chars:
        return items

    result = []
    for item in items:
        new_item = {}
        for k, v in item.items():
            if isinstance(v, str) and len(v) > max_chars:
                new_item[k] = v[:max_chars] + "..."
            else:
                new_item[k] = v
        result.append(new_item)
    return result


def format_output(
    items: list[dict[str, Any]],
    output_format: str,
    fields: str | None = None,
    truncate: int | None = None,
    wrapper: dict[str, Any] | None = None,
) -> str:
    """Format a list of items for output.

    Args:
        items: List of dicts to format.
        output_format: "json", "jsonl", or "tsv".
        fields: Comma-separated field names to include.
        truncate: Max characters for string values.
        wrapper: Optional wrapper dict for JSON format (items inserted as "items" key).

    Returns:
        Formatted string.
    """
    items = filter_fields(items, fields)
    items = truncate_values(items, truncate)

    if output_format == "jsonl":
        return "\n".join(json.dumps(item) for item in items)

    if output_format == "tsv":
        if not items:
            return ""
        headers = list(items[0].keys())
        lines = ["\t".join(headers)]
        lines.extend("\t".join(str(item.get(h, "")) for h in headers) for item in items)
        return "\n".join(lines)

    # json format
    if wrapper is not None:
        wrapper["items"] = items
        wrapper["count"] = len(items)
        return json.dumps(wrapper)
    return json.dumps(items)


def emit_machine_text(text: str) -> None:
    """Write an already-serialized machine payload to stdout, unmodified.

    ``rich.console.Console.print`` is not a transport for machine output.
    It does two things to a plain string that the consumer cannot undo,
    and neither announces itself (#403):

    * **Emoji substitution.** Rich replaces ``:name:`` shortcodes, so
      ``corpus:notes:doc0`` prints with the ``:notes:`` collapsed to a
      musical-notes emoji. The JSON still parses — the *value* is now a
      document id that does not exist in any store, which is worse than a
      parse error because nothing raises. Trellis ids are
      colon-delimited by construction
      (``corpus:<source_system>:<sha1>``, trace ids, entity ids, event
      types), and ``notes``, ``book``, ``art``, ``key``, ``link``,
      ``memo``, ``zap``, ``warning`` and ``x`` are all live emoji names.
    * **Line wrapping at the console width.** A newline folded into a JSON
      string literal is an invalid control character, so the payload stops
      parsing outright. The width comes from ``COLUMNS`` or the tty, so the
      same command succeeds in one terminal and fails in another — and in
      CI, where the width is neither.

    ``typer.echo`` does neither. It is the only sanctioned door for
    ``--format json`` / ``jsonl`` / ``tsv`` output;
    ``tests/unit/test_machine_output_rule.py`` enforces that no
    ``console.print`` call in ``trellis_cli`` carries a serialized payload.
    """
    typer.echo(text)


def emit_json(
    payload: Any,
    *,
    indent: int | None = None,
    default: Callable[[Any], Any] | None = None,
) -> None:
    """Serialize *payload* as JSON and write it to stdout, bypassing Rich.

    ``indent`` and ``default`` are pass-throughs to :func:`json.dumps` for
    the surfaces that pretty-print or carry non-JSON-native values; they
    exist so those callers do not have to reach around this function to
    ``json.dumps`` and reintroduce the Rich path. See
    :func:`emit_machine_text` for what that path does to a payload.
    """
    emit_machine_text(json.dumps(payload, indent=indent, default=default))
