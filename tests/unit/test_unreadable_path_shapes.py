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
from trellis.core.path_presence import (
    UnknownFileIdentity,
    file_identity,
    path_is_present,
)

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


def test_a_dangling_symlink_is_present_not_absent(tmp_path: Path) -> None:
    """``ENOENT`` by a second route, and *not* absence.

    ``Path.stat()`` follows links, so a symlink whose target is gone raises
    ``FileNotFoundError`` exactly as an empty directory does — which would
    put it on the absent side of a ``FileNotFoundError``-only rule and leave
    #479 open by its commonest route. A link is something an operator placed
    there; a target that later moved must not silently return the deployment
    to zero policies. ``is_symlink()`` is the ``lstat`` that tells the two
    ``ENOENT``s apart.
    """
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "missing.json")
    assert path_is_present(link) is True


def test_the_two_enoents_are_told_apart_and_not_by_one_of_them(
    tmp_path: Path,
) -> None:
    """The anti-constant control for the test above.

    Both a dangling link and an empty directory raise ``FileNotFoundError``
    from ``stat``, so a mutant answering that arm with a constant satisfies
    exactly one of the two. Asserted together here so the *difference* is
    the assertion rather than either value on its own.
    """
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "missing.json")
    nothing = tmp_path / "nothing.json"

    with pytest.raises(FileNotFoundError):
        link.stat()
    with pytest.raises(FileNotFoundError):
        nothing.stat()

    assert path_is_present(link) != path_is_present(nothing)
    assert path_is_present(link) is True


def test_a_symlink_to_a_real_file_is_present(tmp_path: Path) -> None:
    """The unbroken case, so "present" is not carried by the link alone."""
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    assert path_is_present(link) is True


def test_an_absent_path_under_an_absent_parent_is_absent(tmp_path: Path) -> None:
    """A missing parent must not be read as "something is there".

    ``is_symlink()`` swallows its own ``OSError``, so the concern is that a
    path nothing exists at could answer ``True`` through it. It cannot: the
    ``lstat`` fails and the answer is ``False``, which keeps a fresh install
    (no data dir yet) on the silent, zero-policy default.
    """
    assert path_is_present(tmp_path / "no-dir" / "nope" / "policies.json") is False


# ---------------------------------------------------------------------------
# One ladder, two questions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
def test_the_two_readers_agree_on_every_unreadable_shape(
    shape: UnreadablePathShape, tmp_path: Path
) -> None:
    """``path_is_present`` is a projection of ``file_identity``, not a copy.

    They were two independent implementations of one errno ladder until this
    change folded them together. This asserts the projection holds where it
    is supposed to: every shape that is "don't know" for the guard is
    "present" for the reader.
    """
    target = tmp_path / "dir" / "file.json"
    with unreadable(shape, target):
        assert isinstance(file_identity(target), UnknownFileIdentity)
        assert path_is_present(target) is True


def test_the_one_place_the_two_readers_part_company(tmp_path: Path) -> None:
    """A dangling symlink, and the divergence is deliberate — so it is pinned.

    The guard must answer *no file*: writing through a link is how its
    target gets created, and answering "don't know" would refuse the first
    write on every symlinked deployment. The reader must answer *present*: a
    link is a declaration, and reading it as absence is #479 surviving by
    its commonest route.

    Asserted together, in one test, because a divergence recorded in two
    files is how the two implementations drifted apart in the first place.
    """
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "missing.json")

    assert file_identity(link) is None
    assert path_is_present(link) is True


def test_the_projection_holds_for_a_real_file_and_for_nothing_at_all(
    tmp_path: Path,
) -> None:
    """The two ends, so the divergence above is the *only* one."""
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    assert isinstance(file_identity(real), tuple)
    assert path_is_present(real) is True

    nothing = tmp_path / "nothing.json"
    assert file_identity(nothing) is None
    assert path_is_present(nothing) is False
