"""``trellis admin install-hooks`` and ``trellis admin install-agent-rule``.

Two small installers for the harness side of the loop. An agent retrieves from
Trellis only when its harness tells it to; these commands put that telling in
place without hand-editing JSON or markdown:

* ``install-hooks`` merges the packaged Claude Code hooks (see
  :mod:`trellis_cli.hooks`) into one ``settings.json``.
* ``install-agent-rule`` appends the agent rule to each agent definition in a
  directory, inside a marker-delimited block.

Both are idempotent, both support ``--dry-run`` and ``--uninstall``, and both
touch only the path they are given (``--scope user`` is ``~/.claude``, so it is
the caller's choice to name it). Exit codes follow :mod:`trellis_cli.exit_codes`
and are computed below the ``--format`` branch, never inside it
(``tests/unit/test_format_exit_parity_rule.py``):

* :data:`EXIT_OK` (0): done, or nothing to do.
* :data:`EXIT_VALIDATION` (2): a bad option, a malformed settings file or a
  directory that does not exist. Nothing was written.
* :data:`EXIT_INTERNAL` (1): a file could not be read or written.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.markup import escape

from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_OK, EXIT_VALIDATION
from trellis_cli.hooks import DEFAULT_EDIT_SCOPE, DEFAULT_GRADING_THRESHOLD
from trellis_cli.hooks_install import (
    EDIT_DENY_CHOICES,
    STATUS_FAILED,
    SettingsError,
    install_agent_rule,
    install_hooks,
    uninstall_hooks,
)
from trellis_cli.output import build_console, emit_json, emit_machine_text
from trellis_cli.prompts import agent_rule_text

console = build_console()
err_console = build_console(stderr=True)

_SCOPES = ("user", "project")


def _refuse(msg: str, output_format: str) -> NoReturn:
    """Print an invalid-option refusal in the chosen format and exit 2."""
    if output_format == "json":
        emit_json({"status": "error", "error": msg})
    else:
        err_console.print(f"[red]Error:[/red] {escape(msg)}", soft_wrap=True)
    raise typer.Exit(EXIT_VALIDATION)


def _settings_path(
    scope: str | None, settings: Path | None, output_format: str
) -> Path:
    if scope is not None and settings is not None:
        _refuse("pass --scope or --settings, not both", output_format)
    if settings is not None:
        return settings
    chosen = scope or "user"
    if chosen == "user":
        return Path.home() / ".claude" / "settings.json"
    if chosen == "project":
        return Path.cwd() / ".claude" / "settings.json"
    return _refuse(
        f"scope must be one of {', '.join(_SCOPES)}, got {chosen!r}", output_format
    )


def _hook_rows(results: list[dict[str, str]]) -> list[dict[str, str]]:
    return [dict(row) for row in results]


def install_hooks_command(
    scope: str | None = typer.Option(
        None,
        "--scope",
        help="user (~/.claude/settings.json) or project (./.claude/settings.json).",
    ),
    settings: Path | None = typer.Option(  # noqa: B008 - typer option default
        None,
        "--settings",
        help="Path of the settings JSON to edit (instead of --scope).",
    ),
    grading_threshold: int = typer.Option(
        DEFAULT_GRADING_THRESHOLD,
        "--grading-threshold",
        help="Tool uses before a finished session is asked to grade its pack.",
    ),
    edit_deny: str = typer.Option(
        DEFAULT_EDIT_SCOPE,
        "--edit-deny",
        help=(
            "Where the first-edit soft deny applies: subagents (default), all"
            " sessions, or off (no edit hook)."
        ),
    ),
    uninstall: bool = typer.Option(
        False, "--uninstall", help="Remove exactly the entries this command added."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the resulting settings JSON and write nothing.",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Merge the packaged Claude Code hooks into a settings file.

    Installs a pre-work nudge (UserPromptSubmit, SubagentStart), a first-edit
    soft deny (PreToolUse) and a grading ask (Stop, SubagentStop). Every other
    key and hook in the file is preserved, a re-run changes nothing, and
    --uninstall removes exactly the entries added. A settings file that is not
    a JSON object is refused and left as it was; a write is atomic and keeps
    the previous content in <file>.trellis-bak.

    The hooks raise retrieval rates. A deployment that runs a holdout should
    install them at a window boundary.
    """
    if output_format not in ("text", "json"):
        _refuse(f"format must be text or json, got {output_format!r}", "text")
    if grading_threshold < 1:
        _refuse("--grading-threshold must be at least 1", output_format)
    if edit_deny not in EDIT_DENY_CHOICES:
        _refuse(
            f"--edit-deny must be one of {', '.join(EDIT_DENY_CHOICES)}, got"
            f" {edit_deny!r}",
            output_format,
        )
    path = _settings_path(scope, settings, output_format)

    payload: dict[str, Any]
    exit_code = EXIT_OK
    text_result = ""
    try:
        report = (
            uninstall_hooks(path, dry_run=dry_run)
            if uninstall
            else install_hooks(
                path,
                grading_threshold=grading_threshold,
                edit_deny=edit_deny,
                dry_run=dry_run,
            )
        )
    except SettingsError as exc:
        payload = {"status": "error", "error": str(exc), "settings_path": str(path)}
        exit_code = EXIT_VALIDATION
    except OSError as exc:
        payload = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "settings_path": str(path),
        }
        exit_code = EXIT_INTERNAL
    else:
        payload = {
            "status": "ok",
            "action": report.action,
            "dry_run": report.dry_run,
            "settings_path": str(report.settings_path),
            "changed": report.changed,
            "written": report.written,
            "backup_path": str(report.backup_path) if report.backup_path else None,
            "hooks": _hook_rows(report.results),
            "command_on_path": report.command_on_path,
        }
        text_result = report.result_text

    if output_format == "json":
        if payload["status"] == "ok" and dry_run:
            payload["settings"] = _as_object(text_result)
        emit_json(payload)
    elif payload["status"] != "ok":
        err_console.print(
            f"[red]Error:[/red] {escape(payload['error'])}", soft_wrap=True
        )
    else:
        _render_hooks_text(payload, text_result)
    if exit_code != EXIT_OK:
        raise typer.Exit(code=exit_code)


