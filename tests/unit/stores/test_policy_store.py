"""Tests for PolicyStore — JSON file-based policy persistence."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from tests.policy_shapes import DEGENERATE_POLICY_FILES, DEGENERATE_POLICY_IDS
from trellis.errors import DegradedStoreWriteError, StaleStoreWriteError
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
        DEGENERATE_POLICY_FILES,
        ids=DEGENERATE_POLICY_IDS,
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
        DEGENERATE_POLICY_FILES,
        ids=DEGENERATE_POLICY_IDS,
    )
    def test_shape_refuses_every_write_and_leaves_the_bytes_alone(
        self, tmp_path: Path, name: str, text: str, reason: str
    ) -> None:
        path = _damaged(tmp_path, text)
        store = PolicyStore(path)
        before = path.read_bytes()

        with pytest.raises(DegradedStoreWriteError) as exc_info:
            store.add(_policy())
        # The label routes the message to the right operator instruction;
        # "advisory" here would send them to the wrong file.
        assert exc_info.value.store == "policy"
        with pytest.raises(DegradedStoreWriteError):
            store.remove("anything")

        assert path.read_bytes() == before, f"{name}: the damaged file was written"
        assert not [p for p in tmp_path.iterdir() if p.name != "policies.json"]

    @pytest.mark.parametrize(
        ("name", "text", "reason"),
        DEGENERATE_POLICY_FILES,
        ids=DEGENERATE_POLICY_IDS,
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
        attempt = (
            (lambda: store.add(_policy()))
            if write == "add"
            else (lambda: store.remove("anything"))
        )

        with pytest.raises(DegradedStoreWriteError):
            attempt()

        assert entered == [], "the write path was entered on a degraded store"

    def test_save_refuses_on_its_own(self, tmp_path: Path) -> None:
        """The unconditional backstop, pinned separately from its callers.

        ``add`` and ``remove`` refuse first, which masks this: remove the
        guard from ``_save`` alone and the suite stayed green. The docstring
        claims "not calling ``refuse_if_degraded`` is never a way to avoid
        one", and that claim is about ``_save``, so it needs its own test —
        it is what protects a future write path added without the leading
        guard.
        """
        store = PolicyStore(_damaged(tmp_path, "{ broken"))
        with pytest.raises(DegradedStoreWriteError):
            store._save()


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

    @pytest.mark.skipif(
        os.geteuid() == 0, reason="root reads any file regardless of mode"
    )
    def test_an_unreadable_file_degrades(self, tmp_path: Path) -> None:
        """Permission-denied is the *most likely* real cause, so assert hard.

        This test used to wrap its assertions in ``if store.is_degraded:``,
        which made it structurally unable to fail: swallow the ``OSError``
        without degrading and the ``if`` is simply skipped. That is the
        complete #413 fail-open — an empty store, writes permitted, the
        ruleset replaced — guarded by a test that passes either way.

        And it is not a hypothetical cause. The reference deployment
        bind-mounts its data directory into containers running under a
        different uid, which is exactly how a policy file becomes readable
        by one writer and not the other.
        """
        path = _damaged(tmp_path, '{"policies": []}')
        path.chmod(0o000)
        try:
            store = PolicyStore(path)
        finally:
            path.chmod(0o644)

        assert store.is_degraded is True
        assert store.degradation is not None
        assert store.degradation.reason == "unreadable_file"
        with pytest.raises(DegradedStoreWriteError):
            store.add(_policy())


class TestConstructionNeverRaises:
    """``_load``'s outer catch, which nothing else reaches.

    "Constructing this store never raises" is what keeps ``trellis policy
    list`` working whatever shape the corruption takes — the entire
    justification for the lenient read. Nothing exercised it, so any future
    change letting an exception escape ``_load_rows`` would turn that
    command into a traceback on exactly the file it exists to show.
    """

    def test_an_unexpected_load_failure_degrades_instead_of_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_self: PolicyStore) -> None:
            msg = "something nobody anticipated"
            raise RuntimeError(msg)

        monkeypatch.setattr(PolicyStore, "_load_rows", _boom)
        path = _damaged(tmp_path, '{"policies": []}')

        store = PolicyStore(path)

        assert store.is_degraded is True
        assert store.degradation is not None
        assert store.degradation.reason == "load_failed"
        assert "RuntimeError" in store.degradation.detail
        assert store.list() == []
        with pytest.raises(DegradedStoreWriteError):
            store.add(_policy())


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
        assert not [p for p in tmp_path.iterdir() if p.name != "policies.json"]

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
        assert not [p for p in tmp_path.iterdir() if p.name != "policies.json"]

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


class TestDegradationIsNotTheOnlyStaleView:
    """The laundering primitive is wider than a degraded load.

    A whole-file rewrite from *any* in-memory view that is no longer the
    file produces #413's end state. These are the two routes that reach it
    with nothing degraded — found by the review pass, not by the issue.
    """

    def test_a_second_writer_is_not_silently_overwritten(self, tmp_path: Path) -> None:
        """The API's cached-store defect, at the store level.

        Two processes write this file — a host ``trellis policy add`` and a
        containerised ``POST /api/policies`` against one bind-mounted data
        dir — so a store that loaded ``[A]`` must not rewrite the file as
        ``[A, C]`` after the other made it ``[A, B]``. Doing so deletes a
        policy from disk *and* from Stage 2 enforcement, on a call that
        succeeds, with every surface reporting normal.
        """
        path = tmp_path / "policies.json"
        mine = PolicyStore(path)
        a = _policy()
        mine.add(a)

        # Another process appends B between my load and my next write.
        theirs = PolicyStore(path)
        b = _policy(scope=PolicyScope(level="domain", value="payments"))
        theirs.add(b)
        after_theirs = path.read_bytes()

        with pytest.raises(StaleStoreWriteError) as exc_info:
            mine.add(_policy(scope=PolicyScope(level="team", value="core")))

        assert path.read_bytes() == after_theirs
        assert exc_info.value.recovery == "trellis policy list"
        # Transient and retryable, so it must not claim the file is damaged.
        assert exc_info.value.code == "STALE_STORE_WRITE"
        assert mine.is_degraded is False

    def test_a_file_created_after_construction_is_not_wiped(
        self, tmp_path: Path
    ) -> None:
        """A store built against an absent path never read the file.

        In the API this was the common startup ordering: the first request
        builds a store while no policy file exists, an operator declares
        policies through the CLI, and a later write replaces them with the
        one row the server knew about.
        """
        path = tmp_path / "policies.json"
        store = PolicyStore(path)  # absent: not degraded, writes permitted

        declared = _policy()
        PolicyStore(path).add(declared)
        landed = path.read_bytes()

        with pytest.raises(StaleStoreWriteError):
            store.add(_policy(scope=PolicyScope(level="team", value="core")))

        assert path.read_bytes() == landed

    def test_the_same_store_may_write_repeatedly(self, tmp_path: Path) -> None:
        """The guard must not fire on a store's own previous write.

        ``os.replace`` changes the inode every time, so without refreshing
        the fingerprint after a successful save the second ``add`` on any
        store would refuse — which would break every ordinary use.
        """
        store = PolicyStore(tmp_path / "policies.json")
        store.add(_policy())
        store.add(_policy(scope=PolicyScope(level="domain", value="payments")))
        store.add(_policy(scope=PolicyScope(level="team", value="core")))

        assert len(store.list()) == 3
        assert len(PolicyStore(tmp_path / "policies.json").list()) == 3

    def test_damage_arriving_after_a_clean_load_is_not_overwritten(
        self, tmp_path: Path
    ) -> None:
        """The refusal must not be keyed on load-time state alone.

        A store that read a healthy file is not degraded, and nothing about
        the *load* will ever say otherwise — so before the stale guard it
        would cheerfully whole-file-rewrite a file an operator had since
        broken (or edited), destroying the edit and resuming from its own
        stale snapshot. The module docstring's promise that "the damaged
        bytes stay on disk, where an operator can look at them" was false on
        exactly this path.
        """
        path = tmp_path / "policies.json"
        store = PolicyStore(path)
        store.add(_policy())
        assert store.is_degraded is False

        path.write_text('{"policys": [{"policy_id": "x"}]}', encoding="utf-8")
        damaged = path.read_bytes()

        with pytest.raises(StaleStoreWriteError):
            store.add(_policy(scope=PolicyScope(level="team", value="core")))

        assert path.read_bytes() == damaged

    def test_a_duplicate_policy_id_degrades_rather_than_collapsing(
        self, tmp_path: Path
    ) -> None:
        """The two readers must not disagree about what the file says.

        This store keys by ``policy_id``; ``policy_source`` builds a *list*
        and evaluates every duplicate (deny wins). Collapsing silently made
        the CRUD view smaller than the enforced ruleset — and the next
        permitted write would have made the file match the smaller view,
        deleting a rule the gate was enforcing.
        """
        first = _policy(rules=[PolicyRule(operation="entity.delete", action="deny")])
        second = first.model_copy(
            update={"rules": [PolicyRule(operation="entity.create", action="deny")]}
        )
        path = tmp_path / "policies.json"
        path.write_text(
            json.dumps(
                {
                    "policies": [
                        first.model_dump(mode="json"),
                        second.model_dump(mode="json"),
                    ]
                }
            ),
            encoding="utf-8",
        )
        before = path.read_bytes()

        store = PolicyStore(path)

        assert store.is_degraded is True
        assert store.degradation is not None
        assert store.degradation.reason == "invalid_rows"
        assert "duplicate policy_id" in store.degradation.detail
        # The first occurrence is kept and served; the write is refused.
        assert [p.policy_id for p in store.list()] == [first.policy_id]
        with pytest.raises(DegradedStoreWriteError):
            store.add(_policy(scope=PolicyScope(level="team", value="core")))
        assert path.read_bytes() == before

    @pytest.mark.skipif(
        os.geteuid() == 0, reason="root traverses any directory regardless of mode"
    )
    def test_an_unsearchable_parent_degrades_rather_than_reading_as_absent(
        self, tmp_path: Path
    ) -> None:
        """``Path.exists()`` swallows ``OSError``, which was the hazard.

        An unreadable file presenting as *absent* is the worst of the
        available answers: absent means "the shipped transparent default",
        which is both not-degraded and writable.
        """
        parent = tmp_path / "locked"
        parent.mkdir()
        path = parent / "policies.json"
        path.write_text('{"policies": []}', encoding="utf-8")
        parent.chmod(0o000)
        try:
            store = PolicyStore(path)
        finally:
            parent.chmod(0o755)

        assert store.is_degraded is True
        assert store.degradation is not None
        assert store.degradation.reason == "unreadable_file"
