"""Replace a file's contents in one step, durably.

The one home for the temp-file-plus-``os.replace`` pattern the JSON-backed
stores use. ``Path.write_text`` truncates the destination and *then*
writes, so a crash, a full disk or a killed cron between the two leaves a
half-written file — and for a store that whole-file-rewrites, that
half-written file is the corrupt state everything downstream then has to
survive. Closing that window closes the main way the state gets created.

Lifted out of :mod:`trellis.stores.advisory_store` (#393/#414), which named
the extraction as a follow-up, so :mod:`trellis.stores.policy_store` (#413)
did not become a fourth copy. It is deliberately *not* a drop-in for the
two watermark writers (``trace_embed/watermark.py``,
``session_capture/watermark.py``): those swallow ``OSError`` and continue,
where this propagates. Converting them is a separate decision about their
failure posture, not a refactor.
"""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

#: Mode for a *newly created* file. Matches what ``write_text`` produced
#: under the common ``umask 022`` before writes became atomic — ``mkstemp``
#: creates ``0600``, and silently narrowing a live file would break a
#: container reader bind-mounting it under a different uid. When the
#: destination already exists its own mode is preserved instead.
#:
#: It is a constant, not ``0o666 & ~umask``: reading the umask means
#: setting it, which is not safe in the threaded API process. The trade is
#: that under a restrictive umask (``077``) this *widens* a new file, where
#: ``write_text`` would have produced ``0600``. Accepted, but it is a
#: change of behaviour in both directions, not only the narrowing one.
NEW_FILE_MODE = 0o644


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path``'s contents in one step, preserving its mode.

    ``mkstemp`` creates ``0600``. Inheriting that would silently narrow a
    live file another uid reads — the reference deployment bind-mounts the
    data directory into containers — so an existing destination's mode is
    copied onto the temp file and a fresh one gets :data:`NEW_FILE_MODE`.

    **Symlinks are followed, deliberately.** ``os.replace`` onto a symlink
    leaves a regular file where the link was and strands the target,
    silently and permanently — and a symlink is a plausible answer to the
    "move the file to the canonical path" advice both
    :func:`~trellis.stores.advisory_source.resolve_advisory_path` and
    :func:`~trellis.mutate.policy_source.resolve_policy_path` give.
    ``write_text`` followed the link; this keeps that.

    One limit worth naming: ``os.replace`` cannot rename onto a
    *single-file* bind mount (``EBUSY``). The reference deployment mounts
    the data directory rather than the file, so this is not hit today — a
    deployment that mounts the file itself must switch to mounting the
    directory.
    """
    target = path.resolve() if path.is_symlink() else path
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            # Not an ``exists()`` test first: that is a TOCTOU on the mode
            # read, and a fresh file is the normal case, not an error.
            mode = NEW_FILE_MODE
        tmp_path.chmod(mode)
        tmp_path.replace(target)
        replaced = True
    finally:
        if not replaced:
            # Best effort. A raise from the cleanup would replace the real
            # exception — the ENOSPC the caller has to see — with a
            # FileNotFoundError about a temp file nobody asked about.
            with suppress(OSError):
                tmp_path.unlink(missing_ok=True)

    fsync_directory(target.parent)


def fsync_directory(directory: Path) -> None:
    """Commit a rename to disk, best effort.

    ``os.replace`` is atomic but not durable: on a crash the rename can be
    lost even though the file's own bytes were fsynced. One ``fsync`` on the
    directory closes that. Best effort, because some filesystems refuse to
    open a directory for fsync and a durability improvement must not become
    a new way for a write to fail.
    """
    with suppress(OSError):
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
