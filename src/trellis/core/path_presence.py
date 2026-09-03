"""Ask whether a file is *there* without letting "unreadable" answer "no".

``Path.exists()`` — and its ``is_file()`` / ``is_dir()`` siblings — swallow
the errnos in ``pathlib._ignore_error``: ``ENOENT``, ``ENOTDIR``, ``EBADF``
and ``ELOOP``. All four come back as ``False``. So a file behind a symlink
loop, or under a path component that has become a regular file, presents as
*absent*, and every caller that reads absence as "nothing declared here"
quietly adopts its own safest-looking default: no policies, no advisories,
first-boot fingerprints. The path is broken; the deployment is told it is
empty.

That laundering has now been found three times in this repo — #444 in
:meth:`~trellis.stores.degradable_json_store.DegradableJsonStore.__init__`,
#471 in the same class's staleness guard, and #479 in
:mod:`trellis.mutate.policy_source`, where the default it launders into is
*zero access-control policies* and every governed mutation is permitted.
This module is the primitive those callers should have been sharing, so
there is one place the rule lives rather than one per module:

    **Absent is ``FileNotFoundError`` and nothing else.**

That is the same line :class:`DegradableJsonStore` draws (its
``_fingerprint`` returns ``None`` only for ``FileNotFoundError`` and an
:class:`~trellis.stores.degradable_json_store.UnknownFileIdentity` for
every other ``OSError``). It is not reimplemented in terms of this
function because it needs a *tri-state* — file / no file / don't know — and
collapsing that back to a bool is precisely the bug #471 fixed. Two shapes
of the one rule, deliberately.

``EACCES`` is worth calling out because it is the errno ``Path.exists()``
does *not* ignore. It re-raises, which fails closed but as a bare
``PermissionError`` traceback carrying none of the recovery advice a
caller's own error path provides. Routing it through here converts it into
whatever that caller already says about an unreadable file.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["path_is_present"]


def path_is_present(path: Path) -> bool:
    """Whether ``path`` names a file that exists, treating unreadable as yes.

    Returns ``False`` **only** when the path is genuinely absent
    (``FileNotFoundError``, i.e. ``ENOENT`` — which is also what a dangling
    symlink produces, since :meth:`Path.stat` follows links). Every other
    ``OSError`` — ``ELOOP``, ``ENOTDIR``, ``EACCES``, ``EIO`` — returns
    ``True``: something is there, this process cannot tell what, and
    "absent" is the one answer that is certainly wrong.

    The contract this puts on callers is that ``True`` means *go and read
    it*, not *the read will succeed*. A caller must already have a legible
    failure path for a file it cannot read — a ``ConfigError`` with recovery
    advice, or a degraded store that refuses writes — because that is where
    the real errno surfaces, with the operating system's own message. Do not
    swap a caller's presence check to this function unless that path exists;
    turning a silent empty default into an unhandled traceback is not an
    improvement, it is a different defect.
    """
    try:
        path.stat()
    except FileNotFoundError:
        return False
    # Anything else: the path is broken, unreachable, or forbidden, and none
    # of those is "no file here". Say present and let the caller's read
    # produce the real error.
    except OSError:
        return True
    return True
