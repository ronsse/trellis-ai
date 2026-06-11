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

These tests pin the two-signal semantics (ADR §2.5):

1. Blocklist value with neither signal → ``structlog`` WARNING under
   event ``entity_type_anti_pattern_warning`` with a recommendation
   naming the properties shape and the two-signal opt-in, plus an
   ``adr`` field referencing the governing ADR (§2.3).
2. ``allow_structural_leaf=True`` alone (``node_role`` semantic or
   unset) → still WARNING — the operator opted out of the policy but
   did not declare the column structural, almost certainly a mistake.
3. ``node_role=STRUCTURAL`` alone → still WARNING — structural role
   without the explicit opt-out flag.
4. Both signals → ``structlog`` INFO under event
   ``entity_type_anti_pattern_opt_in_acknowledged`` and no WARNING,
   confirming the deliberate opt-in for audit purposes.
5. Non-blocklist entity types → silent (the validator is advisory only
   and must not produce noise for ordinary domain types).

The same semantics are pinned through the two producing models —
:class:`~trellis.schemas.extraction.EntityDraft` and
:class:`~trellis.extract.json_rules.EntityRule` — whose
``model_validator`` hooks forward both signals (and, for ``EntityRule``,
bind the rule name onto the event).

The fixture pattern mirrors ``tests/unit/schemas/test_entity_type_validator.py``
so neighbouring tests that install a CRITICAL-level structlog wrapper
do not short-circuit the capturing processor.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog
from structlog.testing import capture_logs

from trellis.extract.json_rules import EntityRule
from trellis.schemas.enums import NodeRole
from trellis.schemas.extraction import EntityDraft
from trellis.schemas.well_known import (
    ENTITY_TYPE_ANTI_PATTERN_ADR,
    ENTITY_TYPE_ANTI_PATTERNS,
    validate_entity_type_not_anti_pattern,
)

WARNING_EVENT = "entity_type_anti_pattern_warning"
ACK_EVENT = "entity_type_anti_pattern_opt_in_acknowledged"


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


def _assert_single_warning(cap: list[dict], entity_type: str) -> dict:
    """Assert exactly one WARNING (and no INFO ack) and return it."""
    events = _events_with_key(cap, WARNING_EVENT)
    assert len(events) == 1, f"expected one WARNING for {entity_type!r}, got {events!r}"
    event = events[0]
    assert event["log_level"] == "warning"
    assert event["entity_type"] == entity_type
    # §2.3: the warning must reference the governing ADR.
    assert event["adr"] == ENTITY_TYPE_ANTI_PATTERN_ADR
    # Recommendation must point operators at the properties shape and
    # name both halves of the two-signal opt-in.
    recommendation = event["recommendation"]
    assert "properties.columns" in recommendation
    assert "allow_structural_leaf=True" in recommendation
    assert "node_role=STRUCTURAL" in recommendation
    # No INFO event should have been emitted alongside the warning.
    assert _events_with_key(cap, ACK_EVENT) == []
    return event


def _assert_single_ack(cap: list[dict], entity_type: str) -> dict:
    """Assert exactly one INFO ack (and no WARNING) and return it."""
    events = _events_with_key(cap, ACK_EVENT)
    assert len(events) == 1, (
        f"expected one INFO ack for {entity_type!r}, got {events!r}"
    )
    event = events[0]
    assert event["log_level"] == "info"
    assert event["entity_type"] == entity_type
    assert event["adr"] == ENTITY_TYPE_ANTI_PATTERN_ADR
    # The opt-in path must NOT emit a warning — that would defeat the
    # audit-log distinction between deliberate and accidental usage.
    assert _events_with_key(cap, WARNING_EVENT) == []
    return event


class TestValidateAntiPatternWarningWithoutOptIn:
    """Blocklist entity types warn when neither opt-in signal is set."""

    @pytest.mark.parametrize("entity_type", sorted(ENTITY_TYPE_ANTI_PATTERNS))
    def test_validate_anti_pattern_warning_without_opt_in(
        self,
        entity_type: str,
        log_output: list[dict],
    ) -> None:
        validate_entity_type_not_anti_pattern(entity_type)
        _assert_single_warning(log_output, entity_type)


class TestValidateAntiPatternSingleSignalStillWarns:
    """Either opt-in signal alone is insufficient — both must be present (§2.5)."""

    @pytest.mark.parametrize("entity_type", sorted(ENTITY_TYPE_ANTI_PATTERNS))
    def test_flag_only_still_warns(
        self,
        entity_type: str,
        log_output: list[dict],
    ) -> None:
        # allow_structural_leaf=True with a semantic role: the operator
        # opted out of the policy but did not declare the column
        # structural — almost certainly a mistake, so it must warn.
        validate_entity_type_not_anti_pattern(
            entity_type,
            allow_structural_leaf=True,
            node_role=NodeRole.SEMANTIC,
        )
        event = _assert_single_warning(log_output, entity_type)
        assert event["allow_structural_leaf"] is True
        assert event["node_role"] == "semantic"

    def test_flag_only_with_role_unset_still_warns(
        self,
        log_output: list[dict],
    ) -> None:
        validate_entity_type_not_anti_pattern("column", allow_structural_leaf=True)
        event = _assert_single_warning(log_output, "column")
        assert event["allow_structural_leaf"] is True
        assert event["node_role"] is None

    @pytest.mark.parametrize("entity_type", sorted(ENTITY_TYPE_ANTI_PATTERNS))
    def test_role_only_still_warns(
        self,
        entity_type: str,
        log_output: list[dict],
    ) -> None:
        validate_entity_type_not_anti_pattern(
            entity_type,
            node_role=NodeRole.STRUCTURAL,
        )
        event = _assert_single_warning(log_output, entity_type)
        assert event["allow_structural_leaf"] is False
        assert event["node_role"] == "structural"


