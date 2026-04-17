"""Output formatting utilities for the CLI."""

from __future__ import annotations

import json
from typing import Any


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
