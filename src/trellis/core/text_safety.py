"""Whether a string holds a byte no backend-agnostic text column can store.

Several unrelated call sites need the exact same answer to the exact same
question -- does this ``str`` contain a NUL byte or an unpaired UTF-16
surrogate -- before trusting it into a Postgres ``jsonb`` column, a CLI
argument, or an audit-trail claim. A Python ``str`` is a sequence of
Unicode code points, not UTF-16 code units, so a surrogate code point
(U+D800 to U+DFFF) needs no missing partner to exist, yet neither hazard
can round-trip through Postgres: a NUL byte raises
``UntranslatableCharacter`` and a lone surrogate cannot be encoded to
UTF-8 at all. SQLite's text columns accept both silently, which is why a
payload carrying either only ever fails in a Postgres deployment, never
in the SQLite-backed test suite.

:func:`contains_nul_or_lone_surrogate` is that one test. Keeping it here,
rather than on :mod:`trellis.mutate.handlers` where it originated, lets a
CLI argument guard (:mod:`trellis_cli.classify`) and a REST audit-trail
scrubber (:mod:`trellis_api.routes._principal`) import it without pulling
in the mutation-handler layer for a predicate that has nothing to do with
mutations.
"""

from __future__ import annotations

from typing import Any

_SURROGATE_RANGE_LOW = 0xD800
_SURROGATE_RANGE_HIGH = 0xDFFF

#: Stands in for a replaced NUL byte or lone surrogate: a visible
#: character, so the result keeps the original's length and positions.
REPLACEMENT_CHAR = "\ufffd"


def _is_unsafe_char(ch: str) -> bool:
    return ch == "\x00" or _SURROGATE_RANGE_LOW <= ord(ch) <= _SURROGATE_RANGE_HIGH


def contains_nul_or_lone_surrogate(value: str) -> bool:
    """True if ``value`` has a NUL byte or an unpaired UTF-16 surrogate."""
    return any(_is_unsafe_char(ch) for ch in value)


def replace_nul_and_lone_surrogates(value: str) -> str:
    """``value`` with each NUL byte and lone surrogate replaced by
    :data:`REPLACEMENT_CHAR`; ``value`` itself when it holds neither."""
    if not contains_nul_or_lone_surrogate(value):
        return value
    return "".join(REPLACEMENT_CHAR if _is_unsafe_char(ch) else ch for ch in value)


def scrub_jsonb_unsafe(value: Any) -> Any:
    """Recursively replace NUL bytes and lone surrogates in ``value``.

    Walks ``str``, ``dict`` (keys and values) and ``list``/``tuple``
    structures -- the shapes a JSON-serializable ``Event.payload`` or
    ``Event.metadata`` can take -- and leaves every other type (``int``,
    ``bool``, ``None``, a datetime already converted elsewhere)
    untouched. A ``tuple`` comes back as a ``list``: both encode to the
    same JSON array, and a namedtuple's extra attributes have no slot in
    a JSON array either, so round-tripping one loses nothing a store
    would have kept.

    This is the one copy call sites share: :mod:`trellis.mutate.executor`
    imports it under its pre-existing private name so its tests keep
    pinning the behaviour without a second implementation to drift from,
    and :class:`~trellis.stores.base.event_log.EventLog` calls it from
    its ``append`` template so every backend gets it for free.
    """
    if isinstance(value, str):
        return replace_nul_and_lone_surrogates(value)
    if isinstance(value, dict):
        return {
            scrub_jsonb_unsafe(key): scrub_jsonb_unsafe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub_jsonb_unsafe(item) for item in value]
    return value


__all__ = [
    "REPLACEMENT_CHAR",
    "contains_nul_or_lone_surrogate",
    "replace_nul_and_lone_surrogates",
    "scrub_jsonb_unsafe",
]
