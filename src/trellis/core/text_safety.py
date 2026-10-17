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

_SURROGATE_RANGE_LOW = 0xD800
_SURROGATE_RANGE_HIGH = 0xDFFF


def contains_nul_or_lone_surrogate(value: str) -> bool:
    """True if ``value`` has a NUL byte or an unpaired UTF-16 surrogate."""
    return any(
        ch == "\x00" or _SURROGATE_RANGE_LOW <= ord(ch) <= _SURROGATE_RANGE_HIGH
        for ch in value
    )


__all__ = ["contains_nul_or_lone_surrogate"]
