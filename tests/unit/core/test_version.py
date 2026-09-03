"""Tests for :mod:`trellis.core.version` — build identity resolution."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from trellis.core import version as version_mod
from trellis.core.base import get_version
from trellis.core.version import (
    FALLBACK_SOURCE,
    FALLBACK_VERSION,
    STALENESS_FRESH,
    STALENESS_NOT_CHECKED,
    STALENESS_STALE,
    STALENESS_UNRESOLVED,
    UNKNOWN_VERSION,
    CodeVersion,
    StampStaleness,
    resolve_code_version,
    resolve_stamp_staleness,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Resolution is memoized per process; each test starts cold."""
    resolve_code_version.cache_clear()
    get_version.cache_clear()


class TestEditableInstall:
    """The shape this repo's own dev environment produces."""

    def test_resolves_from_installed_distribution_metadata(self) -> None:
        """The test run *is* an editable install — it must resolve."""
        resolved = resolve_code_version()
        assert resolved.source == "dist-metadata"
        assert resolved.version
        assert resolved.version != UNKNOWN_VERSION

    def test_editable_install_carries_a_commit(self) -> None:
        """``hatch-vcs`` puts the git sha in the PEP 440 local segment.

        Only asserted when the resolved version *has* a local segment: a
        run installed from a clean tag legitimately has none.
        """
        resolved = resolve_code_version()
        if "+" in resolved.version:
            assert resolved.commit is not None
            assert resolved.commit in resolved.version

    def test_resolution_is_cached_per_process(self) -> None:
        """The hot path must not re-read metadata per event."""
        assert resolve_code_version() is resolve_code_version()


class TestPackagedContext:
    """Simulated non-editable shapes, driven through the metadata reader."""

    @pytest.mark.parametrize(
        ("raw", "expected_commit", "expected_dirty"),
        [
            # Clean tagged release built into a wheel — no local segment.
            ("1.4.0", None, False),
            # Wheel built from a checkout between tags — what a container
            # image carries when the build passed TRELLIS_BUILD_VERSION.
            ("0.9.1.dev156+gd7c3e7ace", "d7c3e7ace", False),
            # Built from a dirty tree — setuptools-scm's node-and-date scheme.
            ("0.9.1.dev156+gd7c3e7ace.d20260802", "d7c3e7ace", True),
            # Explicit dirty marker (alternate local scheme).
            ("0.9.1+gabcdef1234.dirty", "abcdef1234", True),
            # Local segment with no recognisable sha at all.
            ("1.0.0+local", None, False),
        ],
    )
    def test_parses_local_segment(
        self,
        monkeypatch: pytest.MonkeyPatch,
        raw: str,
        expected_commit: str | None,
        expected_dirty: bool,
    ) -> None:
        monkeypatch.setattr(version_mod, "_version_from_metadata", lambda: raw)
        resolved = resolve_code_version()
        assert resolved == CodeVersion(
            version=raw,
            source="dist-metadata",
            commit=expected_commit,
            dirty=expected_dirty,
        )

    def test_unstamped_build_reports_that_it_cannot_identify_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A git-less build resolves to ``fallback-version``, not a version.

        The Docker build context excludes ``.git``, so hatch-vcs falls
        through to ``[tool.hatch.version] fallback-version`` and every
        image built without ``TRELLIS_BUILD_VERSION`` carries the same
        string. Reporting that as ``dist-metadata`` would make an
        unidentifiable image look like an identified one — the exact
        drift this module exists to surface.
        """
        monkeypatch.setattr(
            version_mod, "_version_from_metadata", lambda: FALLBACK_VERSION
        )
        assert resolve_code_version() == CodeVersion(
            version=FALLBACK_VERSION, source=FALLBACK_SOURCE, commit=None, dirty=False
        )

    def test_fallback_version_matches_pyproject(self) -> None:
        """The constant is a copy of build config; keep the copy honest."""
        pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
        hatch_version = tomllib.loads(pyproject.read_text())["tool"]["hatch"]["version"]
        assert hatch_version["fallback-version"] == FALLBACK_VERSION

    def test_falls_back_to_generated_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No distribution metadata, but a build wrote ``trellis._version``."""
        monkeypatch.setattr(version_mod, "_version_from_metadata", lambda: None)
        monkeypatch.setattr(version_mod, "_version_from_module", lambda: "2.0.0+gfeed")
        resolved = resolve_code_version()
        assert resolved.source == "generated-module"
        assert resolved.version == "2.0.0+gfeed"

    def test_unknown_when_neither_source_answers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running from an uninstalled source tree still yields a value."""
        monkeypatch.setattr(version_mod, "_version_from_metadata", lambda: None)
        monkeypatch.setattr(version_mod, "_version_from_module", lambda: None)
        resolved = resolve_code_version()
        assert resolved == CodeVersion(version=UNKNOWN_VERSION, source="unknown")

    def test_missing_distribution_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``PackageNotFoundError`` degrades, it does not propagate."""
        import importlib.metadata as md

        def _raise(_name: str) -> str:
            raise PackageNotFoundError(_name)

        monkeypatch.setattr(md, "version", _raise)
        monkeypatch.setattr(version_mod, "_version_from_module", lambda: None)
        assert resolve_code_version().version == UNKNOWN_VERSION


