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
    UNKNOWN_VERSION,
    CodeVersion,
    resolve_code_version,
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
