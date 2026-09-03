"""Tests for advisory-file resolution — one file, and never a silent ``None``.

Two properties carry the whole of #373.

:class:`TestSurfacesAgreeOnOneFile` is the regression test proper. Writers
resolved ``<data_dir>/advisories.json`` and readers resolved
``<stores_dir>/advisories.json``; both halves worked, the nightly worker
reported success every night for 17 days, and no advisory it produced was
ever readable by a pack. These tests fail if any surface picks a path
locally again.

:class:`TestReaderNeverBindsNoneSilently` covers the *mechanism* that made
that cost weeks rather than minutes. The readers guarded on
``if adv_path.exists()``, so a misresolved path fell through into
``advisory_store = None`` — which PackBuilder treats as "this deployment
has no advisories". A wrong directory and an empty deployment were
indistinguishable. These tests fail if that guard, or anything with its
shape, comes back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from tests.unreadable_paths import (
    UNREADABLE_PATH_IDS,
    UNREADABLE_PATH_SHAPES,
    UnreadablePathShape,
    unreadable,
)
from trellis.errors import DegradedStoreWriteError
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.stores.advisory_source import (
    ADVISORY_FILENAME,
    load_advisory_store,
    resolve_advisory_path,
)
from trellis.stores.advisory_store import AdvisoryStore


def _advisory(advisory_id: str = "adv_1", *, confidence: float = 0.5) -> Advisory:
    return Advisory(
        advisory_id=advisory_id,
        category=AdvisoryCategory.APPROACH,
        confidence=confidence,
        message="test advisory",
        evidence=AdvisoryEvidence(
            sample_size=5,
            success_rate_with=0.6,
            success_rate_without=0.2,
            effect_size=0.4,
        ),
        scope="global",
    )


def _write_advisories(path: Path, advisories: list[Advisory]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"advisories": [a.model_dump(mode="json") for a in advisories]}),
        encoding="utf-8",
    )


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    """``(data_dir, stores_dir)`` with the real production relationship."""
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    return data_dir, stores_dir


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestResolveAdvisoryPath:
    def test_canonical_path_is_under_stores_dir(self, dirs: tuple[Path, Path]) -> None:
        _data_dir, stores_dir = dirs
        assert resolve_advisory_path(stores_dir) == stores_dir / ADVISORY_FILENAME

    def test_existing_canonical_file_wins(self, dirs: tuple[Path, Path]) -> None:
        data_dir, stores_dir = dirs
        _write_advisories(stores_dir / ADVISORY_FILENAME, [_advisory()])
        _write_advisories(data_dir / ADVISORY_FILENAME, [_advisory("adv_legacy")])
        assert resolve_advisory_path(stores_dir) == stores_dir / ADVISORY_FILENAME

    def test_legacy_file_is_honoured(self, dirs: tuple[Path, Path]) -> None:
        """The reference deployment's live file is at the legacy path.

        It is written nightly by cron. Resolution must find it where it is
        — #373 explicitly forbids moving or rewriting it.
        """
        data_dir, stores_dir = dirs
        _write_advisories(data_dir / ADVISORY_FILENAME, [_advisory()])
        assert resolve_advisory_path(stores_dir) == data_dir / ADVISORY_FILENAME

    def test_legacy_hit_warns_naming_both_paths(self, dirs: tuple[Path, Path]) -> None:
        data_dir, stores_dir = dirs
        _write_advisories(data_dir / ADVISORY_FILENAME, [_advisory()])

        with capture_logs() as cap:
            resolve_advisory_path(stores_dir)

        warnings = [e for e in cap if e["event"] == "advisory_file_at_legacy_path"]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        # Both paths, so an operator can act without reading the source.
        assert warnings[0]["legacy_path"] == str(data_dir / ADVISORY_FILENAME)
        assert warnings[0]["canonical_path"] == str(stores_dir / ADVISORY_FILENAME)

    def test_no_file_anywhere_returns_canonical(self, dirs: tuple[Path, Path]) -> None:
        """So a fresh deployment's first write creates the right file."""
        _data_dir, stores_dir = dirs
        assert resolve_advisory_path(stores_dir) == stores_dir / ADVISORY_FILENAME

    def test_none_stores_dir_returns_none(self) -> None:
        assert resolve_advisory_path(None) is None

    def test_resolution_never_moves_or_writes(self, dirs: tuple[Path, Path]) -> None:
        """#373 constraint 2: migration is the operator's call, not ours."""
        data_dir, stores_dir = dirs
        legacy = data_dir / ADVISORY_FILENAME
        _write_advisories(legacy, [_advisory()])
        before = legacy.read_bytes()

        resolve_advisory_path(stores_dir)
        load_advisory_store(stores_dir, surface="test")

        assert legacy.exists()
        assert legacy.read_bytes() == before
        assert not (stores_dir / ADVISORY_FILENAME).exists()


