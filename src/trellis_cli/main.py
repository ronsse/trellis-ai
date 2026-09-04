"""Trellis CLI — trellis.

**The failure boundary lives here** (#459). Every command's error rendering
is a per-command concern right up until nobody wrote one: an uncaught
:class:`~trellis.errors.TrellisError` left the CLI as a Typer rich
traceback, exit ``1``, and — the part a script cares about — *nothing at
all on stdout*, ``--format json`` included. A damaged ``policies.json``
fails every governed write on every surface, so the operator who meets it
first meets it as a stack trace ending in "unexpected; file a bug" for a
file they can fix in one edit.

:class:`_BoundaryGroup` catches that class of failure once, at the root
group. Two things it deliberately does **not** do:

* It does not invent a message. :mod:`trellis.mutate.policy_source` and the
  degradable JSON stores already word the path, the problem and the
  recovery command; this renders ``exc.message`` verbatim rather than
  coining a second vocabulary for the same facts (the ``content_type`` /
  ``document_form`` drift of #325/#326).
* It does not catch broadly. An untyped exception reaching here really is
  unexpected, and the Typer traceback is the right rendering for it — a
  boundary that folds a ``KeyError`` into a tidy actionable envelope turns
  a bug into an operator's problem to diagnose.

The one class caught beside ``TrellisError`` is
:class:`~trellis.retrieve.pack_builder.PackAssemblyError` (#493), named
explicitly rather than by widening the clause. It subclasses
``RuntimeError`` for reasons internal to retrieval, but a pack build whose
every axis failed is a *deployment condition*, not a bug, and it became
CLI-reachable when #488 routed ``trellis retrieve pack`` through
``PackBuilder``. :func:`_render_pack_assembly_failure` carries the
argument for translating it here instead of reparenting it. A second such
class gets a second clause, visible in the diff; a roster dressed up as a
framework would not be.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import typer
from rich.markup import escape
from typer.core import TyperGroup

from trellis.core.error_sanitize import (
    sanitize_error_message,
    sanitized_error_payload,
)
from trellis.errors import TrellisError
from trellis.logging import configure_stderr_logging
from trellis.retrieve.pack_builder import PackAssemblyError
from trellis_cli.admin import admin_app
from trellis_cli.analyze import analyze_app
from trellis_cli.classify import classify_app
from trellis_cli.curate import curate_app
from trellis_cli.demo import demo_app
from trellis_cli.exit_codes import exit_code_for
from trellis_cli.extract_refresh import extract_app
from trellis_cli.ingest import ingest_app
from trellis_cli.metrics import metrics_app
from trellis_cli.output import build_console, emit_json
from trellis_cli.policy import policy_app
from trellis_cli.retrieve import retrieve_app
from trellis_cli.serve import serve_app
from trellis_cli.worker import worker_app

if TYPE_CHECKING:
    from collections.abc import Sequence

console = build_console()

#: ``--format`` values whose consumer is a parser rather than a person. A
#: single JSON object is valid ``jsonl`` too (one record, one line), so both
#: get the same envelope — a ``jsonl`` caller handed Rich prose on the
#: failure path has the #403 problem whichever machine format it asked for.
MACHINE_FORMATS = ("json", "jsonl")

#: Optional attributes copied into the JSON envelope when the exception
#: carries them. Read off the exception rather than kept as a per-class
#: roster: ``ConfigError`` has ``setting``, the ``StoreWriteRefusedError``
#: family has ``path`` / ``recovery``, ``StoreError`` has ``store``, and a
#: subclass added later that carries one of these names is picked up
#: without an edit here — rosters of this shape are what rot (#443).
#:
#: The *names* are Trellis-authored; the *values* are exception content —
#: ``path`` and ``recovery`` are built from a resolved filesystem path —
#: so they go through the same #206 guard as the message. Sanitizing the
#: message and shipping the identical text in a sibling key of the same
#: envelope is a defeated guard, not a guard.
_CONTEXT_ATTRS = ("setting", "path", "recovery", "store", "policy_id")


def _raw_subcommand_args(ctx: Any) -> list[str]:
    """The unparsed argv tail the root group is about to dispatch.

    Typed ``Any`` because Typer 0.27 vendors click privately as
    ``typer._click``: the runtime class is ``typer._click.core.Context``,
    not ``click.core.Context``, and naming either one pins this module to
    an implementation detail of a dependency for no checking worth having
    on a three-line ``getattr`` chain.

    ``--format`` is a *subcommand* option, so by the time an exception
    reaches the root group the sub-context that parsed it has been torn
    down and ``click.get_current_context()`` is gone. The raw args are
    still readable here, before ``Group.invoke`` consumes them, and they
    are all :func:`_requested_format` needs.

    Read from the context rather than ``sys.argv`` on purpose: click's
    ``CliRunner`` never assigns ``sys.argv``, so an argv-based reader would
    be unreachable from the harness every other CLI test in this repo uses
    — a guard that cannot be exercised by the suite that guards it.
    """
    protected = getattr(ctx, "_protected_args", None)
    if protected is None:
        protected = getattr(ctx, "protected_args", None) or []
    return [*protected, *ctx.args]


def _requested_format(args: Sequence[str]) -> str:
    """The ``--format`` value in *args*, or ``"text"``.

    Last occurrence wins, matching click's own semantics for a
    non-``multiple`` option. A command with no ``--format`` option cannot
    reach a machine consumer at all, so defaulting to ``text`` fails in the
    safe direction: the worst case is human-readable output where JSON was
    never possible, never a Rich table where a parser was waiting.
    """
    output_format = "text"
    for index, arg in enumerate(args):
        if arg == "--format" and index + 1 < len(args):
            output_format = args[index + 1]
        elif arg.startswith("--format="):
            output_format = arg.split("=", 1)[1]
    return output_format


def _error_context(exc: TrellisError) -> dict[str, Any]:
    """The caller-actionable fields *exc* carries, for the JSON envelope."""
    context: dict[str, Any] = {"error_code": exc.code}
    for attr in _CONTEXT_ATTRS:
        value = getattr(exc, attr, None)
        if value is None:
            continue
        context[attr] = (
            sanitize_error_message(value) if isinstance(value, str) else value
        )
    return context


def _render_boundary_failure(exc: TrellisError, output_format: str) -> None:
    """Render an uncaught typed failure on the caller's surface, then exit.

    The exit sits below the format branch and the payload's ``status``
    comes from the same flag, per ``docs/design/adr-cli-exit-codes.md`` and
    the rule ``tests/unit/test_format_exit_parity_rule.py`` enforces: the
    machine surface is the one a script reads, so it must not be the one
    that reports success.

    The JSON message goes through
    :func:`~trellis.core.error_sanitize.sanitized_error_payload` — the same
    envelope every other ``--format json`` error in this CLI emits — and
    that is also what makes catching the whole ``TrellisError`` family safe
    here: a ``StoreError`` raised from a driver can echo a DSN, and the
    leak guard for a machine-readable artifact is not something a new
    boundary should re-derive (#206). The text surface prints
    ``exc.message`` unsanitized, as every other error render in this CLI
    does; it is going to a terminal, not into an artifact.
    """
    if output_format in MACHINE_FORMATS:
        emit_json(sanitized_error_payload(exc, **_error_context(exc)))
    else:
        console.print(
            f"[bold red]{escape(exc.code)}[/bold red] — {escape(exc.message)}",
            soft_wrap=True,
        )
    raise typer.Exit(code=exit_code_for(exc))


def _render_pack_assembly_failure(exc: PackAssemblyError, output_format: str) -> None:
    """Render an all-axes-failed pack build, then exit (#493).

    :class:`~trellis.retrieve.pack_builder.PackAssemblyError` subclasses
    ``RuntimeError``, not :class:`~trellis.errors.TrellisError`, so the
    clause above never saw it and an operator whose every retrieval axis
    was down got a traceback: no exit-code contract, no ``--format json``
    envelope, none of the boundary's framing. It became reachable when
    #488 routed ``trellis retrieve pack`` through ``PackBuilder``; a
    second CLI caller, ``analyze pack-quality``, has the same exposure
    and is covered by the same clause.

    **Why not reparent it to ``TrellisError``, which #493 asked to be
    decided deliberately.** Reparenting changes what
    :class:`~trellis.mutate.executor.MutationExecutor` and every other
    ``except TrellisError`` in the tree catches, for a class raised deep
    inside retrieval — the blast radius #483 declined for
    ``RegistryValidationError``. And it would buy nothing extra:
    :func:`~trellis_cli.exit_codes.exit_code_for` resolves it to
    ``EXIT_INTERNAL`` either way, because it is not a Store, Config,
    Validation, Policy or Idempotency error. Same envelope, same exit
    code, one file touched instead of the hierarchy.

    **The issue's stated cost of the local fix does not apply.** #493 said
    a CLI-local catch "leaves the REST and MCP surfaces with the same
    gap"; neither surface has it. MCP wraps the build in ``except
    Exception`` and documents ``PackAssemblyError`` → ``INTERNAL_ERROR``,
    and ``trellis_api`` registers an ``Exception`` handler that answers a
    structured 500. The CLI was the only affected surface.

    Catching this one class by name rather than widening the clause to
    ``Exception``: an untyped exception reaching here really is
    "unexpected; file a bug", and ``exit_code_for``'s docstring says so.
    Dressing a genuine crash up as an actionable envelope is the lie that
    function exists to remove. A pack whose every axis failed is not a
    crash — it is a deployment condition an operator can act on.

    Structured exactly like :func:`_render_boundary_failure`: the exit
    sits *below* the format branch so both surfaces agree the command
    failed (the rule ``tests/unit/test_format_exit_parity_rule.py``
    enforces), and the JSON message goes through
    :func:`~trellis.core.error_sanitize.sanitized_error_payload` while
    the text surface prints unsanitized to a terminal.

    ``strategy_failures`` rides the JSON envelope and the text render
    because the exception's own message does not carry it: the all-failed
    message says how many strategies failed, not *which*, and "which axis
    is down" is the whole of what the operator needs next.
    """
    failures = [failure.to_event_payload() for failure in exc.strategy_failures]
    if output_format in MACHINE_FORMATS:
        emit_json(
            sanitized_error_payload(
                exc,
                error_code=type(exc).__name__,
                strategy_failures=[
                    {
                        "strategy": failure["strategy"],
                        "error_class": failure["error_class"],
                        "message": sanitize_error_message(failure["message"]),
                    }
                    for failure in failures
                ],
            )
        )
    else:
        console.print(
            f"[bold red]{escape(type(exc).__name__)}[/bold red] — {escape(str(exc))}",
            soft_wrap=True,
        )
        for failure in failures:
            console.print(
                f"  [dim]{escape(failure['strategy'])}[/dim]: "
                f"{escape(failure['error_class'])}: {escape(failure['message'])}",
                soft_wrap=True,
            )
    raise typer.Exit(code=exit_code_for(exc))


class _BoundaryGroup(TyperGroup):
    """Root group that renders typed Trellis failures instead of a traceback.

    On the group rather than in a ``main()`` wrapper around ``app()`` so it
    is reachable from ``CliRunner`` as well as from the console script: the
    entry point is ``trellis_cli.main:app``, and a wrapper around it would
    leave the boundary untested by the harness this repo's CLI tests use.

    Sub-apps registered with ``add_typer`` build their own default
    ``TyperGroup``\\ s and an exception from a leaf command propagates up
    through them to here, so one class covers every command in the tree.
    That is why #493's translation goes here and not into ``retrieve
    pack``: ``analyze pack-quality`` builds a pack too, and a per-command
    ``except`` would have fixed one of the two and left the other to be
    found again.
    """

    def invoke(self, ctx: Any) -> Any:
        # ``ctx`` is ``Any`` for the reason ``_raw_subcommand_args`` gives:
        # the vendored click context is private to Typer, and annotating
        # the public ``click.Context`` here is an LSP violation mypy
        # (correctly) rejects.
        # Snapshot first: ``super().invoke`` clears these to dispatch.
        raw_args = _raw_subcommand_args(ctx)
        try:
            return super().invoke(ctx)
        except TrellisError as exc:
            _render_boundary_failure(exc, _requested_format(raw_args))
            raise  # unreachable — the helper exits on every path
        except PackAssemblyError as exc:
            _render_pack_assembly_failure(exc, _requested_format(raw_args))
            raise  # unreachable — the helper exits on every path


app = typer.Typer(
    name="trellis",
    help="Trellis — shared experience store for AI agents and teams.",
    no_args_is_help=True,
    cls=_BoundaryGroup,
)


@app.callback()
def _root(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show INFO-level logs (default: WARNING)."
    ),
    debug: bool = typer.Option(False, "--debug", help="Show DEBUG-level logs."),
) -> None:
    # CLI defaults to WARNING so per-command stderr stays quiet next to
    # the friendly Rich output. ``TRELLIS_LOG_LEVEL`` always wins so
    # operators can pin a level globally; the flags only set defaults
    # when the env var is absent. Routing structlog to stderr keeps
    # ``--format json`` stdout parseable.
    if "TRELLIS_LOG_LEVEL" not in os.environ:
        if debug:
            os.environ["TRELLIS_LOG_LEVEL"] = "DEBUG"
        elif verbose:
            os.environ["TRELLIS_LOG_LEVEL"] = "INFO"
        else:
            os.environ["TRELLIS_LOG_LEVEL"] = "WARNING"
    configure_stderr_logging()


# Register command groups
app.add_typer(admin_app, name="admin", help="Administration and setup")

app.add_typer(ingest_app, name="ingest")
app.add_typer(
    extract_app,
    name="extract",
    help="Re-run extractors and emit structural diffs",
)
app.add_typer(
    classify_app,
    name="classify",
    help="Backfill, shadow-tag, and mine promotion candidates for content tags",
)
app.add_typer(curate_app, name="curate")
app.add_typer(retrieve_app, name="retrieve")
app.add_typer(analyze_app, name="analyze")
app.add_typer(
    metrics_app,
    name="metrics",
    help="Feedback-driven parameter-tuning telemetry and promotion",
)
app.add_typer(policy_app, name="policy", help="Manage governance policies")
app.add_typer(demo_app, name="demo", help="Demo data and exploration")
app.add_typer(worker_app, name="worker")
app.add_typer(serve_app, name="serve", help="Run the Trellis REST API + UI")


if __name__ == "__main__":
    app()
