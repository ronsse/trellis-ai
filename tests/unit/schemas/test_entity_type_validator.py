"""Near-miss warnings on ``Entity.entity_type`` and ``Edge.edge_kind``.

CLAUDE.md and ``docs/design/adr-graph-ontology.md`` make the open-string
contract a hard rule: closing the type set would break domain
integrations. The validator therefore *warns* (via ``structlog``) rather
than raising — the value is preserved verbatim, but a typo of a
well-known type leaves an audit trail downstream consumers can grep.

These tests pin three things:

1. Known values pass silently (no warning event emitted).
2. Near-misses (separator drift, case drift, one-char typo) emit a
   warning whose payload names both the input and the closest match.
3. Genuinely-unknown domain types pass silently — open-string contract
   is intact.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog
from structlog.testing import capture_logs

from trellis.schemas.entity import Entity
from trellis.schemas.enums import EdgeKind, EntityType
from trellis.schemas.graph import Edge


@pytest.fixture
def log_output() -> Iterator[list[dict]]:
    """Capture structlog events emitted during the test.

    Uses :func:`structlog.testing.capture_logs`, which inserts a
    capturing processor at the front of the chain. We *also* force the
    wrapper class to the structlog default for the duration of the
    test: other tests in the suite (notably ``tests/unit/mcp``) install
    a CRITICAL-level filtering wrapper without restoring it, which
    short-circuits ``warning()`` before the capturing processor ever
    runs. Saving and restoring the full config keeps tests
    order-independent regardless of what those neighbours leave behind.
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


# ── Entity.entity_type ────────────────────────────────────────────────


class TestEntityTypeKnownValues:
    """Canonical, alias, and legacy-enum values must all pass silently."""

    def test_canonical_passes_silently(self, log_output: list[dict]) -> None:
        Entity(entity_type="Person", name="alice")
        assert _events_with_key(log_output, "entity_type.suspicious_input") == []

    def test_legacy_alias_passes_silently(self, log_output: list[dict]) -> None:
        # ``service`` is a registered legacy alias of SoftwareApplication.
        Entity(entity_type="service", name="auth")
        assert _events_with_key(log_output, "entity_type.suspicious_input") == []

    def test_legacy_enum_value_passes_silently(self, log_output: list[dict]) -> None:
        Entity(entity_type=EntityType.PROJECT, name="trellis")
        assert _events_with_key(log_output, "entity_type.suspicious_input") == []


class TestEntityTypeNearMissWarns:
    """Typo of a well-known type emits a warning but value is preserved."""

    def test_separator_drift_warns(self, log_output: list[dict]) -> None:
        # ``project`` (alias) and ``Project`` (canonical) both normalise
        # to the same form — either suggestion is correct.
        entity = Entity(entity_type="pro-ject", name="x")
        assert entity.entity_type == "pro-ject"  # value preserved verbatim
        events = _events_with_key(log_output, "entity_type.suspicious_input")
        assert len(events) == 1
        assert events[0]["value"] == "pro-ject"
        assert events[0]["suggestion"] in {"project", "Project"}
        assert events[0]["log_level"] == "warning"

    def test_one_char_typo_warns(self, log_output: list[dict]) -> None:
        # One-char drop from canonical ``Person``.
        entity = Entity(entity_type="Persn", name="alice")
        assert entity.entity_type == "Persn"
        events = _events_with_key(log_output, "entity_type.suspicious_input")
        assert len(events) == 1
        assert events[0]["value"] == "Persn"
        assert events[0]["suggestion"] == "Person"

    def test_case_drift_warns(self, log_output: list[dict]) -> None:
        # Lowercase form of canonical that isn't already a registered
        # alias — ``Agent`` (PROV-O) has no lowercase legacy alias, so
        # ``agent`` should be flagged as a near-miss. (``dataset`` is
        # registered as an alias of ``Dataset`` for OpenLineage interop,
        # so it intentionally does *not* warn.)
        entity = Entity(entity_type="agent", name="alice")
        assert entity.entity_type == "agent"
        events = _events_with_key(log_output, "entity_type.suspicious_input")
        assert len(events) == 1
        assert events[0]["suggestion"] == "Agent"


class TestEntityTypeOpenStringPreserved:
    """Genuinely-novel domain types pass silently (open-set contract)."""

    def test_dbt_model_passes_silently(self, log_output: list[dict]) -> None:
        # ``dbt_model`` is a real domain type from trellis_workers.extract;
        # it has no near-miss in the well-known set.
        entity = Entity(entity_type="dbt_model", name="orders")
        assert entity.entity_type == "dbt_model"
        assert _events_with_key(log_output, "entity_type.suspicious_input") == []

    def test_uc_table_passes_silently(self, log_output: list[dict]) -> None:
        entity = Entity(entity_type="uc_table", name="main.analytics.orders")
        assert entity.entity_type == "uc_table"
        assert _events_with_key(log_output, "entity_type.suspicious_input") == []

    def test_arbitrary_string_passes_silently(self, log_output: list[dict]) -> None:
        entity = Entity(entity_type="kubernetes_deployment", name="api")
        assert entity.entity_type == "kubernetes_deployment"
        assert _events_with_key(log_output, "entity_type.suspicious_input") == []


# ── Edge.edge_kind ────────────────────────────────────────────────────


class TestEdgeKindKnownValues:
    """Canonical, alias, and legacy-enum values pass silently."""

    def test_canonical_passes_silently(self, log_output: list[dict]) -> None:
        Edge(source_id="a", target_id="b", edge_kind="used")
        assert _events_with_key(log_output, "edge_kind.suspicious_input") == []

    def test_legacy_alias_passes_silently(self, log_output: list[dict]) -> None:
        Edge(source_id="a", target_id="b", edge_kind="trace_used_evidence")
        assert _events_with_key(log_output, "edge_kind.suspicious_input") == []

    def test_legacy_enum_passes_silently(self, log_output: list[dict]) -> None:
        Edge(source_id="a", target_id="b", edge_kind=EdgeKind.ENTITY_DEPENDS_ON)
        assert _events_with_key(log_output, "edge_kind.suspicious_input") == []


class TestEdgeKindNearMissWarns:
    """Separator-drift typo of a well-known edge kind warns."""

    def test_separator_drift_warns(self, log_output: list[dict]) -> None:
        # Canonical is ``dependsOn``; ``dependson`` is a one-char drift.
        edge = Edge(source_id="a", target_id="b", edge_kind="dependson")
        assert edge.edge_kind == "dependson"
        events = _events_with_key(log_output, "edge_kind.suspicious_input")
        assert len(events) == 1
        assert events[0]["value"] == "dependson"
        assert events[0]["suggestion"] == "dependsOn"


class TestEdgeKindOpenStringPreserved:
    """Domain-specific edge kinds pass silently."""

    def test_dbt_references_passes_silently(self, log_output: list[dict]) -> None:
        edge = Edge(source_id="a", target_id="b", edge_kind="dbt_references")
        assert edge.edge_kind == "dbt_references"
        assert _events_with_key(log_output, "edge_kind.suspicious_input") == []

    def test_uc_column_of_passes_silently(self, log_output: list[dict]) -> None:
        edge = Edge(source_id="a", target_id="b", edge_kind="uc_column_of")
        assert edge.edge_kind == "uc_column_of"
        assert _events_with_key(log_output, "edge_kind.suspicious_input") == []
