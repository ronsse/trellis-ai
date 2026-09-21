"""Pure withholding payload parsing and markdown rendering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class WithheldGroup:
    """One reason and the number of distinct items withheld under it."""

    reason: str
    count: int


@dataclass(frozen=True)
class WithholdingSummary:
    """What one pack build withheld, grouped by reason."""

    groups: tuple[WithheldGroup, ...] = ()
    withheld_item_ids: tuple[str, ...] = ()
    non_absence_reasons: tuple[str, ...] = ()
    section_filtered: int = 0
    served_count: int = 0

    @property
    def total(self) -> int:
        """Distinct items absent from the pack that a gate removed."""
        return sum(group.count for group in self.groups)

    @property
    def section_note_applies(self) -> bool:
        """Whether the caller-facing section-routing sentence should render."""
        return self.section_filtered > 0 and self.served_count == 0

    def as_telemetry(self) -> dict[str, Any]:
        """Serialize the summary for pack metadata and events."""
        return {
            "total": self.total,
            "by_reason": {group.reason: group.count for group in self.groups},
            "withheld_item_ids": list(self.withheld_item_ids),
            "non_absence_reasons": list(self.non_absence_reasons),
            "section_filtered": self.section_filtered,
            "served_count": self.served_count,
        }


def _keys(payload: Any) -> list[str]:
    """Return field names from an unreadable payload, never its values."""
    return sorted(str(key) for key in payload) if isinstance(payload, dict) else []


def _str_list(value: Any) -> list[str]:
    """Coerce a telemetry list field, tolerating absence."""
    return [str(item) for item in value] if isinstance(value, list) else []


def _count(payload: dict[str, Any], key: str) -> int:
    """Read a telemetry counter and warn when its value is unusable."""
    value = payload.get(key)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        logger.warning(
            "withholding_payload_unreadable",
            payload_keys=_keys(payload),
            field=key,
        )
        return 0
    return value


def withholding_from_payload(
    payload: dict[str, Any] | None,
) -> WithholdingSummary | None:
    """Rebuild a summary from a serialized withholding payload."""
    if payload is None:
        return None
    by_reason = payload.get("by_reason") if isinstance(payload, dict) else None
    if not isinstance(by_reason, dict):
        logger.warning("withholding_payload_unreadable", payload_keys=_keys(payload))
        return None
    try:
        groups = tuple(
            WithheldGroup(reason=str(reason), count=int(count))
            for reason, count in by_reason.items()
        )
    except (TypeError, ValueError):
        logger.warning("withholding_payload_unreadable", payload_keys=_keys(payload))
        return None
    return WithholdingSummary(
        groups=groups,
        withheld_item_ids=tuple(_str_list(payload.get("withheld_item_ids"))),
        non_absence_reasons=tuple(_str_list(payload.get("non_absence_reasons"))),
        section_filtered=_count(payload, "section_filtered"),
        served_count=_count(payload, "served_count"),
    )


def format_withholding_note(summary: WithholdingSummary | None) -> str:
    """Render counts and reasons without exposing withheld ids or content."""
    if summary is None:
        return ""
    sentences: list[str] = []
    if summary.groups:
        detail = ", ".join(f"{group.reason} {group.count}" for group in summary.groups)
        singular = summary.total == 1
        noun = "item" if singular else "items"
        verb = "was" if singular else "were"
        sentences.append(
            f"**Withheld:** {summary.total} {noun} matched this intent but {verb} "
            f"not served ({detail}). Counts only — no ids or content."
        )
    if summary.section_note_applies:
        count = summary.section_filtered
        noun = "item" if count == 1 else "items"
        sentences.append(
            f"**Section routing:** this pack is empty because {count} {noun} "
            "matched this intent but none of the sections you requested — "
            "not because nothing was found. Counts only — no ids or content."
        )
    return "\n".join(sentences)


__all__ = [
    "WithheldGroup",
    "WithholdingSummary",
    "format_withholding_note",
    "withholding_from_payload",
]
