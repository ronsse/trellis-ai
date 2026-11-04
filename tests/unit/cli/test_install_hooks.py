"""``trellis admin install-hooks`` and ``trellis admin install-agent-rule``.

Every test works in ``tmp_path``. ``--scope user`` resolves through ``HOME``, so
the tests that use it point ``HOME`` at a temp directory first; nothing here can
reach a real ``~/.claude``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis_cli import hooks_install
from trellis_cli.hooks import COMMAND, HOOK_NAMES
from trellis_cli.hooks_install import (
    BACKUP_SUFFIX,
    RULE_BEGIN,
    SettingsError,
    add_rule,
    has_rule,
    hook_specs,
    install_agent_rule,
    install_hooks,
    own_hook,
    remove_rule,
    uninstall_hooks,
)
from trellis_cli.main import app
from trellis_cli.prompts import agent_rule_text

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[3]

EVENTS = {"UserPromptSubmit", "SubagentStart", "PreToolUse", "Stop", "SubagentStop"}


def _write(path: Path, data: Any, *, indent: int | str = 2, newline: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=indent)
    path.write_text(text + ("\n" if newline else ""), encoding="utf-8")
    return path


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _ours(settings: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (event, entry["command"])
        for event, groups in settings.get("hooks", {}).items()
        for group in groups
        for entry in group["hooks"]
        if own_hook(entry)
    ]


FOREIGN = {
    "model": "demo-model",
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "~/bin/other-stop.sh"}]}],
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "~/bin/audit.sh"}],
            }
        ],
        "Notification": [{"hooks": [{"type": "command", "command": "~/bin/ping.sh"}]}],
    },
}


# ---------------------------------------------------------------------------
# What is installed
# ---------------------------------------------------------------------------


class TestSpecs:
    def test_the_default_set_covers_the_five_events(self):
        specs = hook_specs()
        assert {spec.event for spec in specs} == EVENTS
        assert {spec.hook for spec in specs} == set(HOOK_NAMES)

    def test_edit_deny_off_drops_the_edit_hook(self):
        specs = hook_specs(edit_deny="off")
        assert "PreToolUse" not in {spec.event for spec in specs}

    def test_the_options_reach_the_commands(self):
        by_event = {
            spec.event: spec.command
            for spec in hook_specs(grading_threshold=25, edit_deny="all")
        }
        assert by_event["Stop"] == f"{COMMAND} grade --threshold 25"
        assert by_event["SubagentStop"] == f"{COMMAND} grade --threshold 25"
        assert by_event["PreToolUse"] == f"{COMMAND} edit-guard --scope all"

    def test_the_edit_hook_matches_the_four_edit_tools(self):
        (spec,) = [s for s in hook_specs() if s.event == "PreToolUse"]
        assert spec.matcher == "Edit|Write|MultiEdit|NotebookEdit"

    @pytest.mark.parametrize("threshold", [0, -3])
    def test_a_threshold_below_one_is_refused(self, threshold: int):
        with pytest.raises(ValueError, match="at least 1"):
            hook_specs(grading_threshold=threshold)

    def test_an_unknown_edit_deny_is_refused(self):
        with pytest.raises(ValueError, match="edit-deny"):
            hook_specs(edit_deny="sometimes")

    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            ({"type": "command", "command": "trellis-hook prework"}, "prework"),
            (
                {"type": "command", "command": "trellis-hook grade --threshold 4"},
                "grade",
            ),
            ({"type": "command", "command": "echo trellis-hook prework"}, None),
            ({"type": "command", "command": "trellis-hook"}, None),
            ({"type": "command", "command": "trellis-hook other"}, None),
            ({"type": "command", "command": "trellis-hooks prework"}, None),
            ({"type": "prompt", "command": "trellis-hook prework"}, None),
            ({"type": "command", "command": 5}, None),
            ("trellis-hook prework", None),
            (None, None),
        ],
    )
    def test_an_entry_is_ours_only_by_its_command(self, entry: Any, expected):
        assert own_hook(entry) == expected


# ---------------------------------------------------------------------------
# Merging into settings.json
# ---------------------------------------------------------------------------


class TestInstall:
    def test_a_fresh_file_gets_every_hook(self, tmp_path: Path):
        path = tmp_path / ".claude" / "settings.json"
        report = install_hooks(path)
        assert report.changed
        assert report.written
        assert report.backup_path is None
        assert {event for event, _ in _ours(_load(path))} == EVENTS
        assert {row["status"] for row in report.results} == {"added"}

    def test_foreign_keys_and_hooks_are_preserved(self, tmp_path: Path):
        path = _write(tmp_path / "settings.json", FOREIGN)
        install_hooks(path)
        merged = _load(path)
        assert merged["model"] == FOREIGN["model"]
        assert merged["permissions"] == FOREIGN["permissions"]
        assert merged["hooks"]["Notification"] == FOREIGN["hooks"]["Notification"]
        assert merged["hooks"]["Stop"][0] == FOREIGN["hooks"]["Stop"][0]
        assert merged["hooks"]["PreToolUse"][0] == FOREIGN["hooks"]["PreToolUse"][0]
        assert {event for event, _ in _ours(merged)} == EVENTS

    def test_a_rerun_changes_nothing(self, tmp_path: Path):
        path = _write(tmp_path / "settings.json", FOREIGN)
        install_hooks(path)
        first = path.read_bytes()
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        backup.unlink()
        report = install_hooks(path)
        assert path.read_bytes() == first
        assert not report.changed
        assert not report.written
        assert not backup.exists()
        assert {row["status"] for row in report.results} == {"unchanged"}

    def test_a_change_of_option_updates_in_place_without_duplicates(
        self, tmp_path: Path
    ):
        path = tmp_path / "settings.json"
        install_hooks(path, grading_threshold=10)
        report = install_hooks(path, grading_threshold=30, edit_deny="all")
        ours = _ours(_load(path))
        assert len(ours) == 5
        assert (("Stop", f"{COMMAND} grade --threshold 30")) in ours
        assert ("PreToolUse", f"{COMMAND} edit-guard --scope all") in ours
        statuses = {row["event"]: row["status"] for row in report.results}
        assert statuses["Stop"] == "updated"
        assert statuses["UserPromptSubmit"] == "unchanged"

    def test_turning_the_edit_guard_off_removes_only_that_entry(self, tmp_path: Path):
        path = _write(tmp_path / "settings.json", FOREIGN)
        install_hooks(path)
        report = install_hooks(path, edit_deny="off")
        merged = _load(path)
        assert "edit-guard" not in {
            own_hook(e) for g in merged["hooks"]["PreToolUse"] for e in g["hooks"]
        }
        assert merged["hooks"]["PreToolUse"] == FOREIGN["hooks"]["PreToolUse"]
        assert {"event": "PreToolUse", "hook": "edit-guard", "status": "removed"} in (
            report.results
        )

    def test_a_hand_duplicated_entry_of_ours_is_collapsed(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        install_hooks(path)
        data = _load(path)
        data["hooks"]["Stop"].append(
            {"hooks": [{"type": "command", "command": f"{COMMAND} grade"}]}
        )
        _write(path, data)
        install_hooks(path)
        assert [c for e, c in _ours(_load(path)) if e == "Stop"] == [
            f"{COMMAND} grade --threshold 10"
        ]

    def test_an_entry_of_ours_sharing_a_group_with_a_foreign_hook_is_kept_apart(
        self, tmp_path: Path
    ):
        shared = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {"type": "command", "command": "~/bin/other.sh"},
                            {"type": "command", "command": f"{COMMAND} grade"},
                        ]
                    }
                ]
            }
        }
        path = _write(tmp_path / "settings.json", shared)
        install_hooks(path)
        stop = _load(path)["hooks"]["Stop"]
        commands = [e["command"] for g in stop for e in g["hooks"]]
        assert commands.count("~/bin/other.sh") == 1
        assert commands.count(f"{COMMAND} grade --threshold 10") == 1
        uninstall_hooks(path)
        assert _load(path)["hooks"]["Stop"] == [
            {"hooks": [{"type": "command", "command": "~/bin/other.sh"}]}
        ]

    def test_a_backup_of_the_previous_content_is_kept(self, tmp_path: Path):
        path = _write(tmp_path / "settings.json", FOREIGN)
        before = path.read_bytes()
        report = install_hooks(path)
        assert report.backup_path == path.with_name(path.name + BACKUP_SUFFIX)
        assert report.backup_path.read_bytes() == before

    def test_the_files_indent_and_final_newline_are_kept(self, tmp_path: Path):
        path = _write(tmp_path / "settings.json", {"a": 1}, indent=4, newline=False)
        install_hooks(path)
        text = path.read_text(encoding="utf-8")
        assert not text.endswith("\n")
        assert '\n    "a": 1' in text

    def test_the_files_mode_is_kept(self, tmp_path: Path):
        path = _write(tmp_path / "settings.json", FOREIGN)
        path.chmod(0o600)
        install_hooks(path)
        assert path.stat().st_mode & 0o777 == 0o600

    def test_a_symlinked_settings_file_stays_a_link(self, tmp_path: Path):
        real = _write(tmp_path / "dotfiles" / "settings.json", FOREIGN)
        link = tmp_path / ".claude" / "settings.json"
        link.parent.mkdir()
        link.symlink_to(real)
        install_hooks(link)
        assert link.is_symlink()
        assert {event for event, _ in _ours(_load(real))} == EVENTS

    def test_no_temp_file_is_left_behind(self, tmp_path: Path):
        path = _write(tmp_path / "settings.json", FOREIGN)
        install_hooks(path)
        left = {p.name for p in tmp_path.iterdir()}
        assert left == {"settings.json", "settings.json" + BACKUP_SUFFIX}


class TestMalformedFilesAreRefused:
    @pytest.mark.parametrize(
        "text",
        ["", "{not json", "[]", '"a string"', "null", "\x00", '{"hooks": '],
    )
    def test_a_file_that_is_not_a_json_object_is_never_rewritten(
        self, tmp_path: Path, text: str
    ):
        path = tmp_path / "settings.json"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(SettingsError):
            install_hooks(path)
        assert path.read_text(encoding="utf-8") == text
        assert not path.with_name(path.name + BACKUP_SUFFIX).exists()

    def test_a_file_that_is_not_utf8_is_refused(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_bytes(b"\xff\xfe{}")
        with pytest.raises(SettingsError):
            install_hooks(path)

    @pytest.mark.parametrize(
        "data",
        [
            {"hooks": []},
            {"hooks": "x"},
            {"hooks": {"Stop": "x"}},
            {"hooks": {"UserPromptSubmit": {"hooks": []}}},
        ],
    )
    def test_a_hooks_section_of_the_wrong_type_is_refused(
        self, tmp_path: Path, data: dict[str, Any]
    ):
        path = _write(tmp_path / "settings.json", data)
        before = path.read_bytes()
        with pytest.raises(SettingsError):
            install_hooks(path)
        assert path.read_bytes() == before

    def test_uninstall_refuses_a_malformed_file_too(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text("{oops", encoding="utf-8")
        with pytest.raises(SettingsError):
            uninstall_hooks(path)


class TestDryRun:
    def test_reports_the_result_and_writes_nothing(self, tmp_path: Path):
        path = _write(tmp_path / "settings.json", FOREIGN)
        before = path.read_bytes()
        report = install_hooks(path, dry_run=True)
        assert report.changed
        assert not report.written
        assert path.read_bytes() == before
        assert {event for event, _ in _ours(json.loads(report.result_text))} == EVENTS
        assert not path.with_name(path.name + BACKUP_SUFFIX).exists()

    def test_creates_no_file_and_no_directory(self, tmp_path: Path):
        path = tmp_path / "new" / ".claude" / "settings.json"
        install_hooks(path, dry_run=True)
        assert not (tmp_path / "new").exists()

    def test_an_uninstall_dry_run_writes_nothing(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        install_hooks(path)
        before = path.read_bytes()
        uninstall_hooks(path, dry_run=True)
        assert path.read_bytes() == before


class TestUninstall:
    @pytest.mark.parametrize(
        ("original", "indent", "newline"),
        [
            (FOREIGN, 2, True),
            ({"model": "demo-model"}, 4, True),
            ({"model": "demo-model"}, 2, False),
            ({"hooks": {"Stop": FOREIGN["hooks"]["Stop"]}}, 2, True),
            ({}, 2, True),
        ],
    )
    def test_restores_the_bytes_of_a_file_in_the_installers_layout(
        self, tmp_path: Path, original: dict[str, Any], indent: int, newline: bool
    ):
        path = _write(
            tmp_path / "settings.json", original, indent=indent, newline=newline
        )
        before = path.read_bytes()
        install_hooks(path)
        assert path.read_bytes() != before
        uninstall_hooks(path)
        assert path.read_bytes() == before

    @pytest.mark.parametrize(
        "original",
        [{"hooks": {}}, {"hooks": {"Stop": []}, "model": "demo-model"}],
    )
    def test_empty_containers_it_did_not_empty_are_left_alone(
        self, tmp_path: Path, original: dict[str, Any]
    ):
        path = _write(tmp_path / "settings.json", original)
        before = path.read_bytes()
        report = uninstall_hooks(path)
        assert not report.changed
        assert path.read_bytes() == before

    def test_a_second_uninstall_changes_nothing(self, tmp_path: Path):
        path = _write(tmp_path / "settings.json", FOREIGN)
        install_hooks(path)
        uninstall_hooks(path)
        after = path.read_bytes()
        report = uninstall_hooks(path)
        assert not report.changed
        assert path.read_bytes() == after

    def test_a_missing_file_is_left_missing(self, tmp_path: Path):
        path = tmp_path / "gone" / "settings.json"
        report = uninstall_hooks(path)
        assert not report.changed
        assert not path.parent.exists()

    def test_foreign_hooks_that_mention_ours_by_name_survive(self, tmp_path: Path):
        original = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {"type": "command", "command": "echo trellis-hook grade"}
                        ]
                    }
                ]
            }
        }
        path = _write(tmp_path / "settings.json", original)
        install_hooks(path)
        uninstall_hooks(path)
        assert _load(path) == original


def _layout(data: dict[str, Any], indent: int | str, *, newline: bool = True) -> str:
    """``data`` as the installer lays a file out: its indent, unescaped non-ASCII."""
    return json.dumps(data, indent=indent, ensure_ascii=False) + (
        "\n" if newline else ""
    )


def _round_trip(path: Path) -> str:
    install_hooks(path)
    uninstall_hooks(path)
    return path.read_text(encoding="utf-8")


class TestWhatTheRoundTripPreserves:
    """Each sentence of the ``Round trip`` bullet in ``operations.md``, as behaviour."""

    ORIGINALS: ClassVar[list[str]] = [
        # compact and escaped: not the installer's layout
        '{"model": "demo-model", "name": "caf\\u00e9"}\n',
        # no final newline, four-space indent, a foreign hook
        _layout(FOREIGN, 4, newline=False),
        # a tab indent and a literal non-ASCII character
        _layout({"name": "caf\u00e9", "model": "demo-model"}, "\t"),
    ]

    @pytest.mark.parametrize("original", ORIGINALS)
    def test_content_is_preserved_whatever_the_layout(
        self, tmp_path: Path, original: str
    ):
        path = tmp_path / "settings.json"
        path.write_text(original, encoding="utf-8")
        after = _round_trip(path)
        # Parsed equality ignores key order, so compare the order explicitly too.
        assert json.loads(after) == json.loads(original)
        assert json.dumps(json.loads(after)) == json.dumps(json.loads(original))

    @pytest.mark.parametrize("indent", [2, 4, "\t"])
    @pytest.mark.parametrize("newline", [True, False])
    def test_bytes_come_back_for_a_file_in_the_installers_layout(
        self, tmp_path: Path, indent: int | str, newline: bool
    ):
        original = _layout(
            {"name": "caf\u00e9", "hooks": FOREIGN["hooks"]}, indent, newline=newline
        )
        path = tmp_path / "settings.json"
        path.write_text(original, encoding="utf-8")
        assert _round_trip(path) == original

    def test_the_first_install_re_renders_the_file(self, tmp_path: Path):
        original = '{"model": "demo-model", "name": "caf\\u00e9"}\n'
        path = tmp_path / "settings.json"
        path.write_text(original, encoding="utf-8")
        install_hooks(path)
        installed = path.read_text(encoding="utf-8")
        assert installed.startswith(
            '{\n  "model": "demo-model",\n  "name": "caf\u00e9",'
        )
        uninstall_hooks(path)
        # Content is back, the bytes are not: the layout is the installer's now.
        assert path.read_text(encoding="utf-8") == _layout(json.loads(original), 2)
        assert path.read_text(encoding="utf-8") != original

    @pytest.mark.parametrize(
        ("original", "expected"),
        [
            ({"hooks": {}}, {}),
            ({"hooks": {"Stop": []}, "model": "demo-model"}, {"model": "demo-model"}),
            (
                {
                    "hooks": {
                        "Stop": [],
                        "Notification": FOREIGN["hooks"]["Notification"],
                    }
                },
                {"hooks": {"Notification": FOREIGN["hooks"]["Notification"]}},
            ),
        ],
    )
    def test_an_empty_container_that_existed_before_is_dropped(
        self, tmp_path: Path, original: dict[str, Any], expected: dict[str, Any]
    ):
        path = _write(tmp_path / "settings.json", original)
        assert _round_trip(path) == _layout(expected, 2)

    def test_the_backup_holds_the_previous_write_only(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        backup = tmp_path / f"settings.json{BACKUP_SUFFIX}"
        original = _layout(FOREIGN, 2)
        path.write_text(original, encoding="utf-8")
        install_hooks(path)
        installed = path.read_bytes()
        assert backup.read_bytes() == original.encode()
        uninstall_hooks(path)
        # The original bytes are in the backup no longer: it is the install's file.
        assert backup.read_bytes() == installed
        assert backup.read_bytes() != original.encode()
        # A run that changes nothing writes nothing, backup included.
        uninstall_hooks(path)
        assert backup.read_bytes() == installed

    def test_the_docs_say_what_the_tests_above_pin(self):
        text = (REPO_ROOT / "docs/agent-guide/operations.md").read_text(
            encoding="utf-8"
        )
        flat = " ".join(text.split())
        assert "restores the original bytes" not in flat
        assert "uninstall of a fresh install restores" not in flat
        bullet = next(
            line for line in text.splitlines() if line.startswith("- **Round trip.**")
        )
        for claim in (
            "preserves the file's content",
            "only for a file already in the layout the installer writes",
            "`json.dumps` with the file's detected indent",
            "non-ASCII characters unescaped",
            "The first install re-renders the whole file",
            "An empty container that existed before the install",
            '`"hooks": {}`',
            "is dropped by the round trip",
            "`<file>.trellis-bak` holds the previous write only",
        ):
            assert claim in bullet, claim


# ---------------------------------------------------------------------------
# The agent rule
# ---------------------------------------------------------------------------

AGENT = (
    "---\n"
    "name: reviewer\n"
    "description: Reviews a diff\n"
    "tools: Read, Grep\n"
    "---\n"
    "\n"
    "You review diffs and report defects.\n"
)


class TestAgentRuleText:
    def test_added_once_and_removed_exactly(self):
        rule = agent_rule_text()
        added = add_rule(AGENT, rule)
        assert added.startswith(AGENT)
        assert added.count(RULE_BEGIN) == 1
        assert has_rule(added, rule)
        assert remove_rule(added) == AGENT

    @pytest.mark.parametrize(
        "text",
        [
            AGENT,
            AGENT.rstrip("\n"),
            "",
            "no frontmatter\n",
            AGENT.replace("\n", "\r\n"),
        ],
    )
    def test_round_trips_byte_for_byte(self, text: str):
        rule = agent_rule_text()
        assert remove_rule(add_rule(text, rule)) == text

    def test_a_pasted_copy_of_the_rule_counts_as_present(self):
        rule = agent_rule_text()
        wrapped = AGENT + "\n" + rule.replace(". ", ".\n") + "\n"
        assert has_rule(wrapped, rule)
        assert remove_rule(wrapped) == wrapped

    def test_a_file_without_the_rule_is_not_present(self):
        assert not has_rule(AGENT, agent_rule_text())


class TestInstallAgentRule:
    def _agents(self, tmp_path: Path) -> Path:
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "reviewer.md").write_text(AGENT, encoding="utf-8")
        (agents / "planner.md").write_text(
            AGENT.replace("reviewer", "planner"), "utf-8"
        )
        (agents / "notes.txt").write_text("not an agent", encoding="utf-8")
        return agents

    def test_adds_the_rule_to_each_markdown_file_once(self, tmp_path: Path):
        agents = self._agents(tmp_path)
        report = install_agent_rule(agents, agent_rule_text())
        assert {row["status"] for row in report.files} == {"added"}
        assert {row["file"] for row in report.files} == {"reviewer.md", "planner.md"}
        text = (agents / "reviewer.md").read_text(encoding="utf-8")
        assert text.startswith(AGENT)
        assert (agents / "notes.txt").read_text(encoding="utf-8") == "not an agent"
        again = install_agent_rule(agents, agent_rule_text())
        assert {row["status"] for row in again.files} == {"present"}
        assert (agents / "reviewer.md").read_text(encoding="utf-8") == text

    def test_uninstall_restores_the_files(self, tmp_path: Path):
        agents = self._agents(tmp_path)
        before = {p.name: p.read_bytes() for p in agents.iterdir()}
        install_agent_rule(agents, agent_rule_text())
        report = install_agent_rule(agents, agent_rule_text(), uninstall=True)
        assert {row["status"] for row in report.files} == {"removed"}
        assert {p.name: p.read_bytes() for p in agents.iterdir()} == before
        again = install_agent_rule(agents, agent_rule_text(), uninstall=True)
        assert {row["status"] for row in again.files} == {"absent"}

    def test_dry_run_writes_nothing(self, tmp_path: Path):
        agents = self._agents(tmp_path)
        before = {p.name: p.read_bytes() for p in agents.iterdir()}
        report = install_agent_rule(agents, agent_rule_text(), dry_run=True)
        assert {row["status"] for row in report.files} == {"added"}
        assert {p.name: p.read_bytes() for p in agents.iterdir()} == before

    def test_a_file_that_cannot_be_read_is_reported_and_the_rest_are_done(
        self, tmp_path: Path
    ):
        agents = self._agents(tmp_path)
        (agents / "binary.md").write_bytes(b"\xff\xfe\x00")
        report = install_agent_rule(agents, agent_rule_text())
        statuses = {row["file"]: row["status"] for row in report.files}
        assert statuses["binary.md"] == "failed"
        assert statuses["reviewer.md"] == "added"
        assert [row["file"] for row in report.failed] == ["binary.md"]

    def test_a_missing_directory_is_refused(self, tmp_path: Path):
        with pytest.raises(NotADirectoryError):
            install_agent_rule(tmp_path / "nope", agent_rule_text())

    def test_a_directory_named_like_a_markdown_file_is_skipped(self, tmp_path: Path):
        agents = self._agents(tmp_path)
        (agents / "folder.md").mkdir()
        report = install_agent_rule(agents, agent_rule_text())
        assert "folder.md" not in {row["file"] for row in report.files}


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


def _invoke(*args: str):
    return runner.invoke(app, ["admin", *args])


class TestInstallHooksCommand:
    def test_json_reports_what_was_done(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        result = _invoke("install-hooks", "--settings", str(path), "--format", "json")
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["action"] == "install"
        assert payload["changed"] is True
        assert payload["written"] is True
        assert payload["settings_path"] == str(path)
        assert {row["event"] for row in payload["hooks"]} == EVENTS
        assert {event for event, _ in _ours(_load(path))} == EVENTS

    def test_text_names_the_path_and_each_hook(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        result = _invoke("install-hooks", "--settings", str(path))
        assert result.exit_code == 0, result.output
        text = plain(result.output)
        assert "Hooks installed" in text
        for event in EVENTS:
            assert event in text

    def test_a_rerun_says_nothing_changed(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        _invoke("install-hooks", "--settings", str(path))
        before = path.read_bytes()
        result = _invoke("install-hooks", "--settings", str(path), "--format", "json")
        assert result.exit_code == 0
        assert json.loads(result.output)["changed"] is False
        assert path.read_bytes() == before

    def test_dry_run_json_carries_the_resulting_settings(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        result = _invoke(
            "install-hooks", "--settings", str(path), "--dry-run", "--format", "json"
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["dry_run"] is True
        assert payload["written"] is False
        assert {event for event, _ in _ours(payload["settings"])} == EVENTS
        assert not path.exists()

    def test_dry_run_text_prints_the_resulting_json_alone_on_stdout(
        self, tmp_path: Path
    ):
        path = tmp_path / "settings.json"
        runner_split = CliRunner()
        result = runner_split.invoke(
            app, ["admin", "install-hooks", "--settings", str(path), "--dry-run"]
        )
        assert result.exit_code == 0, result.output
        assert not path.exists()
        assert "UserPromptSubmit" in result.stdout

    def test_the_options_are_applied(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        result = _invoke(
            "install-hooks",
            "--settings",
            str(path),
            "--grading-threshold",
            "25",
            "--edit-deny",
            "all",
        )
        assert result.exit_code == 0, result.output
        ours = _ours(_load(path))
        assert ("Stop", f"{COMMAND} grade --threshold 25") in ours
        assert ("PreToolUse", f"{COMMAND} edit-guard --scope all") in ours

    def test_edit_deny_off_installs_no_edit_hook(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        _invoke("install-hooks", "--settings", str(path), "--edit-deny", "off")
        assert "PreToolUse" not in {event for event, _ in _ours(_load(path))}

    def test_uninstall_removes_exactly_what_was_added(self, tmp_path: Path):
        path = _write(tmp_path / "settings.json", FOREIGN)
        before = path.read_bytes()
        _invoke("install-hooks", "--settings", str(path))
        result = _invoke(
            "install-hooks", "--settings", str(path), "--uninstall", "--format", "json"
        )
        assert result.exit_code == 0
        assert json.loads(result.output)["action"] == "uninstall"
        assert path.read_bytes() == before

    def test_scope_user_resolves_through_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _invoke("install-hooks", "--scope", "user", "--format", "json")
        assert result.exit_code == 0, result.output
        target = tmp_path / ".claude" / "settings.json"
        assert json.loads(result.output)["settings_path"] == str(target)
        assert target.exists()

    def test_scope_project_resolves_through_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        result = _invoke("install-hooks", "--scope", "project")
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".claude" / "settings.json").exists()

    @pytest.mark.parametrize(
        "extra",
        [
            ["--scope", "global"],
            ["--grading-threshold", "0"],
            ["--edit-deny", "maybe"],
            ["--format", "yaml"],
        ],
    )
    def test_a_bad_option_exits_2_and_writes_nothing(
        self, tmp_path: Path, extra: list[str]
    ):
        path = tmp_path / "settings.json"
        args = ["install-hooks"]
        if "--scope" not in extra:
            args += ["--settings", str(path)]
        result = _invoke(*args, *extra)
        assert result.exit_code == 2, result.output
        assert not path.exists()

    def test_scope_and_settings_together_are_refused(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        result = _invoke("install-hooks", "--scope", "user", "--settings", str(path))
        assert result.exit_code == 2
        assert not path.exists()

    def test_a_malformed_file_is_refused_with_exit_2_and_left_alone(
        self, tmp_path: Path
    ):
        path = tmp_path / "settings.json"
        path.write_text("{broken", encoding="utf-8")
        result = _invoke("install-hooks", "--settings", str(path), "--format", "json")
        assert result.exit_code == 2
        assert json.loads(result.output)["status"] == "error"
        assert path.read_text(encoding="utf-8") == "{broken"

    def test_the_exit_code_never_depends_on_the_format(self, tmp_path: Path):
        scenarios = {}
        good = tmp_path / "good.json"
        bad = tmp_path / "bad.json"
        bad.write_text("[", encoding="utf-8")
        wrong_type = _write(tmp_path / "wrong.json", {"hooks": []})
        for label, args in {
            "fresh": ["--settings", str(tmp_path / "fresh.json")],
            "rerun": ["--settings", str(good)],
            "malformed": ["--settings", str(bad)],
            "wrong-type": ["--settings", str(wrong_type)],
            "dry-run": ["--settings", str(tmp_path / "dry.json"), "--dry-run"],
            "uninstall": ["--settings", str(good), "--uninstall"],
            "bad-option": ["--settings", str(good), "--edit-deny", "maybe"],
        }.items():
            if label == "rerun":
                _invoke("install-hooks", "--settings", str(good))
            codes = {
                fmt: _invoke("install-hooks", *args, "--format", fmt).exit_code
                for fmt in ("text", "json")
            }
            scenarios[label] = codes
        for label, codes in scenarios.items():
            assert codes["text"] == codes["json"], (label, codes)
        assert scenarios["malformed"]["json"] == 2
        assert scenarios["fresh"]["json"] == 0

    def test_an_unwritable_destination_exits_1_in_both_formats(self, tmp_path: Path):
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")
        target = blocker / "settings.json"
        codes = {
            fmt: _invoke(
                "install-hooks", "--settings", str(target), "--format", fmt
            ).exit_code
            for fmt in ("text", "json")
        }
        assert codes == {"text": 1, "json": 1}

    def test_the_path_is_printed_verbatim_even_when_it_looks_like_markup(
        self, tmp_path: Path
    ):
        path = tmp_path / "[bold]dir:smile:" / "settings.json"
        result = _invoke("install-hooks", "--settings", str(path))
        assert result.exit_code == 0, result.output
        assert str(path.parent.name) in plain(result.output).replace("\n", "")


class TestInstallAgentRuleCommand:
    def _agents(self, tmp_path: Path) -> Path:
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "reviewer.md").write_text(AGENT, encoding="utf-8")
        return agents

    def test_json_reports_each_file(self, tmp_path: Path):
        agents = self._agents(tmp_path)
        result = _invoke(
            "install-agent-rule", "--agents-dir", str(agents), "--format", "json"
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["files"] == [{"file": "reviewer.md", "status": "added"}]
        assert RULE_BEGIN in (agents / "reviewer.md").read_text(encoding="utf-8")

    def test_text_output_and_the_uninstall_round_trip(self, tmp_path: Path):
        agents = self._agents(tmp_path)
        result = _invoke("install-agent-rule", "--agents-dir", str(agents))
        assert result.exit_code == 0
        assert "reviewer.md" in plain(result.output)
        _invoke("install-agent-rule", "--agents-dir", str(agents), "--uninstall")
        assert (agents / "reviewer.md").read_text(encoding="utf-8") == AGENT

    def test_dry_run_writes_nothing(self, tmp_path: Path):
        agents = self._agents(tmp_path)
        result = _invoke("install-agent-rule", "--agents-dir", str(agents), "--dry-run")
        assert result.exit_code == 0
        assert (agents / "reviewer.md").read_text(encoding="utf-8") == AGENT

    def test_the_agents_dir_is_required(self):
        assert _invoke("install-agent-rule").exit_code == 2

    def test_a_missing_directory_exits_2_in_both_formats(self, tmp_path: Path):
        codes = {
            fmt: _invoke(
                "install-agent-rule",
                "--agents-dir",
                str(tmp_path / "nope"),
                "--format",
                fmt,
            ).exit_code
            for fmt in ("text", "json")
        }
        assert codes == {"text": 2, "json": 2}

    def test_a_file_that_cannot_be_read_exits_1_in_both_formats(self, tmp_path: Path):
        agents = self._agents(tmp_path)
        (agents / "binary.md").write_bytes(b"\xff\xfe\x00")
        results = {
            fmt: _invoke(
                "install-agent-rule", "--agents-dir", str(agents), "--format", fmt
            )
            for fmt in ("text", "json")
        }
        assert {fmt: r.exit_code for fmt, r in results.items()} == {
            "text": 1,
            "json": 1,
        }
        assert json.loads(results["json"].output)["status"] == "partial"

    def test_the_helper_module_is_the_one_the_command_uses(self):
        assert hooks_install.install_agent_rule is install_agent_rule
