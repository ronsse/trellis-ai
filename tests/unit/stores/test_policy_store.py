"""Tests for PolicyStore — JSON file-based policy persistence."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from trellis.errors import DegradedStoreWriteError
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.policy_store import PolicyStore


def _policy(**kwargs) -> Policy:
    defaults = {
        "policy_type": PolicyType.MUTATION,
        "scope": PolicyScope(level="global"),
        "rules": [PolicyRule(operation="entity.create", action="deny")],
        "enforcement": Enforcement.ENFORCE,
    }
    defaults.update(kwargs)
    return Policy(**defaults)


class TestPolicyStore:
    def test_add_and_list(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        p = _policy()
        store.add(p)
        policies = store.list()
        assert len(policies) == 1
        assert policies[0].policy_id == p.policy_id

    def test_get_by_id(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        p = _policy()
        store.add(p)
        found = store.get(p.policy_id)
        assert found is not None
        assert found.policy_id == p.policy_id

    def test_get_nonexistent(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        assert store.get("nonexistent") is None

    def test_remove(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        p = _policy()
        store.add(p)
        assert store.remove(p.policy_id) is True
        assert store.list() == []

    def test_remove_nonexistent(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        assert store.remove("nonexistent") is False

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "policies.json"
        store1 = PolicyStore(path)
        p = _policy()
        store1.add(p)

        # New instance reads persisted data
        store2 = PolicyStore(path)
        policies = store2.list()
        assert len(policies) == 1
        assert policies[0].policy_id == p.policy_id

    def test_multiple_policies(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        p1 = _policy()
        p2 = _policy(scope=PolicyScope(level="domain", value="payments"))
        store.add(p1)
        store.add(p2)
        assert len(store.list()) == 2

    def test_replace_existing(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        p = _policy()
        store.add(p)
        # Add same policy again (same ID) — should replace
        store.add(p)
        assert len(store.list()) == 1

    def test_empty_store_on_new_path(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "new" / "policies.json")
        assert store.list() == []


# ---------------------------------------------------------------------------
# #413 — read leniently, refuse to write
# ---------------------------------------------------------------------------

#: Every shape a damaged ``policies.json`` has actually been seen to take,
#: paired with the ``reason`` the store must record. The two that matter
#: most are ``{}`` and the typo'd key: they are *valid JSON* and the old
#: ``raw.get("policies", [])`` loaded them as a **clean empty store**, so
#: the defect survived for exactly the shapes that look healthiest. #414
#: shipped that bug inside its own fix before it was caught; this table is
#: why it cannot happen here silently.
_DEGENERATE_SHAPES: list[tuple[str, str, str]] = [
    ("empty_json_object", "{}", "malformed_envelope"),
    ("null_policies_key", '{"policies": null}', "malformed_envelope"),
    ("typoed_key", '{"policys": [{"policy_id": "x"}]}', "malformed_envelope"),
    ("bare_list", "[]", "malformed_envelope"),
    ("scalar", '"not a policy file"', "malformed_envelope"),
    ("empty_file", "", "malformed_json"),
    ("truncated_json", '{"policies": [{"policy_i', "malformed_json"),
]


def _damaged(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policies.json"
    path.write_text(text, encoding="utf-8")
    return path


class TestDegenerateShapesDegradeAndRefuse:
    """Each shape must do **both**: degrade the load *and* refuse the write.

    Degrading alone was the pre-#413 behaviour and is the defect: an empty
    in-memory store that the next ``_save`` writes over the file, laundering
    the damage into a valid, empty, *enforced* ruleset.
    """

    @pytest.mark.parametrize(
        ("name", "text", "reason"),
        _DEGENERATE_SHAPES,
        ids=[s[0] for s in _DEGENERATE_SHAPES],
    )
    def test_shape_degrades(
        self, tmp_path: Path, name: str, text: str, reason: str
    ) -> None:
        store = PolicyStore(_damaged(tmp_path, text))

        assert store.is_degraded is True, f"{name} loaded as a clean store"
        degradation = store.degradation
        assert degradation is not None
        assert degradation.reason == reason
        assert degradation.path == str(tmp_path / "policies.json")
        # Unknowable, not zero: nothing got as far as counting rows, and
        # "0 could not be read" would tell an operator nothing was lost.
        assert degradation.rows_skipped is None
        assert degradation.rows_skipped_display == "unknown"
        assert degradation.recovery.startswith("mv ")

    @pytest.mark.parametrize(
        ("name", "text", "reason"),
        _DEGENERATE_SHAPES,
        ids=[s[0] for s in _DEGENERATE_SHAPES],
    )
    def test_shape_refuses_every_write_and_leaves_the_bytes_alone(
        self, tmp_path: Path, name: str, text: str, reason: str
    ) -> None:
        path = _damaged(tmp_path, text)
        store = PolicyStore(path)
        before = path.read_bytes()

        with pytest.raises(DegradedStoreWriteError):
            store.add(_policy())
        with pytest.raises(DegradedStoreWriteError):
            store.remove("anything")

        assert path.read_bytes() == before, f"{name}: the damaged file was written"
        assert [p.name for p in tmp_path.iterdir()] == ["policies.json"]

    @pytest.mark.parametrize(
        ("name", "text", "reason"),
        _DEGENERATE_SHAPES,
        ids=[s[0] for s in _DEGENERATE_SHAPES],
    )
    def test_a_refused_write_does_not_mutate_memory(
        self, tmp_path: Path, name: str, text: str, reason: str
    ) -> None:
        """Refuse *before* mutating, not after.

        #414 shipped the refusal after the in-memory mutation, so a refused
        write had already changed what ``list()`` returned — the same data
        loss, in-process, landing on exactly the caller the refusal exists
        for (the API route holds its store in a cache across requests).
        """
        store = PolicyStore(_damaged(tmp_path, text))
        before = store.list()

        with pytest.raises(DegradedStoreWriteError):
            store.add(_policy())

        assert store.list() == before

    @pytest.mark.parametrize("write", ["add", "remove"])
    def test_a_degraded_write_never_enters_the_write_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write: str
    ) -> None:
        """Fail before doing the work, not after undoing it.

        The rollback in ``_save_or_roll_back`` makes a *post*-mutation
        refusal look identical from outside — same exception, same
        ``list()`` — so the two mechanisms mask each other, which is the
        precise trap #414 recorded after two of its tests turned out unable
        to detect removal of the fix they protected. Deleting
        ``refuse_if_degraded()`` from ``add`` left the whole suite green
        when this file was first written; this assertion is what noticed.

        It is not a distinction without a difference. FastAPI runs sync
        routes in a threadpool over one cached ``PolicyStore``, so between
        the mutation and the rollback a concurrent ``GET /policies`` can
        observe a policy that is not on disk and never will be — a partial
        view of an access-control file, served as the ruleset.
        """
        store = PolicyStore(_damaged(tmp_path, "{ broken"))
        entered: list[str] = []
        monkeypatch.setattr(
            store,
            "_save",
            lambda: entered.append("save"),  # type: ignore[method-assign]
        )

        with pytest.raises(DegradedStoreWriteError):
            if write == "add":
                store.add(_policy())
            else:
                store.remove("anything")

        assert entered == [], "the write path was entered on a degraded store"


class TestDegradationIsPerRow:
    """One unparseable entry costs one entry, not the ruleset.

    The pre-#413 handler wrapped the whole loop, so a single renamed field
    erased every policy from ``trellis policy list`` — an operator's only
    view of their own governance, blanked at the moment they needed it.
    """

    def test_a_good_row_survives_a_bad_neighbour(self, tmp_path: Path) -> None:
        good = _policy()
        path = tmp_path / "policies.json"
        path.write_text(
            json.dumps(
                {"policies": [good.model_dump(mode="json"), {"policy_type": "bogus"}]}
            ),
            encoding="utf-8",
        )

        store = PolicyStore(path)

        assert [p.policy_id for p in store.list()] == [good.policy_id]
        degradation = store.degradation
        assert degradation is not None
        assert degradation.reason == "invalid_rows"
        assert degradation.rows_loaded == 1
        assert degradation.rows_skipped == 1
        assert degradation.rows_skipped_display == "1"
        assert "row 1" in degradation.detail

    def test_a_partial_load_still_refuses_to_write(self, tmp_path: Path) -> None:
        """The leniency above is only safe because of this.

        A partial load followed by a permitted write rewrites the file
        without the skipped rows — the same laundering at a narrower
        granularity, and harder to notice because the file still has
        content.
        """
        good = _policy()
        path = tmp_path / "policies.json"
        path.write_text(
            json.dumps(
                {"policies": [good.model_dump(mode="json"), {"policy_type": "bogus"}]}
            ),
            encoding="utf-8",
        )
        before = path.read_bytes()
        store = PolicyStore(path)

        with pytest.raises(DegradedStoreWriteError):
            store.add(_policy(scope=PolicyScope(level="domain", value="payments")))

        assert path.read_bytes() == before

    def test_many_bad_rows_report_a_bounded_detail(self, tmp_path: Path) -> None:
        path = tmp_path / "policies.json"
        path.write_text(
            json.dumps({"policies": [{"policy_type": "bogus"} for _ in range(9)]}),
            encoding="utf-8",
        )

        degradation = PolicyStore(path).degradation

        assert degradation is not None
        assert degradation.rows_skipped == 9
        assert "(+6 more)" in degradation.detail


class TestUnreadableFile:
    def test_undecodable_bytes_degrade_rather_than_raise(self, tmp_path: Path) -> None:
        """``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``.

        It is also the shape a truncated or partially-binary write actually
        takes, so catching only ``OSError`` would have let it escape the
        constructor — and constructing this store must never raise, or
        ``trellis policy list`` stops working on exactly the file it exists
        to show.
        """
        path = tmp_path / "policies.json"
        path.write_bytes(b'{"policies": [\xff\xfe]}')

        store = PolicyStore(path)

        assert store.is_degraded is True
        assert store.degradation is not None
        assert store.degradation.reason == "unreadable_file"

    def test_an_unreadable_file_degrades(self, tmp_path: Path) -> None:
        path = _damaged(tmp_path, '{"policies": []}')
        path.chmod(0o000)
        try:
            store = PolicyStore(path)
        finally:
            path.chmod(0o644)

        # Running as root makes the file readable regardless of mode, so
        # this asserts the reachable outcome rather than skipping.
        if store.is_degraded:
            assert store.degradation is not None
            assert store.degradation.reason == "unreadable_file"


class TestCleanLoadIsUnchanged:
    """The fix must be invisible to a deployment whose file is fine."""

    def test_a_healthy_file_is_not_degraded(self, tmp_path: Path) -> None:
        path = tmp_path / "policies.json"
        store = PolicyStore(path)
        store.add(_policy())

        reloaded = PolicyStore(path)
        assert reloaded.is_degraded is False
        assert reloaded.degradation is None
        assert len(reloaded.list()) == 1

    def test_an_absent_file_is_not_degradation(self, tmp_path: Path) -> None:
        """Trellis ships zero policies; never having declared one is normal.

        Conflating "no file" with "unreadable file" would make the shipped
        default posture look like a fault on every deployment.
        """
        store = PolicyStore(tmp_path / "nothing-here.json")
        assert store.is_degraded is False
        assert store.list() == []
        store.add(_policy())  # and writes are permitted

    def test_an_explicitly_empty_list_is_not_degradation(self, tmp_path: Path) -> None:
        """``{"policies": []}`` is what removing the last policy writes."""
        store = PolicyStore(_damaged(tmp_path, '{"policies": []}'))
        assert store.is_degraded is False
        assert store.list() == []
        store.add(_policy())


class TestDegradationIsLoggedWhereOperatorsSee:
    def test_the_line_is_error_not_info(self, tmp_path: Path) -> None:
        """``trellis_cli.main._root`` *defaults* ``TRELLIS_LOG_LEVEL`` to
        ``WARNING``, so an ``info`` line here is filtered out of the CLI —
        the surface an operator is most likely to meet this on.

        The level is pinned because ``capture_logs`` swaps the processor
        chain but leaves ``wrapper_class`` alone: under pytest the bound
        logger records ``debug`` too, so every other assertion in this test
        would pass just as well against an invisible line.
        """
        path = _damaged(tmp_path, "{ broken")

        with capture_logs() as logs:
            PolicyStore(path)

        lines = [e for e in logs if e["event"] == "policy_load_degraded"]
        assert len(lines) == 1
        assert lines[0]["log_level"] == "error"
        assert lines[0]["reason"] == "malformed_json"
        assert lines[0]["path"] == str(path)
        assert lines[0]["recovery"] == f"mv {path} {path}.corrupt"

    def test_a_clean_load_says_nothing_alarming(self, tmp_path: Path) -> None:
        """A warning on every load would train the reader to skip it."""
        path = tmp_path / "policies.json"
        PolicyStore(path).add(_policy())

        with capture_logs() as logs:
            PolicyStore(path)

        assert not [e for e in logs if e["event"] == "policy_load_degraded"]


class TestSaveIsAtomic:
    """The half-written file everything above survives has one author.

    ``write_text`` truncates the destination and *then* writes, so a crash,
    a full disk or a killed process between the two produces it. This store
    is the file's only writer, so closing that window closes the main way
    the degraded state gets created at all.
    """

    def test_no_temp_files_are_left_behind(self, tmp_path: Path) -> None:
        store = PolicyStore(tmp_path / "policies.json")
        store.add(_policy())
        store.add(_policy(scope=PolicyScope(level="domain", value="payments")))
        assert [p.name for p in tmp_path.iterdir()] == ["policies.json"]

    def test_a_failed_write_leaves_the_destination_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The property atomicity buys, stated as behaviour."""
        path = tmp_path / "policies.json"
        store = PolicyStore(path)
        original = _policy()
        store.add(original)
        before = path.read_text(encoding="utf-8")

        def _boom(_fd: int) -> None:
            msg = "No space left on device"
            raise OSError(msg)

        monkeypatch.setattr("trellis.core.atomic_write.os.fsync", _boom)

        with pytest.raises(OSError, match="No space left"):
            store.add(_policy(scope=PolicyScope(level="domain", value="payments")))

        assert path.read_text(encoding="utf-8") == before
        assert [p.name for p in tmp_path.iterdir()] == ["policies.json"]

        # And memory rolled back with it. This is the case the degraded-path
        # refusal cannot cover — the store loaded cleanly, so nothing refuses
        # ahead of the mutation, and without the rollback the API route (which
        # caches its store across requests) would go on listing a policy that
        # is not on disk and never will be.
        assert [p.policy_id for p in store.list()] == [original.policy_id]

    def test_an_existing_file_keeps_its_mode(self, tmp_path: Path) -> None:
        """``mkstemp`` creates 0600; inheriting it would narrow a live file.

        The reference deployment bind-mounts the data directory into
        containers, so a silently-narrowed policies.json is a reader that
        stops reading — and a policy file that cannot be read is the
        fail-open this whole change exists to prevent.
        """
        path = tmp_path / "policies.json"
        store = PolicyStore(path)
        store.add(_policy())
        path.chmod(0o640)

        store.add(_policy(scope=PolicyScope(level="team", value="core")))

        assert stat.S_IMODE(path.stat().st_mode) == 0o640

    def test_a_symlinked_path_is_followed_not_replaced(self, tmp_path: Path) -> None:
        """``os.replace`` onto a symlink strands the target, silently.

        A symlink is a plausible answer to ``resolve_policy_path``'s "move
        the file to the canonical path" advice, so the shape is reachable.
        ``write_text`` followed the link; this must keep that.
        """
        real = tmp_path / "real.json"
        real.write_text('{"policies": []}', encoding="utf-8")
        link = tmp_path / "policies.json"
        link.symlink_to(real)

        PolicyStore(link).add(_policy())

        assert link.is_symlink()
        assert json.loads(real.read_text(encoding="utf-8"))["policies"]
