"""Soft-enforcement tests for the anti-pattern entity-type validator.

Per ``adr-source-modeling-discipline.md`` (sibling Unit G0 — reference
by path; do not assume content beyond the policy summarised here),
per-column entities are an anti-pattern in the Trellis graph: a single
Dataset / Table should hold its columns as a list in
``properties.columns`` rather than spawning thousands of structural
leaves. The
:func:`~trellis.schemas.well_known.validate_entity_type_not_anti_pattern`
helper enforces this *softly*: it logs but never raises, preserving the
open-string contract documented in CLAUDE.md.

These tests pin three behaviours:

1. Blocklist value without opt-in → ``structlog`` WARNING under event
   ``entity_type_anti_pattern_warning`` with a recommendation that
   names the properties shape and the opt-in flag.
2. Blocklist value with ``allow_structural_leaf=True`` → ``structlog``
   INFO under event ``entity_type_anti_pattern_opt_in_acknowledged``,
   confirming the deliberate opt-in for audit purposes.
3. Non-blocklist entity types → silent (the validator is advisory only
   and must not produce noise for ordinary domain types).

The fixture pattern mirrors ``tests/unit/schemas/test_entity_type_validator.py``
so neighbouring tests that install a CRITICAL-level structlog wrapper
do not short-circuit the capturing processor.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog
from structlog.testing import capture_logs

from trellis.schemas.well_known import (
    ENTITY_TYPE_ANTI_PATTERNS,
    validate_entity_type_not_anti_pattern,
)


@pytest.fixture
def log_output() -> Iterator[list[dict]]:
    """Capture structlog events emitted during the test.

    Saves and restores the full structlog config so neighbouring tests
    that install a filtering wrapper (e.g., ``tests/unit/mcp``) cannot
    leak state and short-circuit ``warning()`` / ``info()`` before the
    capturing processor runs.
    """
    saved = structlog.get_config()
    structlog.configure(
        wrapper_class=structlog.BoundLogger,
        processors=saved.get("processors", []),
    )
    try:
        with capture_logs() as cap:
            yield cap
    finally:
        structlog.configure(**saved)


def _events_with_key(cap: list[dict], event_key: str) -> list[dict]:
    return [e for e in cap if e.get("event") == event_key]


class TestValidateAntiPatternWarningWithoutOptIn:
    """Blocklist entity types emit a WARNING when ``allow_structural_leaf`` is False."""

    @pytest.mark.parametrize("entity_type", sorted(ENTITY_TYPE_ANTI_PATTERNS))
    def test_validate_anti_pattern_warning_without_opt_in(
        self,
        entity_type: str,
        log_output: list[dict],
    ) -> None:
        validate_entity_type_not_anti_pattern(entity_type)
        events = _events_with_key(log_output, "entity_type_anti_pattern_warning")
        assert len(events) == 1, (
            f"expected one WARNING for {entity_type!r}, got {events!r}"
        )
        event = events[0]
        assert event["log_level"] == "warning"
        assert event["entity_type"] == entity_type
        # Recommendation must point operators at the properties shape and
        # mention the opt-in escape hatch — both halves of the message
        # carry weight, so pin both substrings.
        recommendation = event["recommendation"]
        assert "properties.columns" in recommendation
        assert "allow_structural_leaf=True" in recommendation
        # No INFO event should have been emitted alongside the warning.
        assert (
            _events_with_key(log_output, "entity_type_anti_pattern_opt_in_acknowledged")
            == []
        )


class TestValidateAntiPatternInfoWithOptIn:
    """``allow_structural_leaf=True`` switches the emit from WARNING to INFO."""

    @pytest.mark.parametrize("entity_type", sorted(ENTITY_TYPE_ANTI_PATTERNS))
    def test_validate_anti_pattern_info_with_opt_in(
        self,
        entity_type: str,
        log_output: list[dict],
    ) -> None:
        validate_entity_type_not_anti_pattern(
            entity_type, allow_structural_leaf=True
        )
        events = _events_with_key(
            log_output, "entity_type_anti_pattern_opt_in_acknowledged"
        )
        assert len(events) == 1, (
            f"expected one INFO ack for {entity_type!r}, got {events!r}"
        )
        event = events[0]
        assert event["log_level"] == "info"
        assert event["entity_type"] == entity_type
        # The opt-in path must NOT emit a warning — that would defeat
        # the audit-log distinction between deliberate and accidental
        # anti-pattern usage.
        assert _events_with_key(log_output, "entity_type_anti_pattern_warning") == []


class TestValidateNonAntiPatternSilent:
    """Non-blocklist entity types emit nothing — advisory only."""

    @pytest.mark.parametrize(
        "entity_type",
        [
            "Person",  # canonical
            "person",  # alias
            "Dataset",  # canonical Dataset (the recommended replacement)
            "dbt_model",  # open-string domain type
            "uc_table",  # open-string domain type
            "custom_type",  # arbitrary open string
            "",  # empty string — still not on blocklist
        ],
    )
    def test_validate_non_anti_pattern_silent(
        self,
        entity_type: str,
        log_output: list[dict],
    ) -> None:
        validate_entity_type_not_anti_pattern(entity_type)
        assert _events_with_key(log_output, "entity_type_anti_pattern_warning") == []
        assert (
            _events_with_key(log_output, "entity_type_anti_pattern_opt_in_acknowledged")
            == []
        )

    def test_non_anti_pattern_silent_with_opt_in_flag(
        self,
        log_output: list[dict],
    ) -> None:
        # Setting the opt-in flag on a non-blocklist type must also stay
        # silent — the flag is only meaningful when the entity type is
        # on the blocklist, otherwise it's a no-op.
        validate_entity_type_not_anti_pattern("Person", allow_structural_leaf=True)
        validate_entity_type_not_anti_pattern("dbt_model", allow_structural_leaf=True)
        assert _events_with_key(log_output, "entity_type_anti_pattern_warning") == []
        assert (
            _events_with_key(log_output, "entity_type_anti_pattern_opt_in_acknowledged")
            == []
        )


class TestBlocklistMembership:
    """Pin the four blocklist values so drift is caught by a unit test."""

    def test_blocklist_contains_expected_four(self) -> None:
        assert frozenset(
            {"Column", "column", "TableColumn", "table_column"}
        ) == ENTITY_TYPE_ANTI_PATTERNS