# ---------------------------------------------------------------------------
# The reader must never bind ``None`` in silence
# ---------------------------------------------------------------------------


class TestReaderNeverBindsNoneSilently:
    """#373 constraint 1, and the structural fix behind it.

    "A deployment with genuinely no advisories and a deployment reading the
    wrong directory must not present identically."
    """

    def test_missing_file_yields_a_store_not_none(
        self, dirs: tuple[Path, Path]
    ) -> None:
        store = load_advisory_store(dirs[1], surface="test")
        assert store is not None
        assert isinstance(store, AdvisoryStore)
        # Behaviourally identical to the old ``None`` for PackBuilder.
        assert store.list() == []

    def test_missing_file_is_logged_naming_every_path_searched(
        self, dirs: tuple[Path, Path]
    ) -> None:
        data_dir, stores_dir = dirs
        with capture_logs() as cap:
            load_advisory_store(stores_dir, surface="mcp")

        absent = [e for e in cap if e["event"] == "advisory_file_absent"]
        assert len(absent) == 1, "an absent advisory file must not be silent"
        assert absent[0]["surface"] == "mcp"
        assert set(absent[0]["searched"]) == {
            str(stores_dir / ADVISORY_FILENAME),
            str(data_dir / ADVISORY_FILENAME),
        }

    def test_found_file_is_logged_with_counts(self, dirs: tuple[Path, Path]) -> None:
        _data_dir, stores_dir = dirs
        _write_advisories(
            stores_dir / ADVISORY_FILENAME, [_advisory("a"), _advisory("b")]
        )
        with capture_logs() as cap:
            load_advisory_store(stores_dir, surface="mcp")

        loaded = [e for e in cap if e["event"] == "advisory_store_loaded"]
        assert len(loaded) == 1
        assert loaded[0]["advisory_count"] == 2
        assert loaded[0]["servable_count"] == 2
        assert loaded[0]["path"] == str(stores_dir / ADVISORY_FILENAME)

    def test_unconfigured_stores_dir_is_logged_too(self) -> None:
        with capture_logs() as cap:
            assert load_advisory_store(None, surface="test") is None
        assert [e["event"] for e in cap] == ["advisory_store_unconfigured"]

    def test_mcp_pack_builder_binds_a_store_when_no_file_exists(
        self, monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path]
    ) -> None:
        """The exact anti-regression: no ``if path.exists()`` guard.

        The bug was not that ``None`` is wrong for an empty deployment — it
        is fine. The bug was that ``None`` was also what a *misresolved*
        path produced. Binding a real (empty) store unconditionally is what
        makes the two cases distinguishable.
        """
        builder = _build_mcp_pack_builder(monkeypatch, dirs[1])
        assert builder._advisory_store is not None
        assert builder._get_matching_advisories(None) == []


# ---------------------------------------------------------------------------
# Every surface, one file
# ---------------------------------------------------------------------------


