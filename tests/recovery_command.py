"""The ``mv`` command a degraded ``DegradableJsonStore`` recovery advises.

:attr:`~trellis.stores.degradable_json_store.LoadDegradation.recovery`
``shlex.quote``s both operands (#427): an unquoted ``mv`` word-splits on a
data directory containing a space — ``~/Library/Application Support/…`` —
into four operands rather than two. A test that builds its *expected*
string with bare interpolation, ``f"mv {path} {path}.corrupt"``, agrees
with the code only because pytest's default ``tmp_path`` has no space in
it; give it a ``--basetemp`` that does and the same assertion reports the
very quoting #427 added as a regression.

:func:`expected_recovery` rebuilds the string with the standard-library
call the property itself uses, not by importing the property or a helper
out of ``degradable_json_store`` — a test that re-derives the answer from
``shlex.quote`` stays an independent check on the code, not a tautology
against it.
"""

from __future__ import annotations

import shlex
from pathlib import Path


def expected_recovery(path: Path) -> str:
    """The exact string ``LoadDegradation.recovery`` builds for ``path``."""
    quoted = shlex.quote(str(path))
    return f"mv {quoted} {shlex.quote(str(path) + '.corrupt')}"
