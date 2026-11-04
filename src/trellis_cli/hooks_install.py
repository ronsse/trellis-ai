"""Install the packaged hooks into a Claude Code ``settings.json``, and the agent
rule into agent definitions.

Two jobs, both small and both reversible:

* :func:`install_hooks` / :func:`uninstall_hooks` merge the ``trellis-hook``
  entries (see :mod:`trellis_cli.hooks`) into one settings file. An entry is
  recognised as ours by its command, which starts ``trellis-hook <name>``;
  Claude Code's settings schema has no room for a private marker key, and a
  command that names the packaged executable is one a person reading the file
  understands. The merge preserves every key and every foreign hook, is
  idempotent (a re-run writes nothing), and uninstalling removes exactly the
  entries it added, plus any container it emptied by doing so (which includes
  an empty one that was there before the install). An install then uninstall
  keeps the content, and the bytes only for a file already in the layout
  :func:`render_settings` writes. A settings file
  that is not a JSON object is refused and never rewritten. A write goes to a
  temp file in the same directory and is renamed over the target, after the
  previous content is copied to ``<name>.trellis-bak``.
* :func:`install_agent_rule` / :func:`uninstall_agent_rule` append the agent
  rule to each agent definition in a directory, inside a marker-delimited block
  at the end of the file, so the frontmatter is never touched and the block is
  removable byte for byte.

Nothing here runs a hook, reads Trellis config or touches ``~/.claude`` unless
the caller passes that path.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trellis_cli.hooks import (
    COMMAND,
    DEFAULT_EDIT_SCOPE,
    DEFAULT_GRADING_THRESHOLD,
    EDIT_DENY_SCOPES,
    EDIT_MATCHER,
    HOOK_EDIT_GUARD,
    HOOK_GRADE,
    HOOK_NAMES,
    HOOK_PREWORK,
)

#: Seconds Claude Code lets a hook run. The hooks finish in well under one.
HOOK_TIMEOUT_SECONDS = 10

#: ``--edit-deny`` values: where the first-edit soft deny applies.
EDIT_DENY_CHOICES: tuple[str, ...] = (*EDIT_DENY_SCOPES, "off")

BACKUP_SUFFIX = ".trellis-bak"

STATUS_ADDED = "added"
STATUS_UPDATED = "updated"
STATUS_UNCHANGED = "unchanged"
STATUS_REMOVED = "removed"
STATUS_ABSENT = "absent"
STATUS_PRESENT = "present"
STATUS_FAILED = "failed"


class SettingsError(ValueError):
    """The settings file is not something this installer will touch."""


# ---------------------------------------------------------------------------
# Hook entries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookSpec:
    """One entry the installer manages: an event, an optional matcher, a command."""

    event: str
    hook: str
    command: str
    matcher: str | None = None

    def entry(self) -> dict[str, Any]:
        return {
            "type": "command",
            "command": self.command,
            "timeout": HOOK_TIMEOUT_SECONDS,
        }

    def group(self) -> dict[str, Any]:
        group: dict[str, Any] = {}
        if self.matcher is not None:
            group["matcher"] = self.matcher
        group["hooks"] = [self.entry()]
        return group


def hook_specs(
    *,
    grading_threshold: int = DEFAULT_GRADING_THRESHOLD,
    edit_deny: str = DEFAULT_EDIT_SCOPE,
) -> list[HookSpec]:
    """The entries to install for these options.

    Raises:
        ValueError: ``grading_threshold`` below 1 or an unknown ``edit_deny``.
    """
    if grading_threshold < 1:
        msg = f"grading threshold must be at least 1, got {grading_threshold}"
        raise ValueError(msg)
    if edit_deny not in EDIT_DENY_CHOICES:
        msg = f"edit-deny must be one of {', '.join(EDIT_DENY_CHOICES)}"
        raise ValueError(msg)
    prework = f"{COMMAND} {HOOK_PREWORK}"
    grade = f"{COMMAND} {HOOK_GRADE} --threshold {grading_threshold}"
    specs = [
        HookSpec("UserPromptSubmit", HOOK_PREWORK, prework),
        HookSpec("SubagentStart", HOOK_PREWORK, prework),
    ]
    if edit_deny != "off":
        specs.append(
            HookSpec(
                "PreToolUse",
                HOOK_EDIT_GUARD,
                f"{COMMAND} {HOOK_EDIT_GUARD} --scope {edit_deny}",
                EDIT_MATCHER,
            )
        )
    specs += [
        HookSpec("Stop", HOOK_GRADE, grade),
        HookSpec("SubagentStop", HOOK_GRADE, grade),
    ]
    return specs


#: ``trellis-hook`` and the hook name: the least a command of ours holds.
_MIN_COMMAND_TOKENS = 2


def own_hook(entry: Any) -> str | None:
    """The hook name when ``entry`` is one of ours, else ``None``."""
    if not isinstance(entry, dict) or entry.get("type") != "command":
        return None
    command = entry.get("command")
    if not isinstance(command, str):
        return None
    tokens = command.split()
    if (
        len(tokens) >= _MIN_COMMAND_TOKENS
        and tokens[0] == COMMAND
        and tokens[1] in HOOK_NAMES
    ):
        return tokens[1]
    return None


def _groups(hooks: dict[str, Any], event: str) -> list[Any]:
    """The matcher groups of ``event``; an event that is not a list is refused."""
    groups = hooks.get(event)
    if groups is None:
        return []
    if not isinstance(groups, list):
        msg = f"hooks.{event} is not a list"
        raise SettingsError(msg)
    return groups


def _own_entries(hooks: dict[str, Any]) -> list[tuple[str, int, int, str]]:
    """Every entry of ours as ``(event, group index, entry index, hook name)``."""
    found: list[tuple[str, int, int, str]] = []
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group_index, group in enumerate(groups):
            members = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(members, list):
                continue
            for entry_index, entry in enumerate(members):
                name = own_hook(entry)
                if name is not None:
                    found.append((event, group_index, entry_index, name))
    return found


def _remove_entry(
    hooks: dict[str, Any], event: str, group_index: int, entry_index: int
) -> None:
    """Delete one entry, and the group and event it leaves empty."""
    groups = hooks[event]
    members = groups[group_index]["hooks"]
    del members[entry_index]
    if not members:
        del groups[group_index]
    if not groups:
        del hooks[event]


def _hooks_object(settings: dict[str, Any]) -> dict[str, Any] | None:
    hooks = settings.get("hooks")
    if hooks is None:
        return None
    if not isinstance(hooks, dict):
        msg = "'hooks' is not an object"
        raise SettingsError(msg)
    return hooks


def apply_specs(
    settings: dict[str, Any], specs: list[HookSpec]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """``settings`` with ``specs`` merged in, and one result row per spec.

    Pure: ``settings`` is not modified. A spec whose entry already exists with
    the same command and timeout is ``unchanged``; one that exists with other
    options is updated in place, keeping its position; a missing one is
    appended as a new matcher group. Entries of ours that no spec names (an
    edit guard installed earlier and now switched off) are removed, and a
    second copy of one is dropped.

    Raises:
        SettingsError: ``hooks`` or an event this installer writes to has the
            wrong type.
    """
    new = copy.deepcopy(settings)
    hooks = _hooks_object(new)
    results: list[dict[str, str]] = []
    created_hooks = hooks is None
    if hooks is None:
        hooks = {}
    for spec in specs:
        _groups(hooks, spec.event)
        results.append(
            {"event": spec.event, "hook": spec.hook, "status": _merge(hooks, spec)}
        )
    wanted = {(spec.event, spec.hook) for spec in specs}
    # Removing shifts indexes, so rescan after each removal.
    while True:
        stale = [
            found for found in _own_entries(hooks) if (found[0], found[3]) not in wanted
        ]
        if not stale:
            break
        event, group_index, entry_index, name = stale[0]
        _remove_entry(hooks, event, group_index, entry_index)
        results.append({"event": event, "hook": name, "status": STATUS_REMOVED})
    if created_hooks and hooks:
        new["hooks"] = hooks
    return new, results


def _merge(hooks: dict[str, Any], spec: HookSpec) -> str:
    """Bring ``hooks`` to hold exactly one entry for ``spec``; return its status."""
    while True:
        mine = [
            found
            for found in _own_entries(hooks)
            if found[0] == spec.event and found[3] == spec.hook
        ]
        if len(mine) <= 1:
            break
        # Keep the first; a duplicate is ours and only ours to drop.
        _, group_index, entry_index, _ = mine[-1]
        _remove_entry(hooks, spec.event, group_index, entry_index)
    if not mine:
        hooks.setdefault(spec.event, []).append(spec.group())
        return STATUS_ADDED
    _, group_index, entry_index, _ = mine[0]
    group = hooks[spec.event][group_index]
    entry = group["hooks"][entry_index]
    matcher_ok = (group.get("matcher") or None) == spec.matcher
    entry_ok = (
        entry.get("command") == spec.command
        and entry.get("timeout") == HOOK_TIMEOUT_SECONDS
    )
    if matcher_ok and entry_ok:
        return STATUS_UNCHANGED
    if matcher_ok or len(group["hooks"]) == 1:
        entry["command"] = spec.command
        entry["timeout"] = HOOK_TIMEOUT_SECONDS
        if not matcher_ok:
            if spec.matcher is None:
                group.pop("matcher", None)
            else:
                group["matcher"] = spec.matcher
        return STATUS_UPDATED
    # Our entry shares a group with a foreign hook under another matcher.
    _remove_entry(hooks, spec.event, group_index, entry_index)
    hooks.setdefault(spec.event, []).append(spec.group())
    return STATUS_UPDATED


def remove_specs(
    settings: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """``settings`` without any entry of ours, and one result row per entry removed.

    A container (matcher group, event list, ``hooks`` object) is dropped only
    when removing our entry is what emptied it.
    """
    new = copy.deepcopy(settings)
    hooks = _hooks_object(new)
    results: list[dict[str, str]] = []
    if hooks is None:
        return new, results
    had_entries = bool(hooks)
    while True:
        found = _own_entries(hooks)
        if not found:
            break
        event, group_index, entry_index, name = found[0]
        _remove_entry(hooks, event, group_index, entry_index)
        results.append({"event": event, "hook": name, "status": STATUS_REMOVED})
    if had_entries and results and not hooks:
        del new["hooks"]
    return new, results


# ---------------------------------------------------------------------------
# Settings file I/O
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingsFile:
    """A settings file as read: its data and how its text was laid out."""

    path: Path
    data: dict[str, Any]
    exists: bool
    indent: str = "  "
    final_newline: bool = True


def read_settings(path: Path) -> SettingsFile:
    """Read ``path``; a missing file is an empty settings object.

    Raises:
        SettingsError: The file is empty, is not valid JSON or is not an
            object. Nothing is guessed and nothing is rewritten.
        OSError: The file exists and cannot be read.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return SettingsFile(path=path, data={}, exists=False)
    try:
        text = raw.decode("utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, ValueError) as exc:
        msg = f"{path} is not valid JSON ({type(exc).__name__}); fix or remove it"
        raise SettingsError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{path} is not a JSON object; fix or remove it"
        raise SettingsError(msg)
    indented = re.search(r"^([ \t]+)\S", text, re.MULTILINE)
    return SettingsFile(
        path=path,
        data=data,
        exists=True,
        indent=indented.group(1) if indented else "  ",
        final_newline=text.endswith("\n"),
    )


def render_settings(data: dict[str, Any], source: SettingsFile) -> str:
    """``data`` as JSON in the layout ``source`` was read with."""
    text = json.dumps(data, indent=source.indent, ensure_ascii=False)
    return text + "\n" if source.final_newline else text


def atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` by temp file and rename, keeping its mode.

    A symlink is followed, so a settings file kept in a dotfiles repo stays a
    link to it.
    """
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            shutil.copymode(target, temp)
        temp.replace(target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


@dataclass
class HooksReport:
    """What an install or uninstall did, or would do under ``--dry-run``."""

    settings_path: Path
    action: str
    dry_run: bool
    changed: bool
    written: bool
    results: list[dict[str, str]]
    result_text: str
    backup_path: Path | None = None
    command_on_path: bool = True


def _finish(
    source: SettingsFile,
    new: dict[str, Any],
    results: list[dict[str, str]],
    *,
    action: str,
    dry_run: bool,
) -> HooksReport:
    changed = new != source.data
    text = (
        render_settings(new, source)
        if changed
        else render_settings(source.data, source)
    )
    report = HooksReport(
        settings_path=source.path,
        action=action,
        dry_run=dry_run,
        changed=changed,
        written=False,
        results=results,
        result_text=text,
        command_on_path=shutil.which(COMMAND) is not None,
    )
    if not changed or dry_run:
        return report
    if source.exists:
        backup = source.path.resolve().with_name(
            source.path.resolve().name + BACKUP_SUFFIX
        )
        shutil.copy2(source.path.resolve(), backup)
        report.backup_path = backup
    atomic_write(source.path, text.encode("utf-8"))
    report.written = True
    return report


def install_hooks(
    path: Path,
    *,
    grading_threshold: int = DEFAULT_GRADING_THRESHOLD,
    edit_deny: str = DEFAULT_EDIT_SCOPE,
    dry_run: bool = False,
) -> HooksReport:
    """Merge the packaged hooks into the settings file at ``path``.

    Raises:
        SettingsError: The file is malformed (it is left as it was).
        ValueError: A bad option value.
        OSError: The file cannot be read or written.
    """
    specs = hook_specs(grading_threshold=grading_threshold, edit_deny=edit_deny)
    source = read_settings(path)
    new, results = apply_specs(source.data, specs)
    return _finish(source, new, results, action="install", dry_run=dry_run)


def uninstall_hooks(path: Path, *, dry_run: bool = False) -> HooksReport:
    """Remove exactly the entries :func:`install_hooks` added from ``path``."""
    source = read_settings(path)
    new, results = remove_specs(source.data)
    return _finish(source, new, results, action="uninstall", dry_run=dry_run)


# ---------------------------------------------------------------------------
# Agent rule
# ---------------------------------------------------------------------------

RULE_BEGIN = "<!-- trellis-agent-rule:begin -->"
#: The begin marker when the file had no final newline, so removal can restore it.
RULE_BEGIN_NO_EOL = "<!-- trellis-agent-rule:begin no-final-newline -->"
RULE_END = "<!-- trellis-agent-rule:end -->"

_BLOCK = re.compile(
    rf"(?P<begin>{re.escape(RULE_BEGIN)}|{re.escape(RULE_BEGIN_NO_EOL)})\n"
    rf".*?{re.escape(RULE_END)}\n?",
    re.DOTALL,
)


def _squash(text: str) -> str:
    return " ".join(text.split())


def has_rule(text: str, rule: str) -> bool:
    """Whether ``text`` carries the rule, in a block of ours or pasted by hand."""
    return (
        RULE_BEGIN in text
        or RULE_BEGIN_NO_EOL in text
        or (_squash(rule) in _squash(text))
    )


def add_rule(text: str, rule: str) -> str:
    """``text`` with the rule appended in a marker-delimited block."""
    body = rule.rstrip("\n")
    if text == "":
        return f"{RULE_BEGIN}\n{body}\n{RULE_END}\n"
    if text.endswith("\n"):
        return f"{text}\n{RULE_BEGIN}\n{body}\n{RULE_END}\n"
    return f"{text}\n\n{RULE_BEGIN_NO_EOL}\n{body}\n{RULE_END}\n"


def remove_rule(text: str) -> str:
    """``text`` without the blocks :func:`add_rule` wrote, byte for byte."""
    while True:
        found = _BLOCK.search(text)
        if found is None:
            return text
        before = text[: found.start()]
        separator = "\n\n" if found.group("begin") == RULE_BEGIN_NO_EOL else "\n"
        before = before.removesuffix(separator)
        text = before + text[found.end() :]


@dataclass
class AgentRuleReport:
    """Per-file outcome of an agent-rule install or uninstall."""

    agents_dir: Path
    action: str
    dry_run: bool
    files: list[dict[str, str]] = field(default_factory=list)

    @property
    def failed(self) -> list[dict[str, str]]:
        return [entry for entry in self.files if entry["status"] == STATUS_FAILED]


def _process_agent_file(
    path: Path, rule: str, *, uninstall: bool, dry_run: bool
) -> str:
    text = path.read_bytes().decode("utf-8")
    if uninstall:
        new = remove_rule(text)
        status_changed, status_same = STATUS_REMOVED, STATUS_ABSENT
    else:
        if has_rule(text, rule):
            return STATUS_PRESENT
        new = add_rule(text, rule)
        status_changed, status_same = STATUS_ADDED, STATUS_PRESENT
    if new == text:
        return status_same
    if not dry_run:
        atomic_write(path, new.encode("utf-8"))
    return status_changed


def install_agent_rule(
    agents_dir: Path, rule: str, *, uninstall: bool = False, dry_run: bool = False
) -> AgentRuleReport:
    """Add (or remove) the agent rule in every ``*.md`` directly under ``agents_dir``.

    A file that already carries the rule, in a block of ours or pasted by hand,
    is left alone; uninstall removes only blocks of ours. One unreadable file is
    reported as ``failed`` and the rest are still processed.

    Raises:
        NotADirectoryError: ``agents_dir`` is not a directory.
    """
    if not agents_dir.is_dir():
        msg = f"{agents_dir} is not a directory"
        raise NotADirectoryError(msg)
    report = AgentRuleReport(
        agents_dir=agents_dir,
        action="uninstall" if uninstall else "install",
        dry_run=dry_run,
    )
    for path in sorted(agents_dir.glob("*.md")):
        if not path.is_file():
            continue
        try:
            status = _process_agent_file(
                path, rule, uninstall=uninstall, dry_run=dry_run
            )
            report.files.append({"file": path.name, "status": status})
        except (OSError, UnicodeDecodeError) as exc:
            report.files.append(
                {
                    "file": path.name,
                    "status": STATUS_FAILED,
                    "error": type(exc).__name__,
                }
            )
    return report
