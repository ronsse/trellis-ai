"""Tests for policy CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

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
        """
        import shlex

        data_dir = tmp_path / "my [staging] dir" / "data"
        (data_dir / "stores").mkdir(parents=True)
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
        policy_file = data_dir / "stores" / "policies.json"
        policy_file.write_text("{ broken", encoding="utf-8")

        result = runner.invoke(app, ["policy", "list"])

        assert result.exit_code == 5
        line = next(
            ln
            for ln in result.stdout.splitlines()
            if ln.strip().startswith("To reset:")
        )
        command = line.split("To reset:", 1)[1].strip()
        assert shlex.split(command) == [
            "mv",
            str(policy_file),
            f"{policy_file}.corrupt",
        ]

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
