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
        policy_id = _add_policy()
        result = runner.invoke(app, ["policy", "show", policy_id, "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["policy_id"] == policy_id

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

    def test_the_recovery_command_survives_rich_markup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bracketed path must not be eaten by Rich.

        The recovery command is the entire justification for refusing the
        write — an operator meets this needing the fix, not a diagnosis —
        so it is the one string that must survive rendering byte-for-byte.
        Two ways to break it, both asserted. Unescaped, a data dir under
        ``/tmp/my [staging] dir/`` renders as ``mv /tmp/my  dir/...``.
        Hard-wrapped, the same command splits across two lines — and a
        pasted newline is two shell commands, neither of them the fix.
        Either way it is an unrunnable command printed to the operator *as*
        the fix, silently.
        """
        data_dir = tmp_path / "my [staging] dir" / "data"
        (data_dir / "stores").mkdir(parents=True)
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
        policy_file = data_dir / "stores" / "policies.json"
        policy_file.write_text("{ broken", encoding="utf-8")

        result = runner.invoke(app, ["policy", "list"])

        assert result.exit_code == 5
        # The exact command, contiguous — brackets intact and no newline in
        # the middle of it. The path here is comfortably past 80 columns, so
        # this fails on either defect.
        assert f"mv {policy_file} {policy_file}.corrupt" in result.stdout

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
        assert json.loads(absent.stdout.strip())["policy_file_present"] is False

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
