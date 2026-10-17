"""Guard: no ``EventLog`` backend may skip the scrub seam in ``append``.

``EventLog.append`` (``src/trellis/stores/base/event_log.py``) is the one
choke point every write path -- a direct ``append(Event(...))`` and the
``emit()`` convenience helper alike -- runs through before a byte reaches
a backend. The design is a template method: ``append`` is concrete and
scrubs ``payload``/``metadata``/``entity_id``/``entity_type``/``source``,
then calls the abstract ``_append`` a backend defines instead.

``ABCMeta`` enforces that ``_append`` gets *an* implementation; it does
nothing to stop a subclass from also re-defining the *concrete*
``append`` and skipping the base class's scrub entirely. That is exactly
the gap this file closes by hand, per CLAUDE.md's instruction to take the
population floor from a hand read, never from the scan it guards
(``tests/ast_rules.py::assert_hand_read_floor``).

Hand read, 2026-10: ``_BUILTIN_BACKENDS["operational"]["event_log"]``
registers exactly three backends -- sqlite, postgres, null.
"""

from __future__ import annotations

import importlib

from tests.ast_rules import assert_hand_read_floor
from trellis.stores.base.event_log import EventLog

#: Hand-read population: sqlite, postgres, null. Bump this, with a fresh
#: hand count, when a fourth event_log backend is registered.
_HAND_READ_BACKEND_COUNT = 3


def _registered_event_log_backends() -> dict[str, tuple[str, str]]:
    from trellis.stores.registry import _BUILTIN_BACKENDS

    backend_dict: dict[str, tuple[str, str]] = _BUILTIN_BACKENDS["operational"][
        "event_log"
    ]
    return backend_dict


def _registered_event_log_classes() -> tuple[dict[str, type], list[str]]:
    """``(importable classes, names that could not be imported)``.

    An optional-driver backend (none today, under ``event_log`` -- but
    Postgres's own dependency, ``psycopg_pool``, is absent from this
    venv) is recorded rather than swallowed, matching
    ``test_vector_store_declarations.py``'s established pattern for the
    same registry.
    """
    classes: dict[str, type] = {}
    unimportable: list[str] = []
    for name, (module_path, class_name) in _registered_event_log_backends().items():
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            unimportable.append(name)
            continue
        classes[name] = getattr(module, class_name)
    return classes, unimportable


def test_every_registered_backend_is_an_event_log_subclass() -> None:
    backends = _registered_event_log_backends()
    assert_hand_read_floor(
        len(backends),
        _HAND_READ_BACKEND_COUNT,
        subject="registered operational.event_log backends",
    )
    classes, unimportable = _registered_event_log_classes()
    assert "sqlite" in classes, (classes, unimportable)
    for name, cls in classes.items():
        assert issubclass(cls, EventLog), (
            f"{name} backend {cls} is registered under operational.event_log "
            f"but is not an EventLog subclass"
        )


def test_no_registered_backend_overrides_append() -> None:
    """A subclass defining its own ``append`` bypasses the scrub entirely.

    This is the one guard standing between "a new backend gets the scrub
    for free" and "a new backend quietly reopens the Postgres NUL-byte /
    lone-surrogate gap this seam exists to close." Every registered
    backend must implement ``_append`` only and leave the inherited
    ``append`` template alone.
    """
    backends = _registered_event_log_backends()
    assert_hand_read_floor(
        len(backends),
        _HAND_READ_BACKEND_COUNT,
        subject="registered operational.event_log backends",
    )
    classes, unimportable = _registered_event_log_classes()
    assert "sqlite" in classes, (classes, unimportable)
    offending = sorted(
        name for name, cls in classes.items() if "append" in cls.__dict__
    )
    assert not offending, (
        f"backend(s) {offending} define their own `append` method, which "
        f"bypasses `EventLog.append`'s scrub-then-delegate template "
        f"entirely. Rename the override to `_append` instead -- "
        f"`EventLog.append` will call it after scrubbing."
    )


def test_append_is_concrete_and_append_underscore_is_abstract() -> None:
    """Pins the shape the two guards above assume.

    ``append`` must be callable on the base class itself (the template,
    not a stub) and ``_append`` must be the abstract seam a subclass is
    required to implement -- if this ever inverts, ``EventLog`` no longer
    forces every backend through the scrub, and the guards above would
    stop meaning anything.
    """
    assert "append" not in EventLog.__abstractmethods__
    assert "_append" in EventLog.__abstractmethods__
    assert callable(EventLog.append)