def _build_mcp_pack_builder(monkeypatch: pytest.MonkeyPatch, stores_dir: Path) -> Any:
    """Call the real MCP ``_build_pack_builder`` against ``stores_dir``.

    Stubs only the strategy/reranker wiring, which needs live backends —
    the advisory branch under test runs unmodified.
    """
    from trellis.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "build_strategies", lambda *a, **k: [])
    monkeypatch.setattr(mcp_server, "build_reranker", lambda *a, **k: None)
    monkeypatch.setattr(mcp_server, "ParameterRegistry", lambda *a, **k: None)

    class _Registry:
        stores_dir = None
        operational = type("_Op", (), {"event_log": None, "parameter_store": None})()

    registry = _Registry()
    registry.stores_dir = stores_dir  # type: ignore[assignment]
    return mcp_server._build_pack_builder(registry)  # type: ignore[arg-type]


def _build_api_pack_builder(monkeypatch: pytest.MonkeyPatch, stores_dir: Path) -> Any:
    """Call the real REST ``_build_pack_builder`` against ``stores_dir``."""
    from trellis_api.routes import retrieve as retrieve_routes

    monkeypatch.setattr(retrieve_routes, "build_strategies", lambda *a, **k: [])
    monkeypatch.setattr(retrieve_routes, "build_reranker", lambda *a, **k: None)
    monkeypatch.setattr(retrieve_routes, "ParameterRegistry", lambda *a, **k: None)

    class _Registry:
        stores_dir = None
        operational = type("_Op", (), {"event_log": None, "parameter_store": None})()

    registry = _Registry()
    registry.stores_dir = stores_dir  # type: ignore[assignment]
    return retrieve_routes._build_pack_builder(registry)