class TestGetVersionCompatibility:
    """``get_version`` keeps its published contract while gaining an answer."""

    def test_delegates_to_resolver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(version_mod, "_version_from_metadata", lambda: "3.1.4")
        assert get_version() == "3.1.4"

    def test_preserves_historical_sentinel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unresolvable still reports ``0.0.0-dev``, as it always has."""
        monkeypatch.setattr(version_mod, "_version_from_metadata", lambda: None)
        monkeypatch.setattr(version_mod, "_version_from_module", lambda: None)
        assert get_version() == "0.0.0-dev"


SHA_A = "a1b2c3d4e5" + "0" * 30
SHA_B = "f9e8d7c6b5" + "1" * 30


def _dist_metadata(commit: str | None) -> CodeVersion:
    """A ``CodeVersion`` as an editable install off a working tree yields."""
    return CodeVersion(
        version=f"0.9.1.dev1+g{commit}" if commit else "0.9.1",
        source="dist-metadata",
        commit=commit,
    )


class TestStampStalenessVerdict:
    """Has the source tree moved on since the metadata was written?"""

    @pytest.fixture(autouse=True)
    def _cold(self) -> None:
        resolve_stamp_staleness.cache_clear()

    def _pin(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        version: CodeVersion,
        tree: str | None = "/src/tree",
        head: str | None = SHA_A,
    ) -> dict[str, int]:
        """Patch the three seams and count how often the probe halves run."""
        calls = {"tree": 0, "head": 0}

        def _tree() -> str | None:
            calls["tree"] += 1
            return tree

        def _head(_tree: str) -> str | None:
            calls["head"] += 1
            return head

        monkeypatch.setattr(version_mod, "resolve_code_version", lambda: version)
        monkeypatch.setattr(version_mod, "_editable_source_tree", _tree)
        monkeypatch.setattr(version_mod, "_git_head", _head)
        return calls

    def test_stale_when_the_tree_moved_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._pin(monkeypatch, version=_dist_metadata(SHA_B[:9]), head=SHA_A)
        verdict = resolve_stamp_staleness()
        assert verdict.state == STALENESS_STALE
        assert verdict.is_stale is True
        assert verdict.source_tree_commit == SHA_A

    def test_fresh_when_head_extends_the_abbreviated_stamp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``hatch-vcs`` abbreviates; ``rev-parse`` does not.

        Comparing the two as equal strings would report every healthy
        editable install in the world as stale.
        """
        self._pin(monkeypatch, version=_dist_metadata(SHA_A[:9]), head=SHA_A)
        verdict = resolve_stamp_staleness()
        assert verdict.state == STALENESS_FRESH
        assert verdict.is_stale is False
        assert verdict.source_tree_commit == SHA_A

    def test_unresolved_when_git_cannot_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Nothing is wrong" and "I could not look" are different facts."""
        self._pin(monkeypatch, version=_dist_metadata(SHA_A[:9]), head=None)
        verdict = resolve_stamp_staleness()
        assert verdict.state == STALENESS_UNRESOLVED
        assert verdict.source_tree_commit is None

    def test_not_checked_when_the_install_is_not_editable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._pin(monkeypatch, version=_dist_metadata(SHA_A[:9]), tree=None)
        assert resolve_stamp_staleness().state == STALENESS_NOT_CHECKED
        assert calls["head"] == 0, "no git probe without a source tree"

    def test_not_checked_when_the_metadata_carries_no_sha(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tagged release has no local segment, so there is no sha."""
        calls = self._pin(monkeypatch, version=_dist_metadata(None))
        assert resolve_stamp_staleness().state == STALENESS_NOT_CHECKED
        assert calls == {"tree": 0, "head": 0}

    def test_a_container_image_never_runs_the_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An image has no ``.git`` and cannot drift — do not go looking.

        The fallback build is the shape every image built without
        ``make docker-build`` reports, and the one most likely to sit in a
        container with no git binary at all.
        """
        calls = self._pin(
            monkeypatch,
            version=CodeVersion(version=FALLBACK_VERSION, source=FALLBACK_SOURCE),
        )
        assert resolve_stamp_staleness().state == STALENESS_NOT_CHECKED
        assert calls == {"tree": 0, "head": 0}

    def test_a_stamped_image_never_runs_the_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``make docker-build`` gives an image a real sha and no tree."""
        calls = self._pin(
            monkeypatch, version=_dist_metadata(SHA_A[:9]), tree=None, head=SHA_B
        )
        assert resolve_stamp_staleness().state == STALENESS_NOT_CHECKED
        assert calls["head"] == 0

    def test_not_checked_for_a_generated_module_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Scope is the metadata clock; a ``_version`` module is not it.

        That build's sha was written into a file at build time by the same
        freeze, and nothing here knows which tree — if any — it came from.
        """
        calls = self._pin(
            monkeypatch,
            version=CodeVersion(
                version=f"0.9.1.dev1+g{SHA_A[:9]}",
                source="generated-module",
                commit=SHA_A[:9],
            ),
        )
        assert resolve_stamp_staleness().state == STALENESS_NOT_CHECKED
        assert calls == {"tree": 0, "head": 0}

    def test_resolved_once_per_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One ``git rev-parse`` per process — this is on the write path."""
        calls = self._pin(monkeypatch, version=_dist_metadata(SHA_A[:9]))
        for _ in range(5):
            resolve_stamp_staleness()
        assert calls["head"] == 1

    def test_cache_clear_re_reads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._pin(monkeypatch, version=_dist_metadata(SHA_A[:9]))
        resolve_stamp_staleness()
        resolve_stamp_staleness.cache_clear()
        resolve_stamp_staleness()
        assert calls["head"] == 2


class TestStampFields:
    """What the verdict contributes to an emitted event's stamp."""

    @pytest.mark.parametrize(
        "state", [STALENESS_FRESH, STALENESS_UNRESOLVED, STALENESS_NOT_CHECKED]
    )
    def test_contributes_nothing_unless_stale(self, state: str) -> None:
        """Silence is the healthy answer; a healthy stamp pays no bytes."""
        assert (
            StampStaleness(state=state, source_tree_commit=SHA_A).as_stamp_fields()
            == {}
        )

    def test_stale_names_the_live_sha(self) -> None:
        fields = StampStaleness(
            state=STALENESS_STALE, source_tree_commit=SHA_A
        ).as_stamp_fields()
        assert fields == {"stamp_stale": True, "source_tree_commit": SHA_A}

    def test_fields_are_flat_primitives(self) -> None:
        """``_copy_stamp`` copies one level; deeper nesting breaks it."""
        fields = StampStaleness(
            state=STALENESS_STALE, source_tree_commit=SHA_A
        ).as_stamp_fields()
        assert all(isinstance(v, (str, bool, int, type(None))) for v in fields.values())


class TestEditableSourceTree:
    """PEP 610 ``direct_url.json`` — editable-ness and tree in one read."""

    def _record(self, monkeypatch: pytest.MonkeyPatch, raw: str | None) -> None:
        import importlib.metadata as md

        class _Dist:
            def read_text(self, _name: str) -> str | None:
                return raw

        monkeypatch.setattr(md, "distribution", lambda _name: _Dist())

    def test_reads_the_editable_directory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._record(
            monkeypatch,
            '{"url": "file:///home/dev/trellis-ai", "dir_info": {"editable": true}}',
        )
        assert version_mod._editable_source_tree() == "/home/dev/trellis-ai"

    def test_percent_decodes_the_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``file://`` URI is not a path — parse it, do not slice it."""
        self._record(
            monkeypatch,
            '{"url": "file:///home/dev/my%20trees/t", "dir_info": {"editable": true}}',
        )
        assert version_mod._editable_source_tree() == "/home/dev/my trees/t"

    def test_none_when_the_directory_install_is_not_editable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``pip install ./trellis-ai`` copies the code; it cannot drift."""
        self._record(
            monkeypatch,
            '{"url": "file:///home/dev/trellis-ai", "dir_info": {"editable": false}}',
        )
        assert version_mod._editable_source_tree() is None

    def test_none_for_a_vcs_install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An install straight from a git URL has no local tree to read."""
        self._record(
            monkeypatch,
            '{"url": "https://example.invalid/t.git", "vcs_info": {"vcs": "git"}}',
        )
        assert version_mod._editable_source_tree() is None

    def test_none_for_an_editable_record_with_a_remote_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There is no local directory to read a ``HEAD`` out of."""
        self._record(
            monkeypatch,
            '{"url": "https://example.invalid/t.git", "dir_info": {"editable": true}}',
        )
        assert version_mod._editable_source_tree() is None

    def test_none_when_the_record_is_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``read_text`` returns ``None`` for a wheel — it does not raise."""
        self._record(monkeypatch, None)
        assert version_mod._editable_source_tree() is None

    def test_none_when_the_record_is_malformed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._record(monkeypatch, "{not json")
        assert version_mod._editable_source_tree() is None

    def test_none_when_the_distribution_is_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib.metadata as md

        def _raise(name: str) -> object:
            raise PackageNotFoundError(name)

        monkeypatch.setattr(md, "distribution", _raise)
        assert version_mod._editable_source_tree() is None

    def test_the_live_install_is_read_without_error(self) -> None:
        """Whatever this test run is installed as, the read must not raise."""
        tree = version_mod._editable_source_tree()
        assert tree is None or tree.startswith("/")


class TestGitHead:
    """The probe itself — advisory, bounded, and never an exception."""

    def test_reads_this_checkout(self) -> None:
        """The real command, against the repository the suite lives in.

        Everything else in this class is monkeypatched, so this is the
        only test that would notice ``capture_output`` or ``text`` going
        missing — both of which turn ``.strip()`` into an exception on a
        path that must never raise.
        """
        root = Path(__file__).resolve().parents[3]
        if not (root / ".git").exists():
            pytest.skip("not a git checkout")
        head = version_mod._git_head(str(root))
        assert head is not None
        assert version_mod._FULL_SHA.match(head)

    def test_none_outside_a_repository(self, tmp_path: Path) -> None:
        assert version_mod._git_head(str(tmp_path)) is None

    def test_none_when_git_is_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        def _missing(*_a: object, **_k: object) -> object:
            msg = "git"
            raise FileNotFoundError(msg)

        monkeypatch.setattr(subprocess, "run", _missing)
        assert version_mod._git_head("/anywhere") is None

    def test_none_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        def _slow(*_a: object, **_k: object) -> object:
            raise subprocess.TimeoutExpired(cmd="git", timeout=1.0)

        monkeypatch.setattr(subprocess, "run", _slow)
        assert version_mod._git_head("/anywhere") is None

    def test_passes_a_timeout_and_does_not_raise_on_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bounded, non-raising call — a stamp must never block a write."""
        import subprocess

        seen: dict[str, object] = {}

        def _spy(cmd: list[str], **kwargs: object) -> object:
            seen["cmd"] = cmd
            seen.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout=SHA_A + "\n", stderr="")

        monkeypatch.setattr(subprocess, "run", _spy)
        assert version_mod._git_head("/some/tree") == SHA_A
        assert seen["cmd"] == ["git", "-C", "/some/tree", "rev-parse", "HEAD"]
        assert isinstance(seen["timeout"], float)
        assert seen["check"] is False
        assert seen["capture_output"] is True
        assert seen["text"] is True

    def test_none_when_git_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed call's stdout is not an answer, whatever it holds."""
        import subprocess

        def _failed(cmd: list[str], **_k: object) -> object:
            return subprocess.CompletedProcess(cmd, 128, stdout=SHA_A, stderr="boom")

        monkeypatch.setattr(subprocess, "run", _failed)
        assert version_mod._git_head("/some/tree") is None

    def test_none_when_the_output_is_not_a_sha(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zero exit with a non-sha body is not an answer."""
        import subprocess

        def _weird(cmd: list[str], **_k: object) -> object:
            return subprocess.CompletedProcess(cmd, 0, stdout="HEAD\n", stderr="")

        monkeypatch.setattr(subprocess, "run", _weird)
        assert version_mod._git_head("/some/tree") is None
