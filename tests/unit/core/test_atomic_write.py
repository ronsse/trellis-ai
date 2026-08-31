"""Tests for the shared atomic-write helper.

The helper was extracted from ``AdvisoryStore`` in #413 so ``PolicyStore``
did not become a fourth copy of the temp-file-plus-``os.replace`` pattern.
Its contract was previously pinned only *through* one of its callers, which
left the fresh-file mode — the thing that decides whether a bind-mounted
container reader can still read the file — asserted in one place, for the
store whose file matters least.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from trellis.core.atomic_write import NEW_FILE_MODE, atomic_write_text


class TestReplacementIsAtomic:
    def test_it_writes_the_text(self, tmp_path: Path) -> None:
        path = tmp_path / "f.json"
        atomic_write_text(path, '{"a": 1}')
        assert path.read_text(encoding="utf-8") == '{"a": 1}'

    def test_it_leaves_no_temp_files(self, tmp_path: Path) -> None:
        path = tmp_path / "f.json"
        atomic_write_text(path, "one")
        atomic_write_text(path, "two")
        assert [p.name for p in tmp_path.iterdir()] == ["f.json"]

    def test_a_failed_write_leaves_the_destination_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The property the whole pattern buys.

        ``write_text`` truncates and *then* writes, so a failure between the
        two is how the half-written file downstream code has to survive gets
        created in the first place.
        """
        path = tmp_path / "f.json"
        atomic_write_text(path, "original")

        def _boom(_fd: int) -> None:
            msg = "No space left on device"
            raise OSError(msg)

        monkeypatch.setattr("trellis.core.atomic_write.os.fsync", _boom)

        with pytest.raises(OSError, match="No space left"):
            atomic_write_text(path, "doomed")

        assert path.read_text(encoding="utf-8") == "original"
        assert [p.name for p in tmp_path.iterdir()] == ["f.json"]

    def test_cleanup_does_not_mask_the_real_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The caller must see the ENOSPC, not a temp-file complaint.

        The cleanup has to *fail* for this to mean anything. Without the
        second monkeypatch the temp file always exists in a writable
        directory, so ``suppress(OSError)`` and ``missing_ok=True`` can
        never fire and the assertion below just duplicates
        ``test_a_failed_write_leaves_the_destination_untouched``: measured,
        deleting the ``suppress`` left the entire suite green. Which is
        the shape this module exists to catch — a guard nothing can tell
        is there.
        """
        path = tmp_path / "f.json"

        def _boom(_fd: int) -> None:
            msg = "No space left on device"
            raise OSError(msg)

        def _cleanup_also_fails(*_args: object, **_kwargs: object) -> None:
            msg = "temp file complaint nobody asked about"
            raise OSError(msg)

        monkeypatch.setattr("trellis.core.atomic_write.os.fsync", _boom)
        monkeypatch.setattr(Path, "unlink", _cleanup_also_fails)

        # The ENOSPC the caller has to act on, not the cleanup's complaint
        # — a raise from a ``finally`` replaces the exception in flight.
        with pytest.raises(OSError, match="No space left"):
            atomic_write_text(path, "doomed")


class TestMode:
    def test_a_fresh_file_is_readable_by_other_uids(self, tmp_path: Path) -> None:
        """``mkstemp`` creates ``0600``; inheriting it would be a regression.

        The reference deployment bind-mounts the data directory into
        containers, so a fresh file at ``0600`` is a reader that never
        starts — and for ``policies.json`` that means an access-control file
        that cannot be read, which is the failure this whole area exists to
        prevent.
        """
        path = tmp_path / "f.json"
        atomic_write_text(path, "x")

        mode = stat.S_IMODE(path.stat().st_mode)
        # The literal, not ``NEW_FILE_MODE``. Asserting that a fresh file
        # gets the constant is a tautology over the constant: setting
        # ``NEW_FILE_MODE = 0o600`` — precisely the regression this
        # docstring is written against — left this test green, and the only
        # thing that caught it was ``test_advisory_store.py``. That is the
        # contract still being pinned *through a caller*, which is the
        # situation this module's extraction was supposed to end.
        assert NEW_FILE_MODE == 0o644
        assert mode == 0o644
        # And the property the number is chosen for, stated directly.
        assert mode & stat.S_IRGRP, "a bind-mounted container reader cannot read it"
        assert mode & stat.S_IROTH, "a bind-mounted container reader cannot read it"

    def test_an_existing_file_keeps_its_own_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "f.json"
        atomic_write_text(path, "x")
        path.chmod(0o640)

        atomic_write_text(path, "y")

        assert stat.S_IMODE(path.stat().st_mode) == 0o640

    def test_a_narrowed_file_is_not_silently_widened(self, tmp_path: Path) -> None:
        """Preservation runs in both directions, not just the safe one."""
        path = tmp_path / "f.json"
        atomic_write_text(path, "x")
        path.chmod(0o600)

        atomic_write_text(path, "y")

        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestSymlinks:
    def test_a_symlink_is_followed_not_replaced(self, tmp_path: Path) -> None:
        """``os.replace`` onto a symlink strands the target, permanently.

        A symlink is a plausible answer to the "move the file to the
        canonical path" advice both ``resolve_policy_path`` and
        ``resolve_advisory_path`` give, so the shape is reachable.
        ``write_text`` followed the link; this must keep that.
        """
        real = tmp_path / "real.json"
        real.write_text("before", encoding="utf-8")
        link = tmp_path / "link.json"
        link.symlink_to(real)

        atomic_write_text(link, "after")

        assert link.is_symlink()
        assert real.read_text(encoding="utf-8") == "after"

    def test_the_target_keeps_its_mode_through_a_symlink(self, tmp_path: Path) -> None:
        real = tmp_path / "real.json"
        real.write_text("before", encoding="utf-8")
        real.chmod(0o640)
        link = tmp_path / "link.json"
        link.symlink_to(real)

        atomic_write_text(link, "after")

        assert stat.S_IMODE(real.stat().st_mode) == 0o640


class TestDurability:
    def test_a_directory_fsync_failure_does_not_fail_the_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Best effort, deliberately.

        Some filesystems refuse to open a directory for fsync, and a
        durability improvement must not become a new way for a write to
        fail. The file's own bytes are already fsynced by this point.
        """
        path = tmp_path / "f.json"
        real_open = os.open

        def _selective_boom(target: object, flags: int, *args: object) -> int:
            if str(target) == str(tmp_path):
                msg = "cannot open directory"
                raise OSError(msg)
            return real_open(target, flags, *args)  # type: ignore[arg-type]

        monkeypatch.setattr("trellis.core.atomic_write.os.open", _selective_boom)

        atomic_write_text(path, "written anyway")

        assert path.read_text(encoding="utf-8") == "written anyway"