class TestSurfacesAgreeOnOneFile:
    """Writers and readers must land on the same advisory file.

    Before unification the nightly worker and ``trellis analyze`` wrote
    ``<data_dir>/advisories.json`` while the MCP pack builder, the REST
    pack builder and the REST admin routes read
    ``<stores_dir>/advisories.json``. Both halves "worked". Neither could
    see the other.
    """

    def test_worker_writer_and_mcp_reader_share_a_file(
        self, monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path]
    ) -> None:
        """The #373 regression test: write as the nightly worker, read as MCP."""
        data_dir, stores_dir = dirs
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))

        from trellis_cli.worker import _advisory_store_from_data_dir

        _advisory_store_from_data_dir().put(_advisory("adv_from_worker"))

        builder = _build_mcp_pack_builder(monkeypatch, stores_dir)
        served = builder._get_matching_advisories(None)
        assert [a.advisory_id for a in served] == ["adv_from_worker"]

    def test_worker_writer_and_api_reader_share_a_file(
        self, monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path]
    ) -> None:
        data_dir, stores_dir = dirs
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))

        from trellis_cli.worker import _advisory_store_from_data_dir

        _advisory_store_from_data_dir().put(_advisory("adv_from_worker"))

        builder = _build_api_pack_builder(monkeypatch, stores_dir)
        served = builder._get_matching_advisories(None)
        assert [a.advisory_id for a in served] == ["adv_from_worker"]

    def test_cli_data_dir_and_registry_stores_dir_resolve_the_same_path(
        self, dirs: tuple[Path, Path]
    ) -> None:
        """The CLI derives ``stores_dir`` from ``data_dir``; the API takes it
        from the registry. Same input, same file."""
        from trellis.stores.registry import StoreRegistry

        data_dir, stores_dir = dirs
        registry = StoreRegistry(config={}, stores_dir=stores_dir)
        assert resolve_advisory_path(data_dir / "stores") == resolve_advisory_path(
            registry.stores_dir
        )

    def test_the_live_legacy_file_is_visible_to_every_reader(
        self, monkeypatch: pytest.MonkeyPatch, dirs: tuple[Path, Path]
    ) -> None:
        """Reproduce the production shape exactly.

        A file at ``<data_dir>/advisories.json`` and nothing at
        ``<stores_dir>/advisories.json`` is the reference deployment as of
        2026-08-27: 37 advisories, refreshed nightly, invisible to all
        three readers. Every reader must now see it, without the file
        having been moved.
        """
        data_dir, stores_dir = dirs
        _write_advisories(data_dir / ADVISORY_FILENAME, [_advisory("adv_live")])
        assert not (stores_dir / ADVISORY_FILENAME).exists()

        mcp_ids = [
            a.advisory_id
            for a in _build_mcp_pack_builder(
                monkeypatch, stores_dir
            )._get_matching_advisories(None)
        ]
        api_ids = [
            a.advisory_id
            for a in _build_api_pack_builder(
                monkeypatch, stores_dir
            )._get_matching_advisories(None)
        ]
        direct = load_advisory_store(stores_dir, surface="test")
        assert direct is not None

        assert mcp_ids == api_ids == [a.advisory_id for a in direct.list()]
        assert mcp_ids == ["adv_live"]
        # And the live file stayed exactly where cron writes it.
        assert (data_dir / ADVISORY_FILENAME).exists()
        assert not (stores_dir / ADVISORY_FILENAME).exists()

    @pytest.mark.parametrize("surface", ["mcp", "api.retrieve", "cli.worker"])
    def test_no_surface_joins_the_filename_itself(self, surface: str) -> None:
        """Guard the seam by source inspection.

        A behavioural test can only cover the surfaces it knows about. This
        one fails when a *new* call site joins ``advisories.json`` onto a
        directory by hand instead of resolving through this module — which
        is precisely how the split was introduced.
        """
        import trellis.mcp.server
        import trellis_api.routes.retrieve
        import trellis_cli.worker

        module = {
            "mcp": trellis.mcp.server,
            "api.retrieve": trellis_api.routes.retrieve,
            "cli.worker": trellis_cli.worker,
        }[surface]
        source = Path(module.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
        assert ADVISORY_FILENAME not in source, (
            f"{surface} names {ADVISORY_FILENAME!r} directly; resolve through "
            "trellis.stores.advisory_source instead"
        )


class TestDegradedFileIsVisibleAtTheReadSurfaces:
    """#393 — a corrupt file must not present as a greenfield deployment.

    ``load_advisory_store``'s whole reason for existing is that "no
    advisories here" and "something is wrong" used to look identical. A
    file that exists but cannot be read is the third state, and it is the
    one that silently un-suppresses everything on the next write.
    """

    def test_a_degraded_store_is_still_returned(self, dirs: tuple[Path, Path]) -> None:
        """#382's lenient read survives: pack assembly keeps working."""
        _data_dir, stores_dir = dirs
        (stores_dir / ADVISORY_FILENAME).write_text("{ not json", encoding="utf-8")

        store = load_advisory_store(stores_dir, surface="mcp")

        assert store is not None
        assert store.is_degraded is True
        assert store.list() == []

    def test_partial_rows_are_still_served(self, dirs: tuple[Path, Path]) -> None:
        _data_dir, stores_dir = dirs
        path = stores_dir / ADVISORY_FILENAME
        _write_advisories(path, [_advisory("adv_good")])
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["advisories"].append({"advisory_id": "adv_bad", "category": "nope"})
        path.write_text(json.dumps(raw), encoding="utf-8")

        store = load_advisory_store(stores_dir, surface="api.retrieve")

        assert store is not None
        assert [a.advisory_id for a in store.list()] == ["adv_good"]

    def test_degradation_is_logged_at_error_with_the_surface(
        self, dirs: tuple[Path, Path]
    ) -> None:
        """The level is the assertion.

        ``trellis_cli.main._root`` pins ``TRELLIS_LOG_LEVEL=WARNING``, so an
        ``info`` line is invisible on the CLI. And ``capture_logs`` leaves
        ``wrapper_class`` alone, so every other assertion below would pass
        against a ``debug`` line that no surface ever prints.
        """
        _data_dir, stores_dir = dirs
        path = stores_dir / ADVISORY_FILENAME
        path.write_text("{ not json", encoding="utf-8")

        with capture_logs() as logs:
            load_advisory_store(stores_dir, surface="mcp")

        lines = [e for e in logs if e["event"] == "advisory_store_degraded"]
        assert len(lines) == 1
        assert lines[0]["log_level"] == "error"
        assert lines[0]["surface"] == "mcp"
        assert lines[0]["path"] == str(path)
        assert lines[0]["recovery"] == f"mv {path} {path}.corrupt"

    def test_the_reassuring_line_is_not_also_emitted(
        self, dirs: tuple[Path, Path]
    ) -> None:
        """``advisory_store_loaded`` reads as an all-clear. It must not fire.

        Two lines about the same load, one of them calm, is how a warning
        gets skipped.
        """
        _data_dir, stores_dir = dirs
        (stores_dir / ADVISORY_FILENAME).write_text("{ not json", encoding="utf-8")

        with capture_logs() as logs:
            load_advisory_store(stores_dir, surface="mcp")

        events = [e["event"] for e in logs]
        assert "advisory_store_loaded" not in events

    def test_a_clean_file_still_logs_the_calm_line(
        self, dirs: tuple[Path, Path]
    ) -> None:
        _data_dir, stores_dir = dirs
        _write_advisories(stores_dir / ADVISORY_FILENAME, [_advisory("adv_ok")])

        with capture_logs() as logs:
            load_advisory_store(stores_dir, surface="mcp")

        events = [e["event"] for e in logs]
        assert "advisory_store_loaded" in events
        assert "advisory_store_degraded" not in events

    def test_an_absent_file_is_not_reported_as_degraded(
        self, dirs: tuple[Path, Path]
    ) -> None:
        """A greenfield deployment must stay quiet — and keep writing."""
        _data_dir, stores_dir = dirs

        with capture_logs() as logs:
            store = load_advisory_store(stores_dir, surface="mcp")

        assert store is not None
        assert store.is_degraded is False
        events = [e["event"] for e in logs]
        assert "advisory_file_absent" in events
        assert "advisory_store_degraded" not in events


class TestAnUnreadablePathIsNotAnAbsentOne:
    """#479's display-side half — the posture stays lenient, the report gets honest.

    ``Path.exists()`` answers ``False`` for ``ELOOP`` and ``ENOTDIR`` as
    readily as for ``ENOENT``, so an advisory file behind a symlink loop
    took the ``advisory_file_absent`` branch: the calm ``info`` line that
    says *this is normal for a deployment that has never run ``trellis
    analyze generate-advisories``*, and an early ``return`` that skips the
    degradation check entirely.

    The store was never fooled — it stats rather than ``exists()`` since
    #444, so it degraded and refused writes correctly — but the surface
    never asked it. Which is #373's own failure, restated: a broken
    deployment and an empty one presenting identically, in silence.

    Nothing here raises. That asymmetry with
    :mod:`trellis.mutate.policy_source` is deliberate and load-bearing
    (display degrades, enforcement fails closed), so the last test pins it.
    """

    @pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
    def test_an_unreadable_file_is_reported_as_degraded(
        self, dirs: tuple[Path, Path], shape: UnreadablePathShape
    ) -> None:
        _data_dir, stores_dir = dirs
        path = stores_dir / ADVISORY_FILENAME

        with unreadable(shape, path), capture_logs() as logs:
            store = load_advisory_store(stores_dir, surface="mcp")

        assert store is not None
        assert store.is_degraded is True
        events = [e["event"] for e in logs]
        assert "advisory_store_degraded" in events
        assert "advisory_file_absent" not in events

    @pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
    def test_the_reassuring_absent_line_is_not_emitted(
        self, dirs: tuple[Path, Path], shape: UnreadablePathShape
    ) -> None:
        """The specific wrong sentence, asserted by its own content.

        ``advisory_file_absent`` carries ``This is normal for a deployment
        that has never run…``. Printing that about a symlink loop is the
        defect, so the test names the sentence rather than only the level.
        """
        _data_dir, stores_dir = dirs
        path = stores_dir / ADVISORY_FILENAME

        with unreadable(shape, path), capture_logs() as logs:
            load_advisory_store(stores_dir, surface="mcp")

        absent_lines = [e for e in logs if e["event"] == "advisory_file_absent"]
        assert absent_lines == []

    @pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
    def test_an_unreadable_store_still_serves_and_still_refuses_writes(
        self, dirs: tuple[Path, Path], shape: UnreadablePathShape
    ) -> None:
        """Lenient read, refused write — #393's rule, unchanged by #479."""
        _data_dir, stores_dir = dirs
        path = stores_dir / ADVISORY_FILENAME

        with unreadable(shape, path):
            store = load_advisory_store(stores_dir, surface="mcp")
            assert store is not None
            assert store.list() == []
            with pytest.raises(DegradedStoreWriteError):
                store.put(_advisory("adv_new"))

    def test_an_unreadable_canonical_file_does_not_fall_through_to_legacy(
        self, dirs: tuple[Path, Path]
    ) -> None:
        """Otherwise a deployment silently serves the stale legacy advisories.

        The symlink loop explicitly: it is the one shape that damages the
        canonical file alone, leaving the legacy path genuinely readable,
        which is what makes the fall-through reachable.
        """
        data_dir, stores_dir = dirs
        _write_advisories(data_dir / ADVISORY_FILENAME, [_advisory("adv_legacy")])

        shape = next(s for s in UNREADABLE_PATH_SHAPES if s.id == "symlink_loop")
        with unreadable(shape, stores_dir / ADVISORY_FILENAME):
            assert resolve_advisory_path(stores_dir) == stores_dir / ADVISORY_FILENAME
            store = load_advisory_store(stores_dir, surface="mcp")

        assert store is not None
        assert store.is_degraded is True
        assert [a.advisory_id for a in store.list()] == []

    def test_an_unreadable_legacy_file_is_not_skipped(
        self, dirs: tuple[Path, Path]
    ) -> None:
        """The second resolution site: canonical absent, legacy broken."""
        data_dir, stores_dir = dirs
        legacy = data_dir / ADVISORY_FILENAME

        shape = next(s for s in UNREADABLE_PATH_SHAPES if s.id == "symlink_loop")
        with unreadable(shape, legacy):
            assert resolve_advisory_path(stores_dir) == legacy
            store = load_advisory_store(stores_dir, surface="mcp")

        assert store is not None
        assert store.is_degraded is True

    @pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
    def test_the_display_posture_is_unchanged_it_still_never_raises(
        self, dirs: tuple[Path, Path], shape: UnreadablePathShape
    ) -> None:
        """The axis, pinned: display degrades where enforcement fails closed.

        Same primitive as ``policy_source``, opposite consequence. If this
        ever starts raising, a corrupt advisory file takes retrieval down
        with it — which is exactly what #382/#393 decided it must not do.
        """
        _data_dir, stores_dir = dirs
        with unreadable(shape, stores_dir / ADVISORY_FILENAME):
            assert resolve_advisory_path(stores_dir) is not None
            assert load_advisory_store(stores_dir, surface="mcp") is not None

    def test_a_genuinely_absent_file_is_still_reported_absent(
        self, dirs: tuple[Path, Path]
    ) -> None:
        """The control. Greenfield must stay quiet and stay writable."""
        _data_dir, stores_dir = dirs

        with capture_logs() as logs:
            store = load_advisory_store(stores_dir, surface="mcp")

        assert store is not None
        assert store.is_degraded is False
        events = [e["event"] for e in logs]
        assert "advisory_file_absent" in events
        assert "advisory_store_degraded" not in events
        store.put(_advisory("adv_fresh"))
