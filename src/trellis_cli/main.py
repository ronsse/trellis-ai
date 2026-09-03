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
* It does not catch anything but :class:`~trellis.errors.TrellisError`. An
  untyped exception reaching here really is unexpected, and the Typer
  traceback is the right rendering for it — a boundary that folds a
  ``KeyError`` into a tidy actionable envelope turns a bug into an
  operator's problem to diagnose.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.markup import escape
from typer.core import TyperGroup

from trellis.core.error_sanitize import sanitized_error_payload
from trellis.errors import TrellisError
from trellis.logging import configure_stderr_logging
from trellis_cli.admin import admin_app
from trellis_cli.analyze import analyze_app
from trellis_cli.classify import classify_app
from trellis_cli.curate import curate_app
from trellis_cli.demo import demo_app
from trellis_cli.exit_codes import exit_code_for
from trellis_cli.extract_refresh import extract_app
from trellis_cli.ingest import ingest_app
from trellis_cli.metrics import metrics_app
from trellis_cli.output import emit_json
from trellis_cli.policy import policy_app
from trellis_cli.retrieve import retrieve_app
from trellis_cli.serve import serve_app
from trellis_cli.worker import worker_app

if TYPE_CHECKING:
    from collections.abc import Sequence

console = Console()

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
#: without an edit here — rosters of this shape are what rot (#443). The
#: values are caller-authored identifiers, not exception content.
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
        if value is not None:
            context[attr] = value
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


class _BoundaryGroup(TyperGroup):
    """Root group that renders typed Trellis failures instead of a traceback.

    On the group rather than in a ``main()`` wrapper around ``app()`` so it
    is reachable from ``CliRunner`` as well as from the console script: the
    entry point is ``trellis_cli.main:app``, and a wrapper around it would
    leave the boundary untested by the harness this repo's CLI tests use.

    Sub-apps registered with ``add_typer`` build their own default
    ``TyperGroup``\\ s and an exception from a leaf command propagates up
    through them to here, so one class covers every command in the tree.
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
