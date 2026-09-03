"""One ``stat`` ladder, for every reader that must not shrug at a broken path.

``Path.exists()`` — and its ``is_file()`` / ``is_dir()`` siblings — swallow
the errnos in ``pathlib._ignore_error``: ``ENOENT``, ``ENOTDIR``, ``EBADF``
and ``ELOOP``. All four come back as ``False``. So a file behind a symlink
loop, or under a path component that has become a regular file, presents as
*absent*, and every caller that reads absence as "nothing declared here"
quietly adopts its own safest-looking default: no policies, no advisories,
first-boot fingerprints, default backends. The path is broken; the
deployment is told it is empty.

That laundering has been found four times in this repo — #444 in
:meth:`~trellis.stores.degradable_json_store.DegradableJsonStore.__init__`,
#471 in the same class's staleness guard, #479 in
:mod:`trellis.mutate.policy_source` (where the default it launders into is
*zero access-control policies* and every governed mutation is permitted),
and #479 again in ``StoreRegistry.from_config_dir``. Four fixes, and until
now two independent implementations of the same errno ladder. This module
is the one home:

    **Only ``FileNotFoundError`` is a claim about absence. Every other
    ``OSError`` means "something is there and this process cannot see
    what".**

Two questions, one ladder
-------------------------
:func:`file_identity` is the tri-state — *this file* / *no file* / *don't
know* — that a **staleness guard** needs. :func:`path_is_present` is the
bool a **reader** needs before it opens something. They are one function
and its projection rather than two implementations, because the revision
that will eventually be needed here ("should ``ESTALE`` on an NFS mount
read as absent?") must be made once, not once per module that happens to
remember the other exists.

The projection is not quite ``is not None``, and the exception is the one
place these two questions genuinely differ:

* ``stat`` **follows links**, so a *dangling symlink* raises ``ENOENT``
  exactly as an empty directory does.
* For a guard, that is correctly *no file*: the ordinary first write
  through a link creates its target (``atomic_write_text`` resolves the
  link on purpose), so answering "don't know" would refuse the first write
  on every symlinked deployment.
* For a reader, it is **not absence**. An operator who put a symlink at the
  policy path *declared a policy there*; a target that later moved must not
  silently return that deployment to zero policies. That is #479 surviving
  by its commonest route — a symlink *loop* needs two mutually-referential
  links, a dangling one needs a single ``rm`` — and ``atomic_write_text``
  calls a symlink here "a plausible answer" to the move-the-file advice
  :func:`~trellis.mutate.policy_source.resolve_policy_path` itself prints,
  so it is a supported shape rather than an exotic one.

So :func:`path_is_present` asks ``is_symlink()`` (an ``lstat``, which sees
what ``stat`` cannot) before conceding absence, and says so in one place
instead of the difference being an accident of two files.

``EACCES`` is worth calling out because it is the errno ``Path.exists()``
does *not* ignore. It re-raises, which fails closed but as a bare
``PermissionError`` traceback carrying none of the recovery advice a
caller's own error path provides. Routing it through here converts it into
whatever that caller already says about an unreadable file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "FileIdentity",
    "UnknownFileIdentity",
    "file_identity",
    "path_is_present",
]


@dataclass(frozen=True, slots=True, eq=False)
class UnknownFileIdentity:
    """``stat`` failed, so the file's identity is *unknown* — not absent.

    The distinction is the whole of #471. :func:`file_identity` used to
    answer ``None`` for both "there is no file" and "I could not look", and
    ``DegradableJsonStore.refuse_if_stale`` compares the fingerprint taken
    at load against one taken before the write. A store built while the
    path was absent (a normal fresh deployment) records ``None``; if a
    later ``stat`` also failed — ``EACCES`` from a parent that lost its
    execute bit, ``ELOOP`` from a symlink cycle, ``EIO`` or ``ESTALE`` from
    a network mount — it recorded ``None`` again, the two compared
    **equal**, and the compare-and-swap passed. The guard that stands
    between a stale in-memory view and #413's fail-open on access control
    disarmed itself precisely when it could not see the file.

    Only one of those two facts is safe to write over. An absent file is
    the ordinary first-write case and must stay writable, or every fresh
    install refuses. An unreadable one is a state this process cannot
    reason about, and ``refuse_if_stale`` refuses on it — cheaply, because
    that refusal is documented as transient and retryable, so the cost of
    being wrong is one retry against the cost of a silent whole-file
    rewrite.

    ``eq=False`` is load-bearing, not tidiness. With the generated
    ``__eq__`` two independently-derived records of the *same* failure —
    which is exactly what ``refuse_if_stale`` holds when a store's ``stat``
    keeps failing the same way — carry equal ``detail`` and compare equal,
    reproducing the defect verbatim one type later. Identity equality makes
    "unknown == unknown" impossible to write by accident. The explicit
    ``isinstance`` branches in the guard are the primary defence and this is
    the backstop; both are pinned by test, because a backstop nothing
    exercises is a comment.
    """

    #: ``"PermissionError: [Errno 13] ..."`` — the exception, for the operator.
    detail: str

    def __str__(self) -> str:
        return self.detail


#: What :func:`file_identity` answers with. Three distinct facts,
#: deliberately not two: a tuple is *this* file, ``None`` is *no* file, and
#: :class:`UnknownFileIdentity` is *don't know*.
FileIdentity = tuple[int, int, int] | None | UnknownFileIdentity


def file_identity(path: Path) -> FileIdentity:
    """Identity of the file at ``path``, or why it could not be taken.

    Three answers, never two (#471). A ``(st_ino, st_mtime_ns, st_size)``
    tuple is *this* file; ``None`` is *no* file, and only
    ``FileNotFoundError`` produces it; :class:`UnknownFileIdentity` is every
    other ``OSError`` — *don't know*. Collapsing the last two into ``None``
    is what let a compare-and-swap compare ``None`` against ``None`` and
    pass.

    ``st_ino`` is the load-bearing part: every write through
    :func:`~trellis.core.atomic_write.atomic_write_text` lands via
    ``os.replace`` from a fresh temp file, so a completed write by any
    process changes the inode even if size and mtime happen to collide.
    Size and mtime are kept because they catch the other shape — an
    in-place edit that keeps the inode, which is what ``sed -i`` and an
    editor configured to write through produce.

    Inode *reuse* — a replacement landing on the number a caller recorded,
    with mtime and size colliding too — is unreachable by construction
    rather than defended against: ``atomic_write_text`` creates its temp
    file while the target still exists, so the target's inode is never free
    to be handed back.

    ``None`` is a value, not the absence of one: a store built while the
    path was absent compares ``None`` against the fingerprint of a file
    that has since appeared, and refuses.

    ``FileNotFoundError`` is caught ahead of ``OSError`` and is the
    **only** shape read as absence. ``NotADirectoryError`` in particular is
    not: a path component that turned into a regular file is a broken path,
    not an empty deployment, and it is one of the errnos ``Path.exists``
    swallows — the #444 door, which this keeps shut on the guard side too.

    A *dangling symlink* answers ``None`` here, because ``stat`` follows
    links and there is genuinely no file to identify — and because writing
    through the link is the ordinary way its target gets created. That is
    the one point where :func:`path_is_present` deliberately parts company;
    see the module docstring.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return UnknownFileIdentity(f"{type(exc).__name__}: {exc}")
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def path_is_present(path: Path) -> bool:
    """Whether something is at ``path``, treating unreadable as yes.

    Returns ``False`` **only** when nothing is there at all — no file, and
    no link to one. Every ``OSError`` other than ``FileNotFoundError`` —
    ``ELOOP``, ``ENOTDIR``, ``EACCES``, ``EIO`` — returns ``True``:
    something is there, this process cannot tell what, and "absent" is the
    one answer that is certainly wrong.

    ``file_identity(path) is not None`` is *almost* the whole definition,
    and the gap is the deliberate one. ``stat`` follows links, so a
    **dangling symlink** produces ``None`` there; a link is nonetheless
    something an operator placed, so it answers ``True`` here and reaches
    the caller's read — which raises the same legible ``FileNotFoundError``
    every other unreadable shape does. ``is_symlink()`` is the ``lstat``
    that sees what ``stat`` cannot; it swallows its own errors, which is
    right, because a path whose parent is unreadable never reaches this arm.

    The contract this puts on callers is that ``True`` means *go and read
    it*, not *the read will succeed*. A caller must already have a legible
    failure path for a file it cannot read — a ``ConfigError`` with recovery
    advice, or a degraded store that refuses writes — because that is where
    the real errno surfaces, with the operating system's own message. Do not
    swap a caller's presence check to this function unless that path exists;
    turning a silent empty default into an unhandled traceback is not an
    improvement, it is a different defect.
    """
    if file_identity(path) is None:
        return path.is_symlink()
    return True
