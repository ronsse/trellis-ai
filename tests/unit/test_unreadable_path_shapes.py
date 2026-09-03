"""The roster of unreadable-path shapes is checked before anything uses it.

Every suite that asserts something about #479 parametrises over
:data:`tests.unreadable_paths.UNREADABLE_PATH_SHAPES`, so a shape that
quietly stopped producing the errno it claims would not fail — it would
make the parametrisation *uniform*, and an assertion satisfiable by one
constant would pass on every row. That is the fixture failure #447 and
#456 both turned out to be, so the table gets its own tests before it is
trusted anywhere else.

This file also pins the two premises the whole fix rests on:

* ``Path.exists()`` really does launder ``ELOOP`` and ``ENOTDIR`` into
  ``False`` while re-raising ``EACCES`` — the asymmetry the issue reported.
  If a future CPython changes ``pathlib._ignore_error``, this says so here
  rather than through a confusing failure three modules away.
* :func:`trellis.core.path_presence.path_is_present` answers ``True`` for
  every one of them and ``False`` only for a genuinely absent path. Getting
  that backwards is the one way this change could break every deployment
  that has never written a policy, so it is asserted directly.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from tests.unreadable_paths import (
    UNREADABLE_PATH_IDS,
    UNREADABLE_PATH_SHAPES,
    UnreadablePathShape,
    unreadable,
)
from trellis.core.path_presence import path_is_present

#: A floor, not an equality: adding a shape must not require editing this,
#: but losing one — or two collapsing onto the same errno — must fail. The
#: number is "enough distinct errnos that an assertion cannot be a constant".
_MIN_DISTINCT_ERRNOS = 3


@pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
def test_shape_produces_the_errno_it_declares(
    shape: UnreadablePathShape, tmp_path: Path
) -> None:
    """Asserted, not assumed. A shape that no longer bites is invisible."""
    target = tmp_path / "dir" / "file.json"
    with unreadable(shape, target):
        with pytest.raises(OSError) as exc_info:
            target.stat()
        assert errno.errorcode[exc_info.value.errno] == shape.errno_name


def test_the_shapes_cover_distinct_errnos() -> None:
    """The anti-uniformity control.

    Several shapes provoking one errno would let a test assert the error
    *type* and pass by constant on every row. The table's value is that the
    rows differ.
    """
    errnos = {shape.errno_name for shape in UNREADABLE_PATH_SHAPES}
    assert len(errnos) == len(UNREADABLE_PATH_SHAPES), (
        f"two shapes share an errno: {sorted(errnos)}"
    )
    assert len(errnos) >= _MIN_DISTINCT_ERRNOS


def test_message_fragments_are_distinct_and_nonempty() -> None:
    """The vacuity guard on the guard.

    Callers assert ``shape.message_fragment in <the error>``, and ``"" in
    anything`` is ``True`` — so an empty or shared fragment would turn every
    one of those per-shape assertions into a constant that no wrong error
    could fail. This is the mutant that survives everything else.
    """
    fragments = [shape.message_fragment for shape in UNREADABLE_PATH_SHAPES]
    assert all(len(f) > len("no") for f in fragments), fragments
    assert len(set(fragments)) == len(fragments), fragments


@pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
def test_message_fragment_is_what_the_os_actually_says(
    shape: UnreadablePathShape, tmp_path: Path
) -> None:
    """Callers assert this fragment reaches their error, so it must be real."""
    target = tmp_path / "dir" / "file.json"
    with unreadable(shape, target):
        with pytest.raises(OSError) as exc_info:
            target.stat()
        assert shape.message_fragment in str(exc_info.value)


@pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
def test_path_exists_launders_exactly_the_declared_errnos(
    shape: UnreadablePathShape, tmp_path: Path
) -> None:
    """The premise of #479, pinned against the standard library.

    ``exists()`` returning ``False`` for a path that is demonstrably *there*
    is the whole defect. ``EACCES`` re-raising is why it was reachable for
    some operator mistakes and not others.
    """
    target = tmp_path / "dir" / "file.json"
    with unreadable(shape, target):
        if shape.swallowed_by_exists:
            assert target.exists() is False
        else:
            with pytest.raises(OSError):
                target.exists()


@pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
def test_path_is_present_says_present_for_every_broken_shape(
    shape: UnreadablePathShape, tmp_path: Path
) -> None:
    target = tmp_path / "dir" / "file.json"
    with unreadable(shape, target):
        assert path_is_present(target) is True


def test_path_is_present_says_absent_for_a_genuinely_absent_file(
    tmp_path: Path,
) -> None:
    """The control that keeps the fix from being "raise on everything".

    Zero policies is a legitimate declared posture and no file at all is the
    shipped default. If this ever returns ``True``, every deployment that
    has never written a policy starts failing every write.
    """
    assert path_is_present(tmp_path / "nothing-here.json") is False


def test_a_dangling_symlink_is_absent_not_unreadable(tmp_path: Path) -> None:
    """``ENOENT`` by another route, and deliberately on the absent side.

    ``Path.stat()`` follows links, so a symlink to a missing file raises
    ``FileNotFoundError`` — the same answer ``DegradableJsonStore._fingerprint``
    gives it (#482). Recorded here because "dangling symlink" reads like a
    broken path and lands on the other side of the line from a symlink
    *loop*, which is a difference worth being explicit about.
    """
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "missing.json")
    assert path_is_present(link) is False