def _as_object(text: str) -> dict[str, Any]:
    loaded = json.loads(text)
    return loaded if isinstance(loaded, dict) else {}


def _render_hooks_text(payload: dict[str, Any], result_text: str) -> None:
    if payload["dry_run"]:
        # The resulting file alone on stdout, so it can be piped; the summary
        # goes to stderr.
        emit_machine_text(result_text.rstrip("\n"))
        target = err_console
    else:
        target = console
    where = escape(payload["settings_path"])
    verb = "uninstall" if payload["action"] == "uninstall" else "install"
    if payload["dry_run"]:
        change = "would change" if payload["changed"] else "would not change"
        target.print(
            f"[yellow]Dry run:[/yellow] {verb} {change} {where}", soft_wrap=True
        )
    elif payload["changed"]:
        target.print(f"[green]Hooks {verb}ed:[/green] {where}", soft_wrap=True)
    else:
        target.print(f"[green]Nothing to change:[/green] {where}", soft_wrap=True)
    for row in payload["hooks"]:
        target.print(
            f"  {escape(row['status']):<10} {escape(row['event'])} "
            f"({escape(row['hook'])})",
            soft_wrap=True,
            markup=True,
        )
    if payload["backup_path"]:
        target.print(
            f"  previous content: {escape(payload['backup_path'])}", soft_wrap=True
        )
    if payload["action"] == "install" and not payload["command_on_path"]:
        target.print(
            "[yellow]Note:[/yellow] the trellis-hook command is not on PATH here;"
            " Claude Code runs hooks through your shell, so install the package"
            " where that shell can find it. Until then the hooks do nothing.",
            soft_wrap=True,
        )


def install_agent_rule_command(
    agents_dir: Path = typer.Option(  # noqa: B008 - typer option default
        ...,
        "--agents-dir",
        help="Directory of agent definition *.md files to add the rule to.",
    ),
    uninstall: bool = typer.Option(
        False, "--uninstall", help="Remove the blocks this command added."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would change and write nothing."
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Append the agent rule to each agent definition in a directory.

    The rule goes at the end of each *.md file, inside a marker-delimited
    block, so the frontmatter is never touched. A file that already carries
    the rule (as a block or pasted by hand) is left alone, and --uninstall
    removes exactly the blocks added.
    """
    if output_format not in ("text", "json"):
        _refuse(f"format must be text or json, got {output_format!r}", "text")
    payload: dict[str, Any]
    exit_code = EXIT_OK
    try:
        report = install_agent_rule(
            agents_dir, agent_rule_text(), uninstall=uninstall, dry_run=dry_run
        )
    except NotADirectoryError as exc:
        payload = {"status": "error", "error": str(exc)}
        exit_code = EXIT_VALIDATION
    else:
        failed = bool(report.failed)
        payload = {
            "status": "partial" if failed else "ok",
            "action": report.action,
            "dry_run": report.dry_run,
            "agents_dir": str(report.agents_dir),
            "files": report.files,
        }
        if failed:
            exit_code = EXIT_INTERNAL

    if output_format == "json":
        emit_json(payload)
    elif payload["status"] == "error":
        err_console.print(
            f"[red]Error:[/red] {escape(payload['error'])}", soft_wrap=True
        )
    else:
        action = escape(str(payload["action"]))
        where = escape(payload["agents_dir"])
        if payload["dry_run"]:
            console.print(
                f"[yellow]Dry run:[/yellow] agent rule {action} in {where}",
                soft_wrap=True,
            )
        else:
            console.print(f"agent rule {action} in {where}", soft_wrap=True)
        for row in payload["files"]:
            label = escape(row["status"])
            if row["status"] == STATUS_FAILED:
                label = f"[red]{label}[/red]"
            console.print(f"  {label:<10} {escape(row['file'])}", soft_wrap=True)
    if exit_code != EXIT_OK:
        raise typer.Exit(code=exit_code)


def register(admin_app: typer.Typer) -> None:
    """Mount ``install-hooks`` and ``install-agent-rule`` onto the ``admin`` app."""
    admin_app.command("install-hooks")(install_hooks_command)
    admin_app.command("install-agent-rule")(install_agent_rule_command)
