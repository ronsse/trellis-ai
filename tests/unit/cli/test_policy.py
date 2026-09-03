"""Tests for policy CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import assert_coloured, force_colour
from tests.unreadable_paths import (
    UNREADABLE_PATH_IDS,
    UNREADABLE_PATH_SHAPES,
    UnreadablePathShape,
    unreadable,
)
from trellis.errors import StaleStoreWriteError
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.policy_store import PolicyStore
from trellis_cli import policy as policy_cli
from trellis_cli.exit_codes import EXIT_STORE
from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def _add_policy() -> str:
    """Helper: add a policy and return its ID."""
    result = runner.invoke(
        app,
        [
            "policy",
            "add",
            "--type",
            "mutation",
            "--scope",
            "global",
            "--operation",
            "entity.create",
            "--action",
            "deny",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout.strip())
    return data["policy_id"]


class TestPolicyList:
    def test_list_empty(self) -> None:
        result = runner.invoke(app, ["policy", "list"])
        assert result.exit_code == 0
        assert "No policies" in result.stdout

    def test_list_empty_json(self) -> None:
        result = runner.invoke(app, ["policy", "list", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["count"] == 0
        assert data["policies"] == []

    def test_list_after_add(self) -> None:
        _add_policy()
        result = runner.invoke(app, ["policy", "list"])
        assert result.exit_code == 0
        assert "mutation" in result.stdout

    def test_list_after_add_json(self) -> None:
        _add_policy()
        result = runner.invoke(app, ["policy", "list", "--format", "json"])
        data = json.loads(result.stdout.strip())
        assert data["count"] == 1
        assert data["policies"][0]["policy_type"] == "mutation"


class TestPolicyAdd:
    def test_add_text(self) -> None:
        result = runner.invoke(
            app,
            [
                "policy",
                "add",
                "--type",
                "mutation",
                "--scope",
                "global",
                "--operation",
                "entity.delete",
                "--action",
                "warn",
            ],
        )
        assert result.exit_code == 0
        assert "Policy added" in result.stdout

    def test_add_json(self) -> None:
        policy_id = _add_policy()
        assert len(policy_id) > 0

    def test_add_with_scope_value(self) -> None:
        result = runner.invoke(
            app,
            [
                "policy",
                "add",
                "--type",
                "access",
                "--scope",
                "domain",
                "--scope-value",
                "platform",
                "--operation",
                "trace.read",
                "--action",
                "allow",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"

    def test_add_with_custom_enforcement(self) -> None:
        result = runner.invoke(
            app,
            [
                "policy",
                "add",
                "--type",
                "mutation",
                "--scope",
                "global",
                "--operation",
                "*",
                "--action",
                "deny",
                "--enforcement",
                "audit_only",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"


class TestPolicyShow:
    def test_show_by_id(self) -> None:
        policy_id = _add_policy()
        result = runner.invoke(app, ["policy", "show", policy_id])
        assert result.exit_code == 0
        assert "mutation" in result.stdout

    def test_show_by_prefix(self) -> None:
        policy_id = _add_policy()
        prefix = policy_id[:8]
        result = runner.invoke(app, ["policy", "show", prefix])
        assert result.exit_code == 0
        assert policy_id in result.stdout

    def test_show_json(self) -> None:
        """**Breaking change (#413):** the payload is now an envelope.

        It used to be a bare ``Policy`` dump. That could not carry
        ``store_degradation`` — every Trellis schema is ``extra="forbid"``,
        so a dump plus a foreign key is a payload ``Policy.model_validate``
        rejects, and it would have broken round-tripping callers exactly
        when the store was degraded. Nesting under ``policy`` matches
        ``GET /api/v1/policies/{id}`` and ``policy list``.
        """
        policy_id = _add_policy()
        result = runner.invoke(app, ["policy", "show", policy_id, "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        assert data["policy"]["policy_id"] == policy_id

    def test_show_json_round_trips_as_a_policy_even_when_degraded(
        self, tmp_path: Path
    ) -> None:
        """The property the envelope exists to protect."""
        from trellis.schemas.policy import Policy

        policy_id = _add_policy()
        path = tmp_path / "data" / "stores" / "policies.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["policies"].append({"policy_type": "bogus"})
        path.write_text(json.dumps(raw), encoding="utf-8")

        result = runner.invoke(app, ["policy", "show", policy_id, "--format", "json"])

        assert result.exit_code == 5
        data = json.loads(result.stdout.strip())
        assert data["status"] == "degraded"
        assert data["store_degradation"]["reason"] == "invalid_rows"
        # The nested payload is still a valid Policy. Before the envelope
        # this raised ``extra_forbidden``.
        assert Policy.model_validate(data["policy"]).policy_id == policy_id

    def test_show_not_found(self) -> None:
        result = runner.invoke(app, ["policy", "show", "nonexistent"])
        assert result.exit_code == 1


class TestPolicyRemove:
    def test_remove_by_id(self) -> None:
        policy_id = _add_policy()
        result = runner.invoke(app, ["policy", "remove", policy_id])
        assert result.exit_code == 0
        assert "removed" in result.stdout.lower()

    def test_remove_json(self) -> None:
        policy_id = _add_policy()
        result = runner.invoke(app, ["policy", "remove", policy_id, "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"

    def test_remove_not_found(self) -> None:
        result = runner.invoke(app, ["policy", "remove", "nonexistent"])
        assert result.exit_code == 1

    def test_remove_not_found_json(self) -> None:
        result = runner.invoke(
            app, ["policy", "remove", "nonexistent", "--format", "json"]
        )
        assert result.exit_code == 1

    def test_remove_then_list_empty(self) -> None:
        policy_id = _add_policy()
        runner.invoke(app, ["policy", "remove", policy_id])
        result = runner.invoke(app, ["policy", "list", "--format", "json"])
        data = json.loads(result.stdout.strip())
        assert data["count"] == 0


class TestPolicyHelp:
    def test_help(self) -> None:
        result = runner.invoke(app, ["policy", "--help"])
        assert result.exit_code == 0
        for cmd in ["list", "show", "add", "remove"]:
            assert cmd in result.stdout


# ---------------------------------------------------------------------------
# #413 — a damaged policy file must be visible, and must not be writable
# ---------------------------------------------------------------------------


def _damage(tmp_path: Path, text: str) -> Path:
    """Put ``text`` at the canonical policy path the CLI resolves to."""
    path = tmp_path / "data" / "stores" / "policies.json"
    path.write_text(text, encoding="utf-8")
    return path


class TestPolicyListSurvivesADamagedFile:
    """The CRUD reader's stated purpose: still tell an operator where they are.

    That purpose is what justifies the lenient read at all, so it has to
    keep working — a fix that made ``policy list`` fail on a damaged file
    would have traded one silent failure for a loud outage on the one
    command an operator reaches for when the file breaks.
    """

    def test_readable_policies_are_still_listed(self, tmp_path: Path) -> None:
        policy_id = _add_policy()
        path = tmp_path / "data" / "stores" / "policies.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["policies"].append({"policy_type": "bogus"})
        path.write_text(json.dumps(raw), encoding="utf-8")

        result = runner.invoke(app, ["policy", "list", "--format", "json"])

        data = json.loads(result.stdout.strip())
        assert [p["policy_id"] for p in data["policies"]] == [policy_id]
        assert data["count"] == 1
        assert data["store_degradation"]["reason"] == "invalid_rows"
        # ...but the answer is incomplete, and says so in the exit code. A
        # script verifying governance must not read a truncated list as truth.
        assert result.exit_code == 5

    def test_text_output_leads_with_the_banner(self, tmp_path: Path) -> None:
        """An operator who reads the first line and stops must not stop on a
        reassuring one."""
        _damage(tmp_path, "{ broken")

        result = runner.invoke(app, ["policy", "list"])

        assert result.exit_code == 5
        assert "POLICY STORE DEGRADED" in result.stdout
        assert "malformed_json" in result.stdout
        assert "To reset:" in result.stdout
        # The banner precedes whatever the listing had to say.
        assert result.stdout.index("POLICY STORE DEGRADED") < result.stdout.index(
            "No policies configured"
        )

    def test_the_banner_precedes_the_table_when_policies_do_parse(
        self, tmp_path: Path
    ) -> None:
        """Banner above the listing, not below it.

        The case that actually matters: some policies parsed, so the table
        renders and looks entirely normal. An operator who reads top-down
        must meet the warning before the reassuring content, not after it.
        """
        _add_policy()
        path = tmp_path / "data" / "stores" / "policies.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["policies"].append({"policy_type": "bogus"})
        path.write_text(json.dumps(raw), encoding="utf-8")

        result = runner.invoke(app, ["policy", "list"])

        assert result.exit_code == 5
        assert result.stdout.index("POLICY STORE DEGRADED") < result.stdout.index(
            "Governance Policies"
        )
        # ...and the readable policy is still shown. Refusing to render it
        # would trade a silent failure for an outage on the one command an
        # operator reaches for when the file breaks.
        assert "mutation" in result.stdout

    def test_show_still_exits_nonzero_when_the_policy_was_found(
        self, tmp_path: Path
    ) -> None:
        """The answer is right; the store is still broken.

        Every ``policy`` command exits 5 on a degraded store — one rule, so
        a wrapper needs no per-command knowledge to notice that the file
        behind its governance answers is unreadable.
        """
        policy_id = _add_policy()
        path = tmp_path / "data" / "stores" / "policies.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["policies"].append({"policy_type": "bogus"})
        path.write_text(json.dumps(raw), encoding="utf-8")

        result = runner.invoke(app, ["policy", "show", policy_id])

        assert result.exit_code == 5
        assert policy_id in result.stdout
        assert "POLICY STORE DEGRADED" in result.stdout

    def test_the_recovery_command_survives_rich_markup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bracketed path must not be eaten by Rich.

        The recovery command is the entire justification for refusing the
        write — an operator meets this needing the fix, not a diagnosis —
        so it is the one string that must survive rendering byte-for-byte.
        **Three** ways to break it, and the earlier version of this test
        certified the third while proving the first two were fixed.

        1. *Markup.* Unescaped, a data dir under ``/tmp/my [staging] dir/``
           renders as ``mv /tmp/my  dir/...`` — Rich eats the brackets.
        2. *Wrapping.* Rich hard-wraps at the console width, so a deep path
           splits the command across lines; a pasted newline is two shell
           commands, neither of them the fix.
        3. *Word-splitting.* The path contains spaces, so an unquoted
           ``mv A B`` is an ``mv`` with **four** operands. Asserting the
           string appears contiguously does not catch this — the old
           assertion passed against a command that could not run.

        So this asserts the property that actually matters: the printed
        command **parses as a shell command** with exactly the two operands
        it should have. That fails on all three.

        A fourth way, and the one that defeated this test itself (#495):
        *colour*. Rich styles parts of a token, so ``mv`` arrives as
        ``\x1b[1mmv`` and ``shlex.split`` sees the escape inside the word.
        Colour is forced here rather than inherited from the ambient run,
        because stripping escapes on a build that emitted none pins
        nothing — the assertion would hold without the coloured renderer
        ever running.
        """
        import shlex

        force_colour(monkeypatch, policy_cli)
        data_dir = tmp_path / "my [staging] dir" / "data"
        (data_dir / "stores").mkdir(parents=True)
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
        policy_file = data_dir / "stores" / "policies.json"
        policy_file.write_text("{ broken", encoding="utf-8")

        result = runner.invoke(app, ["policy", "list"])

        assert result.exit_code == 5
        # 1. Colour really happened, so what follows is about the coloured
        #    renderer and not a plain-path rerun of it.
        rendered = assert_coloured(result.stdout)
        # 2. The payload survives stripping: the bracketed, space-bearing
        #    path is in there whole.
        assert str(policy_file) in rendered, (
            "the path did not survive rendering intact — Rich ate the "
            "bracketed segment, or wrapped the line through the middle of it"
        )
        # 3. And the substantive property: it parses as one shell command
        #    with exactly two operands.
        line = next(
            ln for ln in rendered.splitlines() if ln.strip().startswith("To reset:")
        )
        command = line.split("To reset:", 1)[1].strip()
        assert shlex.split(command) == [
            "mv",
            str(policy_file),
            f"{policy_file}.corrupt",
        ], f"the printed recovery command does not parse as `mv src dst`: {command!r}"

    def test_json_output_is_parseable_with_a_long_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--format json`` is a machine contract (see CLAUDE.md).

        Rich soft-wraps at the terminal width, and a wrap inside a long
        string value produces output ``json.loads`` rejects. The policy
        file's path is easily longer than 80 columns.
        """
        data_dir = tmp_path / ("deeply/" * 12) / "data"
        (data_dir / "stores").mkdir(parents=True)
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))

        result = runner.invoke(app, ["policy", "list", "--format", "json"])

        assert result.exit_code == 0
        assert json.loads(result.stdout.strip())["count"] == 0


class TestEmptyIsNotAlwaysTheSameEmpty:
    """The distinction #413 asks for, made where a human asks the question.

    Enforcement deliberately keeps the two indistinguishable (a per-mutation
    warning on a benign declared-empty file is how operators learn to ignore
    warnings). Here it is asked once, by a person, and it is cheap.
    """

    def test_absent_file_says_nothing_extra(self) -> None:
        result = runner.invoke(app, ["policy", "list"])
        assert result.exit_code == 0
        assert "No policies configured" in result.stdout
        assert "declares an empty policy list" not in result.stdout

    def test_a_file_declaring_zero_policies_says_so(self, tmp_path: Path) -> None:
        _damage(tmp_path, '{"policies": []}')

        result = runner.invoke(app, ["policy", "list"])

        assert result.exit_code == 0
        assert "declares an empty policy list" in result.stdout
        assert "every mutation is permitted" in result.stdout

    def test_json_reports_which_case_it_is(self, tmp_path: Path) -> None:
        absent = runner.invoke(app, ["policy", "list", "--format", "json"])
        payload = json.loads(absent.stdout.strip())
        assert payload["policy_file_present"] is False
        # Present-and-null, not absent: the same shape the REST route uses,
        # so a client never has to distinguish "clean" from "old version of
        # this command".
        assert payload["status"] == "ok"
        assert payload["store_degradation"] is None

        _damage(tmp_path, '{"policies": []}')
        present = runner.invoke(app, ["policy", "list", "--format", "json"])
        assert json.loads(present.stdout.strip())["policy_file_present"] is True

    @pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
    def test_an_unreadable_file_is_present_not_absent(
        self, tmp_path: Path, shape: UnreadablePathShape
    ) -> None:
        """#479 in miniature, at the one surface a human asks this at.

        ``Path.exists()`` reported a symlink loop as ``False``, so the JSON
        said ``policy_file_present: false`` beside ``status: "degraded"`` —
        "there is no file, and it is damaged". The two fields now agree:
        there is a file and it will not load.
        """
        path = tmp_path / "data" / "stores" / "policies.json"

        with unreadable(shape, path):
            result = runner.invoke(app, ["policy", "list", "--format", "json"])

        payload = json.loads(result.stdout.strip())
        assert payload["policy_file_present"] is True
        assert payload["status"] == "degraded"
        assert payload["store_degradation"] is not None
        assert result.exit_code == EXIT_STORE


class TestWritesAreRefusedOnADamagedFile:
    @pytest.mark.parametrize("output_format", ["text", "json"])
    def test_add_refuses_and_leaves_the_bytes_alone(
        self, tmp_path: Path, output_format: str
    ) -> None:
        path = _damage(tmp_path, '{"policys": [{"policy_id": "x"}]}')
        before = path.read_bytes()

        result = runner.invoke(
            app,
            [
                "policy",
                "add",
                "--operation",
                "entity.create",
                "--action",
                "deny",
                "--format",
                output_format,
            ],
        )

        assert result.exit_code == 5, result.stdout
        assert path.read_bytes() == before
        if output_format == "json":
            payload = json.loads(result.stdout.strip())
            assert payload["status"] == "degraded"
            assert payload["store_degradation"]["reason"] == "malformed_envelope"
        else:
            assert "POLICY STORE DEGRADED" in result.stdout

    def test_remove_refuses_rather_than_reporting_not_found(
        self, tmp_path: Path
    ) -> None:
        """Exit 5, not 1.

        On a degraded store "not found" is not an answer the store can
        support — the entry may exist in the file and simply have failed to
        parse — so reporting it would be a wrong answer, not merely an
        unhelpful one.
        """
        path = _damage(tmp_path, "{ broken")
        before = path.read_bytes()

        result = runner.invoke(app, ["policy", "remove", "whatever"])

        assert result.exit_code == 5
        assert path.read_bytes() == before

    def test_show_does_not_claim_a_policy_is_absent(self, tmp_path: Path) -> None:
        _damage(tmp_path, "{ broken")

        result = runner.invoke(app, ["policy", "show", "whatever"])

        assert result.exit_code == 5
        assert "degraded" in result.stdout.lower()

    def test_show_json_carries_the_degradation(self, tmp_path: Path) -> None:
        _damage(tmp_path, "{ broken")

        result = runner.invoke(app, ["policy", "show", "whatever", "--format", "json"])

        assert result.exit_code == 5
        payload = json.loads(result.stdout.strip())
        assert payload["store_degradation"]["reason"] == "malformed_json"


class TestWritesAreRefusedWhenAnotherProcessWroteFirst:
    """The *stale* refusal, at the surface that renders it.

    Every existing test here covers the **degraded** refusal. The stale one
    is a different exception, a different code, a different recovery
    command and a different renderer (``_exit_on_refused_write`` reads the
    exception, because there is no ``LoadDegradation`` to read), and
    nothing exercised it: deleting the ``except StoreWriteRefusedError``
    handler from ``policy add`` **or** from ``policy remove`` left all 266
    targeted tests green. The PR body's "exit 5 on the CLI" for a stale
    store was an unverified claim about dead-as-far-as-the-suite code.

    A stale store is injected rather than raced for. The CLI builds its
    store per command, so the real window is the microseconds between
    ``_get_policy_store()`` and the write — deterministic only by handing
    the command a view that is already behind.
    """

    @staticmethod
    def _policy(**kwargs) -> Policy:
        defaults = {
            "policy_type": PolicyType.MUTATION,
            "scope": PolicyScope(level="global"),
            "rules": [PolicyRule(operation="entity.create", action="deny")],
            "enforcement": Enforcement.ENFORCE,
        }
        defaults.update(kwargs)
        return Policy(**defaults)

    @classmethod
    def _stale_store(
        cls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Policy]:
        """Hand the CLI a store loaded before another process wrote."""
        path = tmp_path / "data" / "stores" / "policies.json"
        PolicyStore(path).add(cls._policy())

        behind = PolicyStore(path)  # loads [A]
        theirs = cls._policy(scope=PolicyScope(level="domain", value="payments"))
        PolicyStore(path).add(theirs)  # file becomes [A, B]

        monkeypatch.setattr("trellis_cli.policy._get_policy_store", lambda: behind)
        return path, theirs

    @pytest.mark.parametrize("output_format", ["text", "json"])
    def test_add_renders_the_refusal_instead_of_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_format: str
    ) -> None:
        path, _ = self._stale_store(tmp_path, monkeypatch)
        before = path.read_bytes()

        result = runner.invoke(
            app,
            [
                "policy",
                "add",
                "--operation",
                "entity.delete",
                "--format",
                output_format,
            ],
        )

        # 5, not 1: an unhandled StoreWriteRefusedError is exit 1 with a
        # traceback and none of the recovery advice.
        assert result.exit_code == 5, result.stdout
        assert path.read_bytes() == before
        if output_format == "json":
            payload = json.loads(result.stdout.strip())
            assert payload["status"] == "refused"
            # Its own code: the operator's next move is to retry, not to go
            # and look at the file.
            assert payload["code"] == "STALE_STORE_WRITE"
            assert payload["recovery"] == "trellis policy list"
        else:
            assert "POLICY WRITE REFUSED" in result.stdout

    def test_remove_of_a_policy_this_view_holds_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "data" / "stores" / "policies.json"
        mine = self._policy()
        PolicyStore(path).add(mine)
        behind = PolicyStore(path)
        PolicyStore(path).add(
            self._policy(scope=PolicyScope(level="domain", value="payments"))
        )
        monkeypatch.setattr("trellis_cli.policy._get_policy_store", lambda: behind)
        before = path.read_bytes()

        result = runner.invoke(app, ["policy", "remove", mine.policy_id])

        assert result.exit_code == 5, result.stdout
        assert "POLICY WRITE REFUSED" in result.stdout
        assert path.read_bytes() == before

    def test_remove_renders_a_refusal_raised_by_the_store_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The backstop the pre-check masks, pinned on its own.

        ``_refuse_stale`` now runs before ``_find_policy``, so it answers
        first and the ``except StoreWriteRefusedError`` around
        ``store.remove`` became undetectable — deleting it left the whole
        suite green. The window it covers is real (another process can
        write between the pre-check and the save) and only reachable by
        injection, which is the same position ``_save``'s own unconditional
        guard is in, and it gets the same treatment: assert it directly
        rather than leave a mechanism nothing can tell is there.
        """
        path = tmp_path / "data" / "stores" / "policies.json"
        mine = self._policy()
        store = PolicyStore(path)
        store.add(mine)
        monkeypatch.setattr("trellis_cli.policy._get_policy_store", lambda: store)

        refusal = StaleStoreWriteError(
            "the file moved under us",
            store="policy",
            path=str(path),
            recovery="trellis policy list",
        )

        def _raise(_policy_id: str) -> bool:
            raise refusal

        monkeypatch.setattr(store, "remove", _raise)

        result = runner.invoke(app, ["policy", "remove", mine.policy_id])

        # 5 with the refusal rendered, not 1 with a traceback.
        assert result.exit_code == 5, result.stdout
        assert "POLICY WRITE REFUSED" in result.stdout

    def test_remove_does_not_report_not_found_for_a_policy_in_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wrong answer ``PolicyStore.remove``'s guard does not reach.

        ``PolicyStore.remove`` refuses ahead of its own membership check so
        that a stale store cannot answer "no such policy" for a policy
        another process added. This command never gets there: it runs its
        *own* ``_find_policy`` first, for prefix matching, and returned
        ``Policy not found`` at exit 1 for a policy sitting in the file —
        the exact wrong answer the store-level fix is written against,
        surviving one layer up. ``DELETE /policies/{id}`` was unaffected; it
        calls ``store.remove`` directly.
        """
        path, theirs = self._stale_store(tmp_path, monkeypatch)

        result = runner.invoke(app, ["policy", "remove", theirs.policy_id])

        assert theirs.policy_id in path.read_text(), "precondition: it is in the file"
        assert result.exit_code == 5, result.stdout
        assert "not found" not in result.stdout.lower()
