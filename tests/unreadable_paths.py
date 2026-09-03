"""The shapes an *unreadable path* takes, for the readers that must not shrug.

``Path.exists()`` swallows the errnos in ``pathlib._ignore_error`` —
``ENOENT``, ``ENOTDIR``, ``EBADF``, ``ELOOP`` — and answers ``False`` to
all of them, so a broken path is reported as an absent one and every reader
that treats absence as "nothing declared" adopts its own benign default.
For :mod:`trellis.mutate.policy_source` that default is *zero
access-control policies* (#479); for :mod:`trellis.stores.advisory_source`
it is "this deployment has never generated an advisory" (#373's shape
again); for ``StoreRegistry._load_fingerprint_meta`` it is "first boot".

This module is the one roster of those shapes, so the suites that assert
**opposite** things about them — enforcement raises, display degrades —
argue over the same table rather than each inventing its own. That is the
same reason :mod:`tests.degradable_shapes` exists, and the two are
complementary: that one damages a file's *contents*, this one damages the
*path*, and only the first ever reaches ``read_text``.

Every shape here is **root-independent by construction** except
``unsearchable_parent``, which probes rather than assuming: ``chmod 000``
is a no-op for uid 0, so the shape verifies that the mode actually took
effect and skips otherwise. A shape that silently degenerated into a
different errno would make the parametrisation uniform, which is exactly
the fixture failure this file is trying to avoid — so
``tests/unit/test_unreadable_path_shapes.py`` pins each shape's errno and
pins that they are all *different*.

``EBADF`` has no member. It is in the swallowed set, but a ``Path`` naming
a bad file descriptor is not something an operator's filesystem produces —
it needs ``/dev/fd/<n>`` or a path built from a closed descriptor — so
there is nothing to construct that a deployment could ever hit.
"""

from __future__ import annotations

import errno
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest


@dataclass(frozen=True)
class UnreadablePathShape:
    """One way for a path to exist-but-not-be-readable.

    Attributes:
        id: Parametrisation id, so a failure names the shape.
        errno_name: The errno the OS must actually raise on ``stat``.
            Asserted, not assumed — see the module docstring.
        message_fragment: A distinctive part of the operating system's own
            strerror for that errno. Callers assert it reaches the error a
            reader produces, which makes the assertion specific to *this*
            shape rather than satisfiable by any error at all.
        swallowed_by_exists: Whether ``Path.exists()`` reports it as
            ``False`` (the laundering) or re-raises it. ``EACCES`` is the
            one that re-raises, which is why the bug was reachable for some
            errnos and not others.
        make: Arranges the filesystem so ``stat`` on the target raises.
        restore: Undoes it, so ``tmp_path`` teardown can remove the tree.
    """

    id: str
    errno_name: str
    message_fragment: str
    swallowed_by_exists: bool
    make: Callable[[Path], None]
    restore: Callable[[Path], None] = field(default=lambda _path: None)


def _make_symlink_loop(target: Path) -> None:
    """``target`` and a sibling point at each other. Pure ``ELOOP``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    sibling = target.with_name(target.name + ".loop")
    if sibling.is_symlink() or sibling.exists():
        sibling.unlink()
    target.symlink_to(sibling)
    sibling.symlink_to(target)


def _restore_symlink_loop(target: Path) -> None:
    sibling = target.with_name(target.name + ".loop")
    for link in (target, sibling):
        if link.is_symlink():
            link.unlink()


def _make_not_a_directory(target: Path) -> None:
    """``target``'s parent becomes a regular file. ``ENOTDIR``.

    The operator shape is a path component that got clobbered — a ``>``
    redirect onto a directory name, a restore that wrote a tarball where a
    directory belonged.
    """
    parent = target.parent
    if parent.is_dir():
        shutil.rmtree(parent)
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("this is a regular file, not a directory\n", encoding="utf-8")


def _restore_not_a_directory(target: Path) -> None:
    parent = target.parent
    if parent.is_file():
        parent.unlink()


def _make_unsearchable_parent(target: Path) -> None:
    """``target``'s parent loses execute permission. ``EACCES``.

    Skipped rather than assumed when the mode does not bite — ``chmod 000``
    is a no-op for uid 0, and a shape that silently does nothing is worse
    than one that is absent, because it quietly duplicates another shape.
    The check is a *probe*, not a ``geteuid`` test: an unprivileged uid in a
    container with ``CAP_DAC_OVERRIDE`` is equally unaffected.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")
    target.parent.chmod(0o000)
    try:
        target.stat()
    except OSError:
        return
    target.parent.chmod(0o700)
    pytest.skip("chmod does not restrict this process (root or CAP_DAC_OVERRIDE)")


def _restore_unsearchable_parent(target: Path) -> None:
    if target.parent.exists():
        target.parent.chmod(0o700)


#: Every shape a reader must not mistake for an absent file.
UNREADABLE_PATH_SHAPES: list[UnreadablePathShape] = [
    UnreadablePathShape(
        id="symlink_loop",
        errno_name="ELOOP",
        message_fragment=os.strerror(errno.ELOOP),
        swallowed_by_exists=True,
        make=_make_symlink_loop,
        restore=_restore_symlink_loop,
    ),
    UnreadablePathShape(
        id="not_a_directory",
        errno_name="ENOTDIR",
        message_fragment=os.strerror(errno.ENOTDIR),
        swallowed_by_exists=True,
        make=_make_not_a_directory,
        restore=_restore_not_a_directory,
    ),
    UnreadablePathShape(
        id="unsearchable_parent",
        errno_name="EACCES",
        message_fragment=os.strerror(errno.EACCES),
        swallowed_by_exists=False,
        make=_make_unsearchable_parent,
        restore=_restore_unsearchable_parent,
    ),
]

#: Parametrisation ids, so a failure names the shape rather than an index.
UNREADABLE_PATH_IDS: list[str] = [shape.id for shape in UNREADABLE_PATH_SHAPES]


@contextmanager
def unreadable(shape: UnreadablePathShape, target: Path) -> Iterator[Path]:
    """Break ``target`` per ``shape`` for the block, then put it back.

    Yields ``target`` unchanged: the point is that the *caller's* path is
    the broken one, so nothing under test has to be told which shape it is
    dealing with.
    """
    shape.make(target)
    try:
        yield target
    finally:
        shape.restore(target)