class TestValidateAntiPatternInfoWithBothSignals:
    """Both signals together switch the emit from WARNING to INFO."""

    @pytest.mark.parametrize("entity_type", sorted(ENTITY_TYPE_ANTI_PATTERNS))
    def test_validate_anti_pattern_info_with_both_signals(
        self,
        entity_type: str,
        log_output: list[dict],
    ) -> None:
        validate_entity_type_not_anti_pattern(
            entity_type,
            allow_structural_leaf=True,
            node_role=NodeRole.STRUCTURAL,
        )
        event = _assert_single_ack(log_output, entity_type)
        assert event["node_role"] == "structural"


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
        assert _events_with_key(log_output, WARNING_EVENT) == []
        assert _events_with_key(log_output, ACK_EVENT) == []

    def test_non_anti_pattern_silent_with_both_signals(
        self,
        log_output: list[dict],
    ) -> None:
        # Setting both opt-in signals on a non-blocklist type must also
        # stay silent — the signals are only meaningful when the entity
        # type is on the blocklist, otherwise they are a no-op.
        validate_entity_type_not_anti_pattern(
            "Person",
            allow_structural_leaf=True,
            node_role=NodeRole.STRUCTURAL,
        )
        validate_entity_type_not_anti_pattern(
            "dbt_model",
            allow_structural_leaf=True,
            node_role=NodeRole.STRUCTURAL,
        )
        assert _events_with_key(log_output, WARNING_EVENT) == []
        assert _events_with_key(log_output, ACK_EVENT) == []


class TestEntityDraftTwoSignalSemantics:
    """The EntityDraft model_validator forwards both signals."""

    def _draft(self, **overrides: object) -> EntityDraft:
        kwargs: dict[str, object] = {
            "entity_type": "column",
            "name": "ssn",
            **overrides,
        }
        return EntityDraft.model_validate(kwargs)

    def test_draft_no_signals_warns(self, log_output: list[dict]) -> None:
        self._draft()
        _assert_single_warning(log_output, "column")

    def test_draft_flag_only_warns(self, log_output: list[dict]) -> None:
        self._draft(allow_structural_leaf=True)
        event = _assert_single_warning(log_output, "column")
        assert event["allow_structural_leaf"] is True
        assert event["node_role"] == "semantic"  # EntityDraft default

    def test_draft_role_only_warns(self, log_output: list[dict]) -> None:
        self._draft(node_role=NodeRole.STRUCTURAL)
        event = _assert_single_warning(log_output, "column")
        assert event["allow_structural_leaf"] is False
        assert event["node_role"] == "structural"

    def test_draft_both_signals_acks(self, log_output: list[dict]) -> None:
        self._draft(allow_structural_leaf=True, node_role=NodeRole.STRUCTURAL)
        _assert_single_ack(log_output, "column")


class TestEntityRuleTwoSignalSemantics:
    """The EntityRule model_validator forwards both signals and the rule name."""

    def _rule(self, **overrides: object) -> EntityRule:
        kwargs: dict[str, object] = {
            "name": "pii_columns",
            "path": ["tables", "*", "columns", "*"],
            "entity_type": "column",
            "id_field": "id",
            **overrides,
        }
        return EntityRule.model_validate(kwargs)

    def test_rule_no_signals_warns_with_rule_name(self, log_output: list[dict]) -> None:
        self._rule()
        event = _assert_single_warning(log_output, "column")
        # §2.3: the EntityRule path must identify the producing rule.
        assert event["rule_name"] == "pii_columns"

    def test_rule_flag_only_warns(self, log_output: list[dict]) -> None:
        self._rule(allow_structural_leaf=True)
        event = _assert_single_warning(log_output, "column")
        assert event["rule_name"] == "pii_columns"
        assert event["allow_structural_leaf"] is True
        assert event["node_role"] == "semantic"  # EntityRule default

    def test_rule_role_only_warns(self, log_output: list[dict]) -> None:
        self._rule(node_role=NodeRole.STRUCTURAL)
        event = _assert_single_warning(log_output, "column")
        assert event["rule_name"] == "pii_columns"
        assert event["allow_structural_leaf"] is False

    def test_rule_both_signals_acks(self, log_output: list[dict]) -> None:
        self._rule(allow_structural_leaf=True, node_role=NodeRole.STRUCTURAL)
        event = _assert_single_ack(log_output, "column")
        assert event["rule_name"] == "pii_columns"


class TestBlocklistMembership:
    """Pin the four blocklist values so drift is caught by a unit test."""

    def test_blocklist_contains_expected_four(self) -> None:
        assert (
            frozenset({"Column", "column", "TableColumn", "table_column"})
            == ENTITY_TYPE_ANTI_PATTERNS
        )

    def test_adr_reference_path(self) -> None:
        assert (
            ENTITY_TYPE_ANTI_PATTERN_ADR
            == "docs/design/adr-source-modeling-discipline.md"
        )
