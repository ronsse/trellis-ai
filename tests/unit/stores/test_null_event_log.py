"""NullEventLog — the no-op event log for knowledge-plane-only mode (#196).

Covers the no-op semantics in isolation plus the registry wiring that makes
``event_log: {backend: null}`` resolve to a :class:`NullEventLog` without
requiring any Operational-Plane persistence (no ``stores_dir``).
"""

from __future__ import annotations

from pathlib import Path

from trellis.stores.base.event_log import Event, EventType
from trellis.stores.null.event_log import NullEventLog
from trellis.stores.registry import StoreRegistry


class TestNullEventLogNoOp:
    def test_emit_returns_event_but_persists_nothing(self) -> None:
        log = NullEventLog()
        event = log.emit(EventType.ENTITY_CREATED, source="test", entity_id="e1")
        # emit still returns a real, id-bearing event so callers that read
        # the returned ``event_id`` keep working...
        assert event.event_id is not None
        assert event.entity_id == "e1"
        # ...but nothing is stored.
        assert log.get_events() == []
        assert log.count() == 0

    def test_direct_append_is_noop(self) -> None:
        log = NullEventLog()
        log.append(Event(event_type=EventType.ENTITY_CREATED, source="test"))
        assert log.count() == 0
        assert log.get_events() == []

    def test_has_idempotency_key_always_false(self) -> None:
        log = NullEventLog()
        log.emit(
            EventType.MUTATION_EXECUTED,
            source="mutation_executor",
            payload={"idempotency_key": "k1"},
        )
        # A no-op log holds no history, so even a just-"emitted" key is unseen.
        assert log.has_idempotency_key("k1") is False

    def test_get_events_empty_with_filters(self) -> None:
        log = NullEventLog()
        assert log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=10) == []

    def test_count_with_filter_is_zero(self) -> None:
        log = NullEventLog()
        assert log.count(event_type=EventType.LINK_CREATED) == 0

    def test_close_does_not_raise(self) -> None:
        NullEventLog().close()


class TestNullBackendRegistration:
    def test_registry_resolves_null_backend(self, tmp_path: Path) -> None:
        reg = StoreRegistry(
            config={"event_log": {"backend": "null"}},
            stores_dir=tmp_path / "stores",
        )
        assert isinstance(reg.operational.event_log, NullEventLog)

    def test_null_event_log_needs_no_stores_dir(self) -> None:
        # The knowledge-plane-only case: a remote-graph deployment with no
        # local persistence resolves the operational event log without a
        # stores_dir (a sqlite event_log would raise ConfigError here).
        reg = StoreRegistry(config={"event_log": {"backend": "null"}})
        assert isinstance(reg.operational.event_log, NullEventLog)

    def test_validate_passes_for_null_backend(self, tmp_path: Path) -> None:
        # Fail-fast config validation (E.3) must accept the null backend.
        reg = StoreRegistry(
            config={"event_log": {"backend": "null"}},
            stores_dir=tmp_path / "stores",
        )
        reg.validate(store_types=["event_log"])  # no raise
